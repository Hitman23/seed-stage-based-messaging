import responses
import json
from uuid import uuid4
from requests.exceptions import HTTPError
from datetime import timedelta, datetime

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

import pytz

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.db.models.signals import post_save
from django.utils import timezone

from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.authtoken.models import Token

from .models import (Subscription, SubscriptionSendFailure,
                     fire_sub_action_if_new, disable_schedule_if_complete,
                     disable_schedule_if_deactivated, fire_metrics_if_new,
                     fire_metric_per_message_set, fire_metric_per_lang,
                     fire_metric_per_message_format)
from contentstore.models import Schedule, MessageSet, BinaryContent, Message
from .tasks import (schedule_create, schedule_disable, fire_metric,
                    scheduled_metrics)
from . import tasks


class APITestCase(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.adminclient = APIClient()


class AuthenticatedAPITestCase(APITestCase):

    def make_schedule(self):
        # Create hourly schedule
        schedule_data = {
            'hour': 1
        }
        return Schedule.objects.create(**schedule_data)

    def make_messageset(self):
        messageset_data = {
            'short_name': 'messageset_one',
            'notes': None,
            'next_set': None,
            'default_schedule': self.schedule,
            'content_type': 'text'
        }
        return MessageSet.objects.create(**messageset_data)

    def make_messageset_audio(self):
        messageset_data = {
            'short_name': 'messageset_two',
            'notes': None,
            'next_set': None,
            'default_schedule': self.schedule,
            'content_type': 'audio'
        }
        return MessageSet.objects.create(**messageset_data)

    def make_subscription(self):
        post_data = {
            "identity": "8646b7bc-b511-4965-a90b-e1145e398703",
            "messageset": self.messageset,
            "next_sequence_number": 1,
            "lang": "eng_ZA",
            "active": True,
            "completed": False,
            "schedule": self.schedule,
            "process_status": 0,
            "metadata": {
                "source": "RapidProVoice"
            }
        }
        return Subscription.objects.create(**post_data)

    def make_subscription_welcome(self):
        post_data = {
            "identity": "8646b7bc-b511-4965-a90b-e1145e398703",
            "messageset": self.messageset,
            "next_sequence_number": 1,
            "lang": "eng_ZA",
            "active": True,
            "completed": False,
            "schedule": self.schedule,
            "process_status": 0,
            "metadata": {
                "prepend_next_delivery": "Welcome to your messages!"
            }
        }
        return Subscription.objects.create(**post_data)

    def make_subscription_audio(self):
        post_data = {
            "identity": "8646b7bc-b511-4965-a90b-e1145e398703",
            "messageset": self.messageset_audio,
            "next_sequence_number": 1,
            "lang": "eng_ZA",
            "active": True,
            "completed": False,
            "schedule": self.schedule,
            "process_status": 0,
            "metadata": {
                "source": "RapidProVoice"
            }
        }
        return Subscription.objects.create(**post_data)

    def make_subscription_audio_welcome(self):
        post_data = {
            "identity": "8646b7bc-b511-4965-a90b-e1145e398703",
            "messageset": self.messageset_audio,
            "next_sequence_number": 1,
            "lang": "eng_ZA",
            "active": True,
            "completed": False,
            "schedule": self.schedule,
            "process_status": 0,
            "metadata": {
                "prepend_next_delivery": "http://example.com/welcome.mp3"
            }
        }
        return Subscription.objects.create(**post_data)

    def _replace_post_save_hooks(self):
        def has_listeners():
            return post_save.has_listeners(Subscription)
        assert has_listeners(), (
            "Subscription model has no post_save listeners. Make sure"
            " helpers cleaned up properly in earlier tests.")
        post_save.disconnect(fire_sub_action_if_new, sender=Subscription)
        post_save.disconnect(disable_schedule_if_complete, sender=Subscription)
        post_save.disconnect(disable_schedule_if_deactivated,
                             sender=Subscription)
        post_save.disconnect(fire_metrics_if_new, sender=Subscription)
        post_save.disconnect(fire_metric_per_message_set, sender=Subscription)
        post_save.disconnect(fire_metric_per_lang, sender=Subscription)
        post_save.disconnect(fire_metric_per_message_format,
                             sender=Subscription)
        assert not has_listeners(), (
            "Subscription model still has post_save listeners. Make sure"
            " helpers cleaned up properly in earlier tests.")

    def _restore_post_save_hooks(self):
        def has_listeners():
            return post_save.has_listeners(Subscription)
        assert not has_listeners(), (
            "Subscription model still has post_save listeners. Make sure"
            " helpers removed them properly in earlier tests.")
        post_save.connect(fire_sub_action_if_new, sender=Subscription)
        post_save.connect(disable_schedule_if_complete, sender=Subscription)
        post_save.connect(disable_schedule_if_deactivated, sender=Subscription)
        post_save.connect(fire_metrics_if_new, sender=Subscription)
        post_save.connect(fire_metric_per_message_set, sender=Subscription)
        post_save.connect(fire_metric_per_lang, sender=Subscription)
        post_save.connect(fire_metric_per_message_format, sender=Subscription)

    def _add_metrics_response(self):
        responses.add(
            responses.POST, 'http://metrics-url/metrics/', json={}, status=201)

    def setUp(self):
        super(AuthenticatedAPITestCase, self).setUp()

        self._replace_post_save_hooks()

        self.username = 'testuser'
        self.password = 'testpass'
        self.user = User.objects.create_user(self.username,
                                             'testuser@example.com',
                                             self.password)
        token = Token.objects.create(user=self.user)
        self.token = token.key
        self.client.credentials(HTTP_AUTHORIZATION='Token ' + self.token)
        self.schedule = self.make_schedule()
        self.messageset = self.make_messageset()
        self.messageset_audio = self.make_messageset_audio()
        self.superuser = User.objects.create_superuser('testsu',
                                                       'su@example.com',
                                                       'dummypwd')
        sutoken = Token.objects.create(user=self.superuser)
        self.adminclient.credentials(
            HTTP_AUTHORIZATION='Token %s' % sutoken)
        self._add_metrics_response()

    def tearDown(self):
        self._restore_post_save_hooks()


class TestLogin(AuthenticatedAPITestCase):

    def test_login(self):
        # Setup
        post_auth = {"username": "testuser",
                     "password": "testpass"}
        # Execute
        request = self.client.post(
            '/api/token-auth/', post_auth)
        token = request.data.get('token', None)
        # Check
        self.assertIsNotNone(
            token, "Could not receive authentication token on login post.")
        self.assertEqual(
            request.status_code, 200,
            "Status code on /api/token-auth was %s (should be 200)."
            % request.status_code)


class TestSubscriptionsAPI(AuthenticatedAPITestCase):

    def test_create_subscription_data(self):
        # Setup
        post_subscription = {
            "identity": "7646b7bc-b511-4965-a90b-e1145e398703",
            "messageset": self.messageset.id,
            "next_sequence_number": 1,
            "lang": "eng_ZA",
            "active": True,
            "completed": False,
            "schedule": self.schedule.id,
            "process_status": 0,
            "metadata": {
                "source": "RapidProVoice"
            }
        }
        # Execute
        response = self.client.post('/api/v1/subscriptions/',
                                    json.dumps(post_subscription),
                                    content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        d = Subscription.objects.last()
        self.assertIsNotNone(d.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(d.messageset.id, self.messageset.id)
        self.assertEqual(d.next_sequence_number, 1)
        self.assertEqual(d.lang, "eng_ZA")
        self.assertEqual(d.active, True)
        self.assertEqual(d.completed, False)
        self.assertEqual(d.schedule.id, self.schedule.id)
        self.assertEqual(d.process_status, 0)
        self.assertEqual(d.metadata["source"], "RapidProVoice")

    def test_read_subscription_data(self):
        # Setup
        existing = self.make_subscription()
        # Execute
        response = self.client.get('/api/v1/subscriptions/%s/' % existing.id,
                                   content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        d = Subscription.objects.last()
        self.assertIsNotNone(d.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(d.messageset.id, self.messageset.id)
        self.assertEqual(d.next_sequence_number, 1)
        self.assertEqual(d.lang, "eng_ZA")
        self.assertEqual(d.active, True)
        self.assertEqual(d.completed, False)
        self.assertEqual(d.schedule.id, self.schedule.id)
        self.assertEqual(d.process_status, 0)
        self.assertEqual(d.metadata["source"], "RapidProVoice")

    def test_filter_subscription_data(self):
        # Setup
        sub_active = self.make_subscription()
        sub_inactive = self.make_subscription()
        sub_inactive.active = False
        sub_inactive.save()
        # Precheck
        self.assertEqual(sub_active.active, True)
        self.assertEqual(sub_inactive.active, False)
        # Execute
        response = self.client.get(
            '/api/v1/subscriptions/',
            {"identity": "8646b7bc-b511-4965-a90b-e1145e398703",
             "active": "True"},
            content_type='application/json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(sub_active.id))

    def test_filter_subscription_created_after(self):
        self.make_subscription()
        sub2 = self.make_subscription()
        sub3 = self.make_subscription()

        response = self.client.get(
            '/api/v1/subscriptions/',
            {"created_after": sub2.created_at.isoformat()},
            content_type='application/json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        ids = set(s['id'] for s in response.data['results'])
        self.assertEqual(set([str(sub2.id), str(sub3.id)]), ids)

    def test_filter_subscription_created_before(self):
        sub1 = self.make_subscription()
        sub2 = self.make_subscription()
        self.make_subscription()

        response = self.client.get(
            '/api/v1/subscriptions/',
            {"created_before": sub2.created_at.isoformat()},
            content_type='application/json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        ids = set(s['id'] for s in response.data['results'])
        self.assertEqual(set([str(sub1.id), str(sub2.id)]), ids)

    def test_update_subscription_data(self):
        # Setup
        existing = self.make_subscription()
        patch_subscription = {
            "next_sequence_number": 10,
            "active": False,
            "completed": True
        }
        # Execute
        response = self.client.patch('/api/v1/subscriptions/%s/' % existing.id,
                                     json.dumps(patch_subscription),
                                     content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        d = Subscription.objects.get(pk=existing.id)
        self.assertEqual(d.active, False)
        self.assertEqual(d.completed, True)
        self.assertEqual(d.next_sequence_number, 10)
        self.assertEqual(d.lang, "eng_ZA")

    def test_delete_subscription_data(self):
        # Setup
        existing = self.make_subscription()
        # Execute
        response = self.client.delete(
            '/api/v1/subscriptions/%s/' % existing.id,
            content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        d = Subscription.objects.filter(id=existing.id).count()
        self.assertEqual(d, 0)


class TestCreateScheduleTask(AuthenticatedAPITestCase):

    @responses.activate
    def test_create_schedule_task(self):
        # Setup
        # make schedule
        schedule_data = {
            "minute": "1",
            "hour": "6",
            "day_of_week": "1",
            "day_of_month": "*",
            "month_of_year": "*"
        }
        schedule = Schedule.objects.create(**schedule_data)

        # make messageset
        messageset_data = {
            "short_name": "pregnancy",
            "notes": "Base pregancy set",
            "next_set": None,
            "default_schedule": schedule
        }
        messageset = MessageSet.objects.create(**messageset_data)

        # make binarycontent
        binarycontent_data1 = {
            "content": "fakefilename",
        }
        binarycontent1 = BinaryContent.objects.create(**binarycontent_data1)
        binarycontent_data2 = {
            "content": "fakefilename",
        }
        binarycontent2 = BinaryContent.objects.create(**binarycontent_data2)

        # make messages
        message_data1 = {
            "messageset": messageset,
            "sequence_number": 1,
            "lang": "eng_ZA",
            "binary_content": binarycontent1,
        }
        Message.objects.create(**message_data1)
        message_data2 = {
            "messageset": messageset,
            "sequence_number": 2,
            "lang": "eng_ZA",
            "binary_content": binarycontent2,
        }
        Message.objects.create(**message_data2)

        # make subscription
        post_data = {
            "identity": "8646b7bc-b511-4965-a90b-e1145e398703",
            "messageset": messageset,
            "next_sequence_number": 1,
            "lang": "eng_ZA",
            "active": True,
            "completed": False,
            "schedule": schedule,
            "process_status": 0,
            "metadata": {
                "source": "RapidProVoice"
            }
        }
        existing = Subscription.objects.create(**post_data)

        # Create schedule
        schedule_post = {
            "id": "6455245a-028b-4fa1-82fc-6b639c4e7710",
            "cron_definition": "1 6 1 * *",
            "endpoint": "%s/%s/%s/send" % (
                "http://seed-stage-based-messaging/api/v1",
                "subscription",
                str(existing.id)),
            "frequency": None,
            "messages": None,
            "triggered": 0,
            "created_at": "2015-04-05T21:59:28Z",
            "updated_at": "2015-04-05T21:59:28Z"
        }
        responses.add(responses.POST,
                      "http://seed-scheduler/api/v1/schedule/",
                      json.dumps(schedule_post),
                      status=200, content_type='application/json')

        result = schedule_create.apply_async(args=[str(existing.id)])
        self.assertEqual(
            str(result.get()), "6455245a-028b-4fa1-82fc-6b639c4e7710")

        d = Subscription.objects.get(pk=existing.id)
        self.assertIsNotNone(d.id)
        self.assertEqual(
            d.metadata["scheduler_schedule_id"],
            "6455245a-028b-4fa1-82fc-6b639c4e7710")

    @responses.activate
    def test_disable_schedule_task(self):
        # Setup
        subscription = self.make_subscription()
        schedule_id = "6455245a-028b-4fa1-82fc-6b639c4e7710"
        subscription.metadata["scheduler_schedule_id"] = schedule_id
        subscription.save()

        # mock schedule update
        responses.add(
            responses.PATCH,
            "http://seed-scheduler/api/v1/schedule/%s/" % schedule_id,
            json.dumps({"enabled": False}),
            status=200, content_type='application/json')

        # Execute
        result = schedule_disable.apply_async(args=[str(subscription.id)])

        # Check
        self.assertEqual(result.get(), True)
        self.assertEqual(len(responses.calls), 1)


class TestSubscriptionsWebhookListener(AuthenticatedAPITestCase):

    def test_webhook_subscription_data_good(self):
        # Setup
        post_webhook = {
            "hook": {
                "id": 5,
                "event": "subscriptionrequest.added",
                "target": "http://example.com/api/v1/subscriptions/request"
            },
            "data": {
                "messageset": self.messageset.id,
                "updated_at": "2016-02-17T07:59:42.831568+00:00",
                "identity": "7646b7bc-b511-4965-a90b-e1145e398703",
                "lang": "eng_ZA",
                "created_at": "2016-02-17T07:59:42.831533+00:00",
                "id": "5282ed58-348f-4a54-b1ff-f702e36ec3cc",
                "next_sequence_number": 2,
                "schedule": self.schedule.id
            }
        }
        # Execute
        response = self.client.post('/api/v1/subscriptions/request',
                                    json.dumps(post_webhook),
                                    content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        d = Subscription.objects.last()
        self.assertIsNotNone(d.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(d.messageset.id, self.messageset.id)
        self.assertEqual(d.next_sequence_number, 2)
        self.assertEqual(d.initial_sequence_number, 2)
        self.assertEqual(d.lang, "eng_ZA")
        self.assertEqual(d.active, True)
        self.assertEqual(d.completed, False)
        self.assertEqual(d.schedule.id, self.schedule.id)
        self.assertEqual(d.process_status, 0)

    def test_webhook_subscription_data_bad(self):
        # Setup with missing identity
        post_webhook = {
            "hook": {
                "id": 5,
                "event": "subscriptionrequest.added",
                "target": "http://example.com/api/v1/subscriptions/request"
            },
            "data": {
                "messageset": self.messageset.id,
                "updated_at": "2016-02-17T07:59:42.831568+00:00",
                "lang": "eng_ZA",
                "created_at": "2016-02-17T07:59:42.831533+00:00",
                "id": "5282ed58-348f-4a54-b1ff-f702e36ec3cc",
                "next_sequence_number": 1,
                "schedule": self.schedule.id
            }
        }
        # Execute
        response = self.client.post('/api/v1/subscriptions/request',
                                    json.dumps(post_webhook),
                                    content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(),
                         {"identity": ["This field is required."]})


class TestSendMessageTask(AuthenticatedAPITestCase):

    @responses.activate
    def test_send_message_task_to_mother_text(self):
        post_save.connect(fire_sub_action_if_new, sender=Subscription)
        # mock schedule sending
        responses.add(
            responses.POST,
            "http://seed-scheduler/api/v1/schedule/",
            json={
                "id": "1234"
            },
            status=201, content_type='application/json'
        )
        # Setup
        existing = self.make_subscription()
        self.messageset.channel = 'CHANNEL1'
        self.messageset.save()

        # Precheck
        subs_all = Subscription.objects.all()
        self.assertEqual(subs_all.count(), 1)
        scheds_all = Schedule.objects.all()
        self.assertEqual(scheds_all.count(), 1)

        # mock identity address lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/addresses/msisdn?default=True&use_communicate_through=True" % (existing.identity, ),  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"address": "+2345059992222"}]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # Create message sender call
        responses.add(
            responses.POST,
            "http://seed-message-sender/api/v1/outbound/",
            json={
                "url": "http://seed-message-sender/api/v1/outbound/c7f3c839-2bf5-42d1-86b9-ccb886645fb4/",  # noqa
                "id": "c7f3c839-2bf5-42d1-86b9-ccb886645fb4",
                "version": 1,
                "to_addr": "+2345059992222",
                "to_identity": "8646b7bc-b511-4965-a90b-e1145e398703",
                "vumi_message_id": None,
                "content": "This is message 1",
                "delivered": False,
                "attempts": 0,
                "metadata": {},
                "created_at": "2016-03-24T13:43:43.614952Z",
                "updated_at": "2016-03-24T13:43:43.614921Z"
            },
            status=200, content_type='application/json'
        )

        # Create metrics call - deactivate TestSession for this
        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json; charset=utf-8'
        )

        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json; charset=utf-8'
        )

        # make messages
        message_data_eng_1 = {
            "messageset": existing.messageset,
            "sequence_number": 1,
            "lang": "eng_ZA",
            "text_content": "This is message 1",
        }
        Message.objects.create(**message_data_eng_1)
        message_data_eng_2 = {
            "messageset": existing.messageset,
            "sequence_number": 2,
            "lang": "eng_ZA",
            "text_content": "This is message 2",
        }
        Message.objects.create(**message_data_eng_2)
        message_data_eng_3 = {
            "messageset": existing.messageset,
            "sequence_number": 3,
            "lang": "eng_ZA",
            "text_content": "This is message 3",
        }
        Message.objects.create(**message_data_eng_3)
        message_data_zul_1 = {
            "messageset": existing.messageset,
            "sequence_number": 1,
            "lang": "zu_ZA",
            "text_content": "Ke msg 1",
        }
        Message.objects.create(**message_data_zul_1)
        message_data_zul_2 = {
            "messageset": existing.messageset,
            "sequence_number": 2,
            "lang": "zu_ZA",
            "text_content": "Ke msg 2",
        }
        Message.objects.create(**message_data_zul_2)
        message_data_zul_3 = {
            "messageset": existing.messageset,
            "sequence_number": 3,
            "lang": "zu_ZA",
            "text_content": "Ke msg 3",
        }
        Message.objects.create(**message_data_zul_3)

        # Execute
        response = self.client.post('/api/v1/subscriptions/%s/send' % (
            existing.id, ), content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        d = Subscription.objects.get(id=existing.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(d.messageset.id, self.messageset.id)
        self.assertEqual(d.next_sequence_number, 2)
        self.assertEqual(d.active, True)
        self.assertEqual(d.completed, False)
        self.assertEqual(d.process_status, 0)
        subs_all = Subscription.objects.all()
        self.assertEqual(subs_all.count(), 1)
        scheds_all = Schedule.objects.all()
        self.assertEqual(scheds_all.count(), 1)
        self.assertEqual(len(responses.calls), 5)

        # Check the request body of metric call
        metric_call = responses.calls[3]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.text.messageset_one.sum": 1.0
        })

        metric_call = responses.calls[4]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.text.sum": 1.0
        })

        # check that channel is sent to message sender
        sender_call = responses.calls[2]
        self.assertEqual(json.loads(sender_call.request.body).get("channel"),
                         "CHANNEL1")

        # Check the message_count / set_max count
        message_count = existing.messageset.messages.filter(
            lang=existing.lang).count()
        self.assertEqual(message_count, 3)

        post_save.disconnect(fire_sub_action_if_new, sender=Subscription)

    @responses.activate
    def test_send_message_task_to_mother_text_welcome(self):
        # Setup
        existing = self.make_subscription_welcome()

        # mock identity address lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/addresses/msisdn?default=True&use_communicate_through=True" % (existing.identity, ),  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"address": "+2345059992222"}]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # Create message sender call
        responses.add(
            responses.POST,
            "http://seed-message-sender/api/v1/outbound/",
            json={
                "url": "http://seed-message-sender/api/v1/outbound/c7f3c839-2bf5-42d1-86b9-ccb886645fb4/",  # noqa
                "id": "c7f3c839-2bf5-42d1-86b9-ccb886645fb4",
                "version": 1,
                "to_addr": "+2345059992222",
                "to_identity": "8646b7bc-b511-4965-a90b-e1145e398703",
                "vumi_message_id": None,
                "content": "Welcome to your messages!\nThis is message 1",
                "delivered": False,
                "attempts": 0,
                "metadata": {},
                "created_at": "2016-03-24T13:43:43.614952Z",
                "updated_at": "2016-03-24T13:43:43.614921Z"
            },
            status=200, content_type='application/json'
        )

        # Create metrics call - deactivate TestSession for this
        self.session = None
        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json'
        )

        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json'
        )

        # make messages
        message_data1 = {
            "messageset": existing.messageset,
            "sequence_number": 1,
            "lang": "eng_ZA",
            "text_content": "This is message 1",
        }
        Message.objects.create(**message_data1)
        message_data2 = {
            "messageset": existing.messageset,
            "sequence_number": 2,
            "lang": "eng_ZA",
            "text_content": "This is message 2",
        }
        Message.objects.create(**message_data2)

        # Execute
        response = self.client.post('/api/v1/subscriptions/%s/send' % (
            existing.id, ), content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        d = Subscription.objects.get(id=existing.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(d.messageset.id, self.messageset.id)
        self.assertEqual(d.next_sequence_number, 2)
        self.assertEqual(d.active, True)
        self.assertEqual(d.completed, False)
        self.assertEqual(d.process_status, 0)
        self.assertEqual(d.metadata["prepend_next_delivery"], None)

        # Check the request body of metric call
        metric_call = responses.calls[2]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.text.messageset_one.sum": 1.0
        })

        metric_call = responses.calls[3]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.text.sum": 1.0
        })

    @responses.activate
    def test_send_message_task_to_mother_text_last(self):
        # Setup
        post_save.connect(disable_schedule_if_complete, sender=Subscription)
        schedule_id = "6455245a-028b-4fa1-82fc-6b639c4e7710"
        existing = self.make_subscription()
        existing.metadata["scheduler_schedule_id"] = schedule_id
        existing.next_sequence_number = 2  # fast forward to end
        existing.save()

        # add a next message set
        messageset_data = {
            'short_name': 'messageset_two_text',
            'notes': None,
            'next_set': None,
            'default_schedule': self.schedule,
            'content_type': 'text'
        }
        next_message_set = MessageSet.objects.create(**messageset_data)
        messageset = existing.messageset
        messageset.next_set = next_message_set
        messageset.save()

        # mock identity address lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/addresses/msisdn?default=True&use_communicate_through=True" % (existing.identity, ),  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"address": "+2345059992222"}]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # mock message sender call
        responses.add(
            responses.POST,
            "http://seed-message-sender/api/v1/outbound/",
            json={
                "url": "http://seed-message-sender/api/v1/outbound/c7f3c839-2bf5-42d1-86b9-ccb886645fb4/",  # noqa
                "id": "c7f3c839-2bf5-42d1-86b9-ccb886645fb4",
                "version": 1,
                "to_addr": "+2345059992222",
                "to_identity": "8646b7bc-b511-4965-a90b-e1145e398703",
                "vumi_message_id": None,
                "content": "This is message 2",
                "delivered": False,
                "attempts": 0,
                "metadata": {},
                "created_at": "2016-03-24T13:43:43.614952Z",
                "updated_at": "2016-03-24T13:43:43.614921Z"
            },
            status=200, content_type='application/json'
        )

        # mock schedule update
        responses.add(
            responses.PATCH,
            "http://seed-scheduler/api/v1/schedule/%s/" % schedule_id,
            json.dumps({"enabled": False}),
            status=200, content_type='application/json')

        # Create metrics call - deactivate TestSession for this
        self.session = None
        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={'foo': "bar"},
            status=200, content_type='application/json'
        )

        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={'foo': "bar"},
            status=200, content_type='application/json'
        )

        # make messages
        message_data1 = {
            "messageset": existing.messageset,
            "sequence_number": 1,
            "lang": "eng_ZA",
            "text_content": "This is message 1",
        }
        Message.objects.create(**message_data1)
        message_data2 = {
            "messageset": existing.messageset,
            "sequence_number": 2,
            "lang": "eng_ZA",
            "text_content": "This is message 2",
        }
        Message.objects.create(**message_data2)

        # Execute
        response = self.client.post('/api/v1/subscriptions/%s/send' % (
            existing.id, ), content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        d = Subscription.objects.get(id=existing.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(d.messageset.id, self.messageset.id)
        self.assertEqual(d.next_sequence_number, 2)
        self.assertEqual(d.active, False)
        self.assertEqual(d.completed, True)
        self.assertEqual(d.process_status, 2)
        self.assertEqual(len(responses.calls), 5)

        # make sure a subscription is created on the next message set
        subs_active = Subscription.objects.filter(
            identity=existing.identity, active=True)
        self.assertEqual(subs_active.count(), 1)
        self.assertEqual(subs_active[0].messageset, d.messageset.next_set)
        self.assertEqual(subs_active[0].next_sequence_number, 1)
        self.assertEqual(subs_active[0].initial_sequence_number, 1)
        self.assertEqual(subs_active[0].completed, False)

        # Check the request body of metric call
        metric_call = responses.calls[3]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.text.messageset_one.sum": 1.0
        })
        metric_call = responses.calls[4]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.text.sum": 1.0
        })

        post_save.disconnect(disable_schedule_if_complete, sender=Subscription)

    @responses.activate
    def test_send_message_task_to_mother_text_in_process(self):
        # Setup
        existing = self.make_subscription()
        existing.process_status = 1
        existing.save()

        # Precheck for comparison
        self.assertEqual(existing.next_sequence_number, 1)
        subs_all = Subscription.objects.all()
        self.assertEqual(subs_all.count(), 1)
        scheds_all = Schedule.objects.all()
        self.assertEqual(scheds_all.count(), 1)

        # make messages
        message_data_eng_1 = {
            "messageset": existing.messageset,
            "sequence_number": 1,
            "lang": "eng_ZA",
            "text_content": "This is message 1",
        }
        Message.objects.create(**message_data_eng_1)
        message_data_eng_2 = {
            "messageset": existing.messageset,
            "sequence_number": 2,
            "lang": "eng_ZA",
            "text_content": "This is message 2",
        }
        Message.objects.create(**message_data_eng_2)
        message_data_eng_3 = {
            "messageset": existing.messageset,
            "sequence_number": 3,
            "lang": "eng_ZA",
            "text_content": "This is message 3",
        }
        Message.objects.create(**message_data_eng_3)
        message_data_zul_1 = {
            "messageset": existing.messageset,
            "sequence_number": 1,
            "lang": "zu_ZA",
            "text_content": "Ke msg 1",
        }
        Message.objects.create(**message_data_zul_1)
        message_data_zul_2 = {
            "messageset": existing.messageset,
            "sequence_number": 2,
            "lang": "zu_ZA",
            "text_content": "Ke msg 2",
        }
        Message.objects.create(**message_data_zul_2)
        message_data_zul_3 = {
            "messageset": existing.messageset,
            "sequence_number": 3,
            "lang": "zu_ZA",
            "text_content": "Ke msg 3",
        }
        Message.objects.create(**message_data_zul_3)

        # Execute
        response = self.client.post('/api/v1/subscriptions/%s/send' % (
            existing.id, ), content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        d = Subscription.objects.get(id=existing.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(d.messageset.id, self.messageset.id)
        self.assertEqual(d.next_sequence_number, 1)
        self.assertEqual(d.active, True)
        self.assertEqual(d.completed, False)
        self.assertEqual(d.process_status, 1)
        subs_all = Subscription.objects.all()
        self.assertEqual(subs_all.count(), 1)
        scheds_all = Schedule.objects.all()
        self.assertEqual(scheds_all.count(), 1)
        self.assertEqual(len(responses.calls), 0)

        post_save.disconnect(fire_sub_action_if_new, sender=Subscription)

    @responses.activate
    def test_send_message_task_to_other_text(self):
        # Setup
        existing = self.make_subscription()

        # mock identity address lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/addresses/msisdn?default=True&use_communicate_through=True" % (  # noqa
                existing.identity, ),
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"address": "+2345059993333"}]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # Create message sender call
        responses.add(
            responses.POST,
            "http://seed-message-sender/api/v1/outbound/",
            json={
                "url": "http://seed-message-sender/api/v1/outbound/c7f3c839-2bf5-42d1-86b9-ccb886645fb4/",  # noqa
                "id": "c7f3c839-2bf5-42d1-86b9-ccb886645fb4",
                "version": 1,
                "to_addr": "+2345059993333",
                "to_identity": "8646b7bc-b511-4965-a90b-e1145e398703",
                "vumi_message_id": None,
                "content": "This is message 1",
                "delivered": False,
                "attempts": 0,
                "metadata": {},
                "created_at": "2016-03-24T13:43:43.614952Z",
                "updated_at": "2016-03-24T13:43:43.614921Z"
            },
            status=200, content_type='application/json'
        )

        # Create metrics call - deactivate TestSession for this
        self.session = None
        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json'
        )

        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json'
        )

        # make messages
        message_data1 = {
            "messageset": existing.messageset,
            "sequence_number": 1,
            "lang": "eng_ZA",
            "text_content": "This is message 1",
        }
        Message.objects.create(**message_data1)
        message_data2 = {
            "messageset": existing.messageset,
            "sequence_number": 2,
            "lang": "eng_ZA",
            "text_content": "This is message 2",
        }
        Message.objects.create(**message_data2)

        # Execute
        response = self.client.post('/api/v1/subscriptions/%s/send' % (
            existing.id, ), content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        d = Subscription.objects.get(id=existing.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(d.messageset.id, self.messageset.id)
        self.assertEqual(d.next_sequence_number, 2)
        self.assertEqual(d.active, True)
        self.assertEqual(d.completed, False)
        self.assertEqual(d.process_status, 0)

        # Check the request body of metric call
        metric_call = responses.calls[2]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.text.messageset_one.sum": 1.0
        })

        metric_call = responses.calls[3]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.text.sum": 1.0
        })

    @responses.activate
    def test_send_message_task_to_mother_audio(self):
        # Setup
        existing = self.make_subscription_audio()

        # mock identity address lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/addresses/msisdn?default=True&use_communicate_through=True" % (existing.identity, ),  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"address": "+2345059992222"}]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # Create message sender call
        responses.add(
            responses.POST,
            "http://seed-message-sender/api/v1/outbound/",
            json={
                "url": "http://seed-message-sender/api/v1/outbound/c7f3c839-2bf5-42d1-86b9-ccb886645fb4/",  # noqa
                "id": "c7f3c839-2bf5-42d1-86b9-ccb886645fb4",
                "version": 1,
                "to_addr": "+2345059992222",
                "to_identity": "8646b7bc-b511-4965-a90b-e1145e398703",
                "vumi_message_id": None,
                "content": None,
                "delivered": False,
                "attempts": 0,
                "metadata": {
                    'voice_speech_url': 'fakefilename.mp3'
                },
                "created_at": "2016-03-24T13:43:43.614952Z",
                "updated_at": "2016-03-24T13:43:43.614921Z"
            },
            status=200, content_type='application/json'
        )

        # Create metrics call - deactivate TestSession for this
        self.session = None
        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json'
        )

        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json'
        )

        # make binarycontent
        binarycontent_data1 = {
            "content": "fakefilename.mp3",
        }
        binarycontent1 = BinaryContent.objects.create(**binarycontent_data1)
        binarycontent_data2 = {
            "content": "fakefilename.mp3",
        }
        binarycontent2 = BinaryContent.objects.create(**binarycontent_data2)

        # make messages
        message_data1 = {
            "messageset": existing.messageset,
            "sequence_number": 1,
            "lang": "eng_ZA",
            "binary_content": binarycontent1,
        }
        Message.objects.create(**message_data1)
        message_data2 = {
            "messageset": existing.messageset,
            "sequence_number": 2,
            "lang": "eng_ZA",
            "binary_content": binarycontent2,
        }
        Message.objects.create(**message_data2)

        # Execute
        response = self.client.post('/api/v1/subscriptions/%s/send' % (
            existing.id, ), content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        d = Subscription.objects.get(id=existing.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(d.next_sequence_number, 2)
        self.assertEqual(d.active, True)
        self.assertEqual(d.completed, False)
        self.assertEqual(d.process_status, 0)

        # Check the request body of metric call
        metric_call = responses.calls[2]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.audio.messageset_two.sum": 1.0
        })

        metric_call = responses.calls[3]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.audio.sum": 1.0
        })

    @responses.activate
    def test_send_message_task_to_mother_audio_first_with_welcome(self):
        # Setup
        existing = self.make_subscription_audio_welcome()

        # mock identity address lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/addresses/msisdn?default=True&use_communicate_through=True" % (existing.identity, ),  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"address": "+2345059992222"}]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # Create message sender call
        responses.add(
            responses.POST,
            "http://seed-message-sender/api/v1/outbound/",
            json={
                "url": "http://seed-message-sender/api/v1/outbound/c7f3c839-2bf5-42d1-86b9-ccb886645fb4/",  # noqa
                "id": "c7f3c839-2bf5-42d1-86b9-ccb886645fb4",
                "version": 1,
                "to_addr": "+2345059992222",
                "to_identity": "8646b7bc-b511-4965-a90b-e1145e398703",
                "vumi_message_id": None,
                "content": None,
                "delivered": False,
                "attempts": 0,
                "metadata": {
                    'voice_speech_url': [
                        'http://example.com/welcome.mp3', 'fakefilename.mp3'
                    ]
                },
                "created_at": "2016-03-24T13:43:43.614952Z",
                "updated_at": "2016-03-24T13:43:43.614921Z"
            },
            status=200, content_type='application/json'
        )

        # Create metrics call - deactivate TestSession for this
        self.session = None
        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json'
        )

        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json'
        )

        # make binarycontent
        binarycontent_data1 = {
            "content": "fakefilename.mp3",
        }
        binarycontent1 = BinaryContent.objects.create(**binarycontent_data1)
        binarycontent_data2 = {
            "content": "fakefilename.mp3",
        }
        binarycontent2 = BinaryContent.objects.create(**binarycontent_data2)

        # make messages
        message_data1 = {
            "messageset": existing.messageset,
            "sequence_number": 1,
            "lang": "eng_ZA",
            "binary_content": binarycontent1,
        }
        Message.objects.create(**message_data1)
        message_data2 = {
            "messageset": existing.messageset,
            "sequence_number": 2,
            "lang": "eng_ZA",
            "binary_content": binarycontent2,
        }
        Message.objects.create(**message_data2)

        # Execute
        response = self.client.post('/api/v1/subscriptions/%s/send' % (
            existing.id, ), content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        d = Subscription.objects.get(id=existing.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(d.next_sequence_number, 2)
        self.assertEqual(d.active, True)
        self.assertEqual(d.completed, False)
        self.assertEqual(d.process_status, 0)
        self.assertEqual(d.metadata["prepend_next_delivery"], None)

        # Check the request body of metric call
        metric_call = responses.calls[2]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.audio.messageset_two.sum": 1.0
        })

    @responses.activate
    def test_send_message_task_to_mother_text_no_content(self):
        post_save.connect(fire_sub_action_if_new, sender=Subscription)
        # mock schedule sending
        responses.add(
            responses.POST,
            "http://seed-scheduler/api/v1/schedule/",
            json={
                "id": "1234"
            },
            status=201, content_type='application/json'
        )
        # Setup
        existing = self.make_subscription()

        # Precheck
        subs_all = Subscription.objects.all()
        self.assertEqual(subs_all.count(), 1)
        scheds_all = Schedule.objects.all()
        self.assertEqual(scheds_all.count(), 1)

        # Execute
        response = self.client.post('/api/v1/subscriptions/%s/send' % (
            existing.id, ), content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        d = Subscription.objects.get(id=existing.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(d.messageset.id, self.messageset.id)
        self.assertEqual(d.next_sequence_number, 1)
        self.assertEqual(d.active, True)
        self.assertEqual(d.completed, False)
        # Ensure the the process_status doesnt get left in processing state
        self.assertEqual(d.process_status, 0)
        subs_all = Subscription.objects.all()
        self.assertEqual(subs_all.count(), 1)
        scheds_all = Schedule.objects.all()
        self.assertEqual(scheds_all.count(), 1)
        self.assertEqual(len(responses.calls), 1)

        post_save.disconnect(fire_sub_action_if_new, sender=Subscription)

    @responses.activate
    def test_retry_send_message_task_to_mother_text(self):
        post_save.connect(fire_sub_action_if_new, sender=Subscription)
        # mock schedule sending
        responses.add(
            responses.POST,
            "http://seed-scheduler/api/v1/schedule/",
            json={
                "id": "1234"
            },
            status=201, content_type='application/json'
        )
        # Setup
        existing = self.make_subscription()

        # Precheck
        subs_all = Subscription.objects.all()
        self.assertEqual(subs_all.count(), 1)
        scheds_all = Schedule.objects.all()
        self.assertEqual(scheds_all.count(), 1)

        # mock identity address lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/addresses/msisdn?default=True&use_communicate_through=True" % (existing.identity, ),  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"address": "+2345059992222"}]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # Create message sender call
        responses.add(
            responses.POST,
            "http://seed-message-sender/api/v1/outbound/",
            json={
                "url": "http://seed-message-sender/api/v1/outbound/c7f3c839-2bf5-42d1-86b9-ccb886645fb4/",  # noqa
                "id": "c7f3c839-2bf5-42d1-86b9-ccb886645fb4",
                "version": 1,
                "to_addr": "+2345059992222",
                "to_identity": "8646b7bc-b511-4965-a90b-e1145e398703",
                "vumi_message_id": None,
                "content": "This is message 1",
                "delivered": False,
                "attempts": 0,
                "metadata": {},
                "created_at": "2016-03-24T13:43:43.614952Z",
                "updated_at": "2016-03-24T13:43:43.614921Z"
            },
            status=200, content_type='application/json'
        )

        # Create metrics call - deactivate TestSession for this
        self.session = None
        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json; charset=utf-8'
        )

        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json; charset=utf-8'
        )

        # make messages
        message_data_eng_1 = {
            "messageset": existing.messageset,
            "sequence_number": 1,
            "lang": "eng_ZA",
            "text_content": "This is message 1",
        }
        Message.objects.create(**message_data_eng_1)
        message_data_eng_2 = {
            "messageset": existing.messageset,
            "sequence_number": 2,
            "lang": "eng_ZA",
            "text_content": "This is message 2",
        }
        Message.objects.create(**message_data_eng_2)
        message_data_eng_3 = {
            "messageset": existing.messageset,
            "sequence_number": 3,
            "lang": "eng_ZA",
            "text_content": "This is message 3",
        }
        Message.objects.create(**message_data_eng_3)
        message_data_zul_1 = {
            "messageset": existing.messageset,
            "sequence_number": 1,
            "lang": "zu_ZA",
            "text_content": "Ke msg 1",
        }
        Message.objects.create(**message_data_zul_1)
        message_data_zul_2 = {
            "messageset": existing.messageset,
            "sequence_number": 2,
            "lang": "zu_ZA",
            "text_content": "Ke msg 2",
        }
        Message.objects.create(**message_data_zul_2)
        message_data_zul_3 = {
            "messageset": existing.messageset,
            "sequence_number": 3,
            "lang": "zu_ZA",
            "text_content": "Ke msg 3",
        }
        Message.objects.create(**message_data_zul_3)

        # Execute
        SubscriptionSendFailure.objects.create(
            subscription=existing,
            task_id=uuid4(),
            initiated_at=timezone.now(),
            reason='Error')
        # Requeue
        tasks.requeue_failed_tasks()
        d = Subscription.objects.get(id=existing.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(d.messageset.id, self.messageset.id)
        self.assertEqual(d.next_sequence_number, 2)
        self.assertEqual(d.active, True)
        self.assertEqual(d.completed, False)
        self.assertEqual(d.process_status, 0)
        subs_all = Subscription.objects.all()
        self.assertEqual(subs_all.count(), 1)
        scheds_all = Schedule.objects.all()
        self.assertEqual(scheds_all.count(), 1)
        self.assertEqual(len(responses.calls), 5)

        # Check the request body of metric call
        metric_call = responses.calls[3]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.text.messageset_one.sum": 1.0
        })

        metric_call = responses.calls[4]
        self.assertEqual(json.loads(metric_call.request.body), {
            "message.text.sum": 1.0
        })

        # Check the message_count / set_max count
        message_count = existing.messageset.messages.filter(
            lang=existing.lang).count()
        self.assertEqual(message_count, 3)

        post_save.disconnect(fire_sub_action_if_new, sender=Subscription)

    @override_settings(USE_SSL=True)
    def test_make_absolute_url(self):
        self.assertEqual(
            tasks.make_absolute_url('foo'),
            'https://example.com/foo')
        self.assertEqual(
            tasks.make_absolute_url('/foo'),
            'https://example.com/foo')

    @override_settings(USE_SSL=False)
    def test_make_absolute_url_ssl(self):
        self.assertEqual(
            tasks.make_absolute_url('foo'),
            'http://example.com/foo')
        self.assertEqual(
            tasks.make_absolute_url('/foo'),
            'http://example.com/foo')


class TestDeactivateSubscription(AuthenticatedAPITestCase):

    @responses.activate
    def test_deactivation_deactivates_schedule(self):
        # Setup
        post_save.connect(disable_schedule_if_deactivated, sender=Subscription)
        schedule_id = "6455245a-028b-4fa1-82fc-6b639c4e7710"
        sub = self.make_subscription()
        sub.metadata["scheduler_schedule_id"] = schedule_id
        sub.save()

        # mock schedule update
        responses.add(
            responses.PATCH,
            "http://seed-scheduler/api/v1/schedule/%s/" % schedule_id,
            json.dumps({"enabled": False}),
            status=200, content_type='application/json')

        # Execute
        sub.active = False
        sub.save()
        # Check
        self.assertEqual(len(responses.calls), 1)
        post_save.disconnect(disable_schedule_if_deactivated,
                             sender=Subscription)


class TestMetricsAPI(AuthenticatedAPITestCase):

    def test_metrics_read(self):
        # Setup
        self.make_subscription()
        # Execute
        response = self.client.get('/api/metrics/',
                                   content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(
            sorted(response.data["metrics_available"]), sorted([
                'subscriptions.created.sum',
                'subscriptions.send_next_message_errored.sum',
                'subscriptions.active.last',
                'subscriptions.created.last',
                'subscriptions.broken.last',
                'subscriptions.completed.last',
                'subscriptions.send.estimate.0.last',
                'subscriptions.send.estimate.1.last',
                'subscriptions.send.estimate.2.last',
                'subscriptions.send.estimate.3.last',
                'subscriptions.send.estimate.4.last',
                'subscriptions.send.estimate.5.last',
                'subscriptions.send.estimate.6.last',
                'subscriptions.send_next_message.connection_error.sum',
                'subscriptions.send_next_message.http_error.400.sum',
                'subscriptions.send_next_message.http_error.401.sum',
                'subscriptions.send_next_message.http_error.403.sum',
                'subscriptions.send_next_message.http_error.404.sum',
                'subscriptions.send_next_message.http_error.500.sum',
                'subscriptions.send_next_message.timeout.sum',
                'subscriptions.messageset_one.active.last',
                'subscriptions.messageset_two.active.last',
                'subscriptions.message_set.messageset_one.sum',
                'subscriptions.message_set.messageset_two.sum',
                'subscriptions.message_set.messageset_one.total.last',
                'subscriptions.message_set.messageset_two.total.last',
                'subscriptions.language.eng_ZA.sum',
                'subscriptions.language.eng_ZA.total.last',
                'subscriptions.message_format.text.sum',
                'subscriptions.message_format.text.total.last',
                'subscriptions.message_format.audio.sum',
                'subscriptions.message_format.audio.total.last',
                'message.text.messageset_one.sum',
                'message.audio.messageset_one.sum',
                'message.text.messageset_two.sum',
                'message.audio.messageset_two.sum',
                'message.text.sum',
                'message.audio.sum',
            ])
        )

    @responses.activate
    def test_post_metrics(self):
        # Setup
        # deactivate Testsession for this test
        self.session = None
        responses.add(responses.POST,
                      "http://metrics-url/metrics/",
                      json={"foo": "bar"},
                      status=200, content_type='application/json')
        # Execute
        response = self.client.post('/api/metrics/',
                                    content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["scheduled_metrics_initiated"], True)


class TestMetrics(AuthenticatedAPITestCase):

    def check_request(
            self, request, method, params=None, data=None, headers=None):
        self.assertEqual(request.method, method)
        if params is not None:
            url = urlparse.urlparse(request.url)
            qs = urlparse.parse_qsl(url.query)
            self.assertEqual(dict(qs), params)
        if headers is not None:
            for key, value in headers.items():
                self.assertEqual(request.headers[key], value)
        if data is None:
            self.assertEqual(request.body, None)
        else:
            self.assertEqual(json.loads(request.body), data)

    @responses.activate
    def test_direct_fire(self):
        """
        When calling the `fire_metric` task, it should send the specified
        metric to the metrics API.
        """
        # Execute
        result = fire_metric.apply_async(kwargs={
            "metric_name": 'foo.last',
            "metric_value": 1,
        })
        # Check
        request = responses.calls[-1].request
        self.check_request(
            request, 'POST',
            data={"foo.last": 1.0}
        )
        self.assertEqual(result.get(),
                         "Fired metric <foo.last> with value <1.0>")

    @responses.activate
    def test_created_metrics(self):
        """
        When a new subscription is created, the correct metric should be sent
        to the metrics API.
        """
        # reconnect metric post_save hook
        post_save.connect(fire_metrics_if_new, sender=Subscription)

        # Execute
        self.make_subscription()

        # Check
        request = responses.calls[-1].request
        self.check_request(
            request, 'POST',
            data={"subscriptions.created.sum": 1.0}
        )
        # remove post_save hooks to prevent teardown errors
        post_save.disconnect(fire_metrics_if_new, sender=Subscription)

    @responses.activate
    def test_multiple_created_metrics(self):
        # Setup
        # deactivate Testsession for this test
        self.session = None
        # reconnect metric post_save hook
        post_save.connect(fire_metrics_if_new, sender=Subscription)
        # add metric post response
        responses.add(responses.POST,
                      "http://metrics-url/metrics/",
                      json={"foo": "bar"},
                      status=200, content_type='application/json')

        # Execute
        self.make_subscription()
        self.make_subscription()

        # Check
        self.assertEqual(len(responses.calls), 2)
        # remove post_save hooks to prevent teardown errors
        post_save.disconnect(fire_metrics_if_new, sender=Subscription)

    @responses.activate
    def test_scheduled_metrics(self):
        # Setup
        # deactivate Testsession for this test
        self.session = None
        # add metric post response
        responses.add(responses.POST,
                      "http://metrics-url/metrics/",
                      json={"foo": "bar"},
                      status=200, content_type='application/json')

        # Execute
        result = scheduled_metrics.apply_async()
        # Check
        self.assertEqual(result.get(), "7 Scheduled metrics launched")
        # fire_messagesets_tasks fires two metrics, therefore extra call
        total = 7
        # fire_week_estimate_last can fire up to 7 extra metrics based on the
        # day of the week
        total = total + (7 - datetime.today().weekday())
        self.assertEqual(len(responses.calls), total)

    @responses.activate
    def test_metric_per_message_set_sum(self):
        """
        When a new subscription is created, a sum metric should be fired for
        that subscription's message set.
        """
        # deactivate Testsession for this test
        self.session = None
        # add metric post response
        responses.add(responses.POST,
                      "http://metrics-url/metrics/",
                      json={"foo": "bar"},
                      status=200, content_type='application/json')
        post_save.connect(fire_metric_per_message_set, sender=Subscription)

        self.make_subscription()

        [sum_call, _] = responses.calls
        self.assertEqual(json.loads(sum_call.request.body), {
            "subscriptions.message_set.messageset_one.sum": 1.0
        })

        post_save.disconnect(fire_metric_per_message_set, sender=Subscription)

    @responses.activate
    def test_metric_per_message_set_last(self):
        """
        When a new subscription is created, a last metric should be fired for
        the total amount of subscriptions for that message set.
        """
        # deactivate Testsession for this test
        self.session = None
        # add metric post response
        responses.add(responses.POST,
                      "http://metrics-url/metrics/",
                      json={"foo": "bar"},
                      status=200, content_type='application/json')
        post_save.connect(fire_metric_per_message_set, sender=Subscription)

        self.make_subscription()
        self.make_subscription()

        [_, last_call1, _, last_call2] = responses.calls
        self.assertEqual(json.loads(last_call1.request.body), {
            "subscriptions.message_set.messageset_one.total.last": 1.0
        })
        self.assertEqual(json.loads(last_call2.request.body), {
            "subscriptions.message_set.messageset_one.total.last": 2.0
        })

        post_save.disconnect(fire_metric_per_message_set, sender=Subscription)

    @responses.activate
    def test_metric_per_language_sum(self):
        """
        When a new subscription is created, a sum metric should be fired for
        that subscription's language.
        """
        # deactivate Testsession for this test
        self.session = None
        # add metric post response
        responses.add(responses.POST,
                      "http://metrics-url/metrics/",
                      json={"foo": "bar"},
                      status=200, content_type='application/json')
        post_save.connect(fire_metric_per_lang, sender=Subscription)

        self.make_subscription()

        [sum_call, _] = responses.calls
        self.assertEqual(json.loads(sum_call.request.body), {
            "subscriptions.language.eng_ZA.sum": 1.0
        })

        post_save.disconnect(fire_metric_per_lang, sender=Subscription)

    @responses.activate
    def test_metric_per_language_last(self):
        """
        When a new subscription is created, a last metric should be fired for
        the total amount of subscriptions for that language.
        """
        # deactivate Testsession for this test
        self.session = None
        # add metric post response
        responses.add(responses.POST,
                      "http://metrics-url/metrics/",
                      json={"foo": "bar"},
                      status=200, content_type='application/json')
        post_save.connect(fire_metric_per_lang, sender=Subscription)

        self.make_subscription()
        self.make_subscription()

        [_, last_call1, _, last_call2] = responses.calls
        self.assertEqual(json.loads(last_call1.request.body), {
            "subscriptions.language.eng_ZA.total.last": 1.0
        })
        self.assertEqual(json.loads(last_call2.request.body), {
            "subscriptions.language.eng_ZA.total.last": 2.0
        })

        post_save.disconnect(fire_metric_per_lang, sender=Subscription)

    @responses.activate
    def test_metric_per_message_format_sum(self):
        """
        When a new subscription is created, a sum metric should be fired for
        that subscription's message format.
        """
        # deactivate Testsession for this test
        self.session = None
        # add metric post response
        responses.add(responses.POST,
                      "http://metrics-url/metrics/",
                      json={"foo": "bar"},
                      status=200, content_type='application/json')
        post_save.connect(fire_metric_per_message_format, sender=Subscription)

        self.make_subscription()

        [sum_call, _] = responses.calls
        self.assertEqual(json.loads(sum_call.request.body), {
            "subscriptions.message_format.text.sum": 1.0
        })

        post_save.disconnect(fire_metric_per_message_format,
                             sender=Subscription)

    @responses.activate
    def test_metric_per_message_format_last(self):
        """
        When a new subscription is created, a last metric should be fired for
        the total amount of subscriptions for that message format.
        """
        # deactivate Testsession for this test
        self.session = None
        # add metric post response
        responses.add(responses.POST,
                      "http://metrics-url/metrics/",
                      json={"foo": "bar"},
                      status=200, content_type='application/json')
        post_save.connect(fire_metric_per_message_format, sender=Subscription)

        self.make_subscription()
        self.make_subscription_audio()
        self.make_subscription()

        [_, last_call1, _, last_call2, _, last_call3] = responses.calls
        self.assertEqual(json.loads(last_call1.request.body), {
            "subscriptions.message_format.text.total.last": 1.0
        })
        self.assertEqual(json.loads(last_call2.request.body), {
            "subscriptions.message_format.audio.total.last": 1.0
        })
        self.assertEqual(json.loads(last_call3.request.body), {
            "subscriptions.message_format.text.total.last": 2.0
        })

        post_save.disconnect(fire_metric_per_message_format,
                             sender=Subscription)

    @responses.activate
    def test_fire_active_last(self):
        """
        Ensure that the subscriptions.active.last metric gets called with the
        correct amount of active subscriptions.
        """
        # make two active and one inactive subscription
        self.make_subscription()
        self.make_subscription()
        sub = self.make_subscription()
        sub.active = False
        sub.completed = True
        sub.save()

        # Execute
        result = tasks.fire_active_last.apply_async()

        # Check
        self.assertEqual(
            result.get().get(),
            "Fired metric <subscriptions.active.last> with value <2.0>"
        )
        self.check_request(
            responses.calls[-1].request, 'POST',
            data={"subscriptions.active.last": 2.0}
        )

    @responses.activate
    def test_fire_created_last(self):
        """
        Ensure that the subscriptions.created.last metric gets called with the
        correct amount of created subscriptions.
        """
        # make two active and one inactive subscription
        self.make_subscription()
        self.make_subscription()
        sub = self.make_subscription()
        sub.active = False
        sub.completed = True
        sub.save()

        # Execute
        result = tasks.fire_created_last.apply_async()

        # Check
        self.assertEqual(
            result.get().get(),
            "Fired metric <subscriptions.created.last> with value <3.0>"
        )
        request = responses.calls[-1].request
        self.check_request(
            request, 'POST',
            data={"subscriptions.created.last": 3.0}
        )

    @responses.activate
    def test_fire_broken_last(self):
        """
        Ensure that the subscriptions.broken.last metric gets called with the
        correct amount of broken subscriptions.
        """
        # make two healthy subscriptions
        self.make_subscription()
        sub = self.make_subscription()
        sub.process_status = 1
        sub.save()
        # make two broken subscriptions
        sub = self.make_subscription()
        sub.process_status = -1
        sub.save()
        sub = self.make_subscription()
        sub.messageset = self.messageset_audio
        sub.process_status = -1
        sub.save()

        # Execute
        result = tasks.fire_broken_last.apply_async()

        # Check
        self.assertEqual(
            result.get().get(),
            "Fired metric <subscriptions.broken.last> with value <2.0>"
        )
        request = responses.calls[-1].request
        self.check_request(
            request, 'POST',
            data={"subscriptions.broken.last": 2.0}
        )

    @responses.activate
    def test_fire_completed_last(self):
        """
        Ensure that the subscriptions.completed.last metric gets called with
        the correct amount of completed subscriptions.
        """
        # make two incomplete and one complete subscription
        self.make_subscription()
        self.make_subscription()
        sub = self.make_subscription()
        sub.completed = True
        sub.save()

        # Execute
        result = tasks.fire_completed_last.apply_async()

        # Check
        self.assertEqual(
            result.get().get(),
            "Fired metric <subscriptions.completed.last> with value <1.0>"
        )
        self.check_request(
            responses.calls[-1].request, 'POST',
            data={"subscriptions.completed.last": 1.0}
        )

    @responses.activate
    def test_fire_incomplete_last(self):
        """
        Ensure that the subscriptions.incomplete.last metric gets called with
        the correct amount of incomplete subscriptions.
        """
        # make two incomplete and one complete subscription
        self.make_subscription()
        self.make_subscription()
        sub = self.make_subscription()
        sub.completed = True
        sub.save()

        # Execute
        result = tasks.fire_incomplete_last.apply_async()

        # Check
        self.assertEqual(
            result.get().get(),
            "Fired metric <subscriptions.incomplete.last> with value <2.0>"
        )
        self.check_request(
            responses.calls[-1].request, 'POST',
            data={"subscriptions.incomplete.last": 2.0}
        )

    @responses.activate
    def test_messagesets_tasks(self):
        """
        Ensure that the `fire_messagesets_tasks` fires the correct amount of
        messageset metric tasks.
        """
        self.make_subscription()

        # Execute
        result = tasks.fire_messagesets_tasks.apply_async()

        # Check
        self.assertEqual(
            result.get(),
            "2 MessageSet metrics launched"
        )

    @responses.activate
    def test_mesageset_last(self):
        """
        Ensure that the subscriptions.<messageset>.active.last metric gets
        called with the correct amount of active subscriptions for that
        messageset.
        """
        self.make_subscription()

        # Execute
        result = tasks.fire_messageset_last.apply_async(kwargs={
            "msgset_id": self.messageset.id,
            "short_name": self.messageset.short_name
        })

        # Check
        self.assertEqual(
            result.get().get(),
            "Fired metric <subscriptions.messageset_one.active.last> with "
            "value <1.0>"
        )
        self.check_request(
            responses.calls[-1].request, 'POST',
            data={"subscriptions.messageset_one.active.last": 1.0}
        )

    @responses.activate
    def test_fire_week_estimate_last(self):
        """
        Ensure that the fire_week_estimate_last task sends the correct amount
        of metrics to the metrics API, which should be the amount of days left
        in this week.
        """
        # Setup
        self.make_subscription()

        # Execute
        tasks.fire_week_estimate_last.apply_async()

        # Check
        days_left_in_week = 7 - datetime.now().weekday()
        self.assertEqual(len(responses.calls), days_left_in_week)


class TestUserCreation(AuthenticatedAPITestCase):

    def test_create_user_and_token(self):
        # Setup
        user_request = {"email": "test@example.org"}
        # Execute
        request = self.adminclient.post('/api/v1/user/token/', user_request)
        token = request.json().get('token', None)
        # Check
        self.assertIsNotNone(
            token, "Could not receive authentication token on post.")
        self.assertEqual(
            request.status_code, 201,
            "Status code on /api/v1/user/token/ was %s (should be 201)."
            % request.status_code)

    def test_create_user_and_token_fail_nonadmin(self):
        # Setup
        user_request = {"email": "test@example.org"}
        # Execute
        request = self.client.post('/api/v1/user/token/', user_request)
        error = request.json().get('detail', None)
        # Check
        self.assertIsNotNone(
            error, "Could not receive error on post.")
        self.assertEqual(
            error, "You do not have permission to perform this action.",
            "Error message was unexpected: %s."
            % error)

    def test_create_user_and_token_not_created(self):
        # Setup
        user_request = {"email": "test@example.org"}
        # Execute
        request = self.adminclient.post('/api/v1/user/token/', user_request)
        token = request.json().get('token', None)
        # And again, to get the same token
        request2 = self.adminclient.post('/api/v1/user/token/', user_request)
        token2 = request2.json().get('token', None)

        # Check
        self.assertEqual(
            token, token2,
            "Tokens are not equal, should be the same as not recreated.")

    def test_create_user_new_token_nonadmin(self):
        # Setup
        user_request = {"email": "test@example.org"}
        request = self.adminclient.post('/api/v1/user/token/', user_request)
        token = request.json().get('token', None)
        cleanclient = APIClient()
        cleanclient.credentials(HTTP_AUTHORIZATION='Token %s' % token)
        # Execute
        request = cleanclient.post('/api/v1/user/token/', user_request)
        error = request.json().get('detail', None)
        # Check
        # new user should not be admin
        self.assertIsNotNone(
            error, "Could not receive error on post.")
        self.assertEqual(
            error, "You do not have permission to perform this action.",
            "Error message was unexpected: %s."
            % error)


class TestHealthcheckAPI(AuthenticatedAPITestCase):

    def test_healthcheck_read(self):
        # Setup
        # Execute
        response = self.client.get('/api/health/',
                                   content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["up"], True)
        self.assertEqual(response.data["result"]["database"], "Accessible")


@override_settings(
    SCHEDULER_URL='http://scheduler/',
    SCHEDULER_API_TOKEN='scheduler_token'
)
class TestRemoveDuplicateSubscriptions(AuthenticatedAPITestCase):

    def test_noop(self):
        stdout, stderr = StringIO(), StringIO()
        self.make_subscription()
        call_command('remove_duplicate_subscriptions',
                     stdout=stdout, stderr=stderr)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(
            stdout.getvalue().strip(),
            'Removed 0 duplicate subscriptions.')

    def test_duplicate_removal_dry_run(self):
        sub1, sub2, sub3 = [self.make_subscription() for i in range(3)]

        stdout, stderr = StringIO(), StringIO()

        call_command('remove_duplicate_subscriptions',
                     stdout=stdout, stderr=stderr)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(
            set(stdout.getvalue().strip().split('\n')),
            set([
                'Not removing %s, use --fix to actually remove.' % (sub2,),
                'Not removing %s, use --fix to actually remove.' % (sub3,),
                'Removed 2 duplicate subscriptions.',
            ]))
        self.assertEqual(Subscription.objects.count(), 3)

    @responses.activate
    def test_duplicate_removal(self):

        # Canary, if this is called something's going wrong
        responses.add(
            responses.DELETE, 'http://scheduler/schedule/schedule-id-1/',
            body=HTTPError('This should not have been called'))

        responses.add(
            responses.DELETE,
            'http://scheduler/schedule/schedule-id-2/')
        responses.add(
            responses.DELETE,
            'http://scheduler/schedule/schedule-id-3/')

        sub1, sub2, sub3 = [self.make_subscription() for i in range(3)]

        sub1.metadata["scheduler_schedule_id"] = "schedule-id-1"
        sub1.save()

        sub2.metadata["scheduler_schedule_id"] = "schedule-id-2"
        sub2.save()

        sub3.metadata["scheduler_schedule_id"] = "schedule-id-3"
        sub3.save()

        stdout, stderr = StringIO(), StringIO()

        call_command('remove_duplicate_subscriptions', '--fix',
                     stdout=stdout, stderr=stderr)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(
            stdout.getvalue().strip(),
            'Removed 2 duplicate subscriptions.')
        self.assertEqual(Subscription.objects.count(), 1)

    @responses.activate
    def test_retain_duplicates_outside_time_delta(self):

        # Canary, if this is called something's going wrong
        responses.add(
            responses.DELETE, 'http://scheduler/schedule/schedule-id-3/',
            body=HTTPError('This should not have been called'))

        responses.add(
            responses.DELETE, 'http://scheduler/schedule/schedule-id-2/')

        sub1, sub2, sub3 = [self.make_subscription() for i in range(3)]
        sub1.metadata['scheduler_schedule_id'] = 'schedule-id-1'
        sub1.save()

        sub2.metadata['scheduler_schedule_id'] = 'schedule-id-2'
        sub2.save()

        sub3.created_at = sub1.created_at + timedelta(seconds=22)
        sub3.metadata['scheduler_schedule_id'] = 'schedule-id-3'
        sub3.save()

        stdout, stderr = StringIO(), StringIO()

        call_command('remove_duplicate_subscriptions', '--fix',
                     '--time-delta', '20',
                     stdout=stdout, stderr=stderr)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(
            stdout.getvalue().strip(),
            'Removed 1 duplicate subscriptions.')
        self.assertEqual(Subscription.objects.count(), 2)

    @responses.activate
    def test_duplicate_removal_without_scheduler(self):
        sub1, sub2, sub3 = [self.make_subscription() for i in range(3)]

        # only have one of the subs a have a schedule id for some reason
        sub1.metadata["scheduler_schedule_id"] = "schedule-id-1"
        sub1.save()

        stdout, stderr = StringIO(), StringIO()

        call_command('remove_duplicate_subscriptions', '--fix',
                     stdout=stdout, stderr=stderr)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(
            set(stdout.getvalue().strip().split('\n')),
            set([
                'Subscription %s has no scheduler_id.' % (sub2,),
                'Subscription %s has no scheduler_id.' % (sub3,),
                'Removed 2 duplicate subscriptions.',
            ]))
        self.assertEqual(Subscription.objects.count(), 1)


class TestSubscription(AuthenticatedAPITestCase):

    def make_schedule(self):
        schedule_data = {
            'hour': '8',
            'minute': '0',
            'day_of_week': '2, 4'
        }
        return Schedule.objects.create(**schedule_data)

    def make_subscription(self):
        post_data = {
            "identity": "8646b7bc-b511-4965-a90b-e1145e398703",
            "messageset": self.messageset,
            "next_sequence_number": 1,
            "lang": "eng_ZA",
            "active": True,
            "completed": False,
            "schedule": self.schedule,
            "process_status": 0,
            "metadata": {
                "source": "RapidProVoice"
            }
        }
        return Subscription.objects.create(**post_data)

    def make_messageset_content(self, messageset, count=3):

        for msg in range(0, count):
            message_data_eng = {
                "messageset": messageset,
                "sequence_number": msg + 1,
                "lang": "eng_ZA",
                "text_content": "This is message %s" % (msg + 1),
            }
            Message.objects.create(**message_data_eng)

    def test_get_expected_next_sequence_number(self):
        self.make_messageset_content(self.messageset)
        start = datetime(2016, 11, 1, 0, 0, tzinfo=pytz.UTC)
        end = datetime(2016, 11, 7, 22, 0, tzinfo=pytz.UTC)
        subscription = self.make_subscription()
        subscription.created_at = start
        est, comp = subscription.get_expected_next_sequence_number(end)
        self.assertEqual(est, 3)
        self.assertEqual(comp, False)

        end = datetime(2016, 11, 10, 22, 0, tzinfo=pytz.UTC)
        est, comp = subscription.get_expected_next_sequence_number(end)
        self.assertEqual(est, 3)
        self.assertEqual(comp, True)

        start = datetime(2016, 11, 1, 0, 0, tzinfo=pytz.UTC)
        end = datetime(2016, 11, 1, 7, 0, tzinfo=pytz.UTC)
        subscription = self.make_subscription()
        subscription.created_at = start
        est, comp = subscription.get_expected_next_sequence_number(end)
        self.assertEqual(est, 1)
        self.assertEqual(comp, False)

    def test_mark_as_complete(self):
        subscription = self.make_subscription()
        subscription.mark_as_complete()
        self.assertEqual(subscription.completed, True)
        self.assertEqual(subscription.process_status, 2)
        self.assertEqual(subscription.active, False)

    def test_has_next_sequence_number(self):
        self.make_messageset_content(self.messageset)
        subscription = self.make_subscription()
        self.assertEqual(subscription.has_next_sequence_number, True)

        subscription.next_sequence_number = 3
        self.assertEqual(subscription.has_next_sequence_number, False)

    def test_fast_foward_incomplete(self):
        self.make_messageset_content(self.messageset)
        subscription = self.make_subscription()
        end = datetime(2016, 11, 4, 10, 0, tzinfo=pytz.UTC)
        subscription.created_at = datetime(2016, 11, 1, 0, 0, tzinfo=pytz.UTC)
        subscription.save()
        complete = subscription.fast_forward(end)
        self.assertEqual(complete, False)
        self.assertEqual(subscription.completed, False)
        self.assertEqual(subscription.next_sequence_number, 3)
        self.assertEqual(subscription.active, True)

    def test_fast_foward_complete_with_initial(self):
        self.make_messageset_content(self.messageset)
        subscription = self.make_subscription()
        end = datetime(2016, 11, 4, 10, 0, tzinfo=pytz.UTC)
        subscription.created_at = datetime(2016, 11, 1, 0, 0, tzinfo=pytz.UTC)
        subscription.initial_sequence_number = 2
        subscription.save()
        complete = subscription.fast_forward(end)
        self.assertEqual(complete, True)
        self.assertEqual(subscription.completed, True)
        self.assertEqual(subscription.process_status, 2)
        self.assertEqual(subscription.active, False)

    def test_fast_foward_incomplete_with_initial(self):
        self.make_messageset_content(self.messageset, 6)
        subscription = self.make_subscription()
        end = datetime(2016, 11, 8, 10, 0, tzinfo=pytz.UTC)
        subscription.created_at = datetime(2016, 11, 1, 0, 0, tzinfo=pytz.UTC)
        subscription.initial_sequence_number = 2
        subscription.save()
        complete = subscription.fast_forward(end)
        self.assertEqual(complete, False)
        self.assertEqual(subscription.completed, False)
        self.assertEqual(subscription.next_sequence_number, 5)
        self.assertEqual(subscription.active, True)

    def test_fast_foward_complete(self):
        self.make_messageset_content(self.messageset)
        subscription = self.make_subscription()
        end = datetime(2016, 11, 30, 7, 0, tzinfo=pytz.UTC)
        subscription.created_at = datetime(2016, 11, 1, 0, 0, tzinfo=pytz.UTC)
        subscription.save()
        complete = subscription.fast_forward(end)
        self.assertEqual(complete, True)
        self.assertEqual(subscription.completed, True)
        self.assertEqual(subscription.process_status, 2)
        self.assertEqual(subscription.active, False)

    def test_fast_foward_lifecycle_complete(self):
        messageset_data = {
            'short_name': 'messageset_pre',
            'notes': None,
            'next_set': self.messageset,
            'default_schedule': self.schedule,
            'content_type': 'text'
        }
        first_ms = MessageSet.objects.create(**messageset_data)
        self.make_messageset_content(first_ms)
        self.make_messageset_content(self.messageset)

        subscription = self.make_subscription()
        end = datetime(2016, 11, 30, 7, 0, tzinfo=pytz.UTC)
        subscription.created_at = datetime(2016, 11, 1, 0, 0, tzinfo=pytz.UTC)
        subscription.messageset = first_ms
        subscription.save()

        result = Subscription.fast_forward_lifecycle(subscription, end)
        self.assertEqual(len(result), 2)
        sub1 = result[0]
        sub2 = result[1]
        self.assertEqual(sub1.completed, True)
        self.assertEqual(sub2.completed, True)
        self.assertEqual(
            sub2.created_at,
            datetime(2016, 11, 8, 8, 1, tzinfo=pytz.UTC))

    def test_fast_foward_lifecycle_incomplete(self):
        messageset_data = {
            'short_name': 'messageset_pre',
            'notes': None,
            'next_set': self.messageset,
            'default_schedule': self.schedule,
            'content_type': 'text'
        }
        first_ms = MessageSet.objects.create(**messageset_data)
        self.make_messageset_content(first_ms)
        self.make_messageset_content(self.messageset)

        subscription = self.make_subscription()
        end = datetime(2016, 11, 11, 7, 0, tzinfo=pytz.UTC)
        subscription.created_at = datetime(2016, 11, 1, 0, 0, tzinfo=pytz.UTC)
        subscription.messageset = first_ms
        subscription.save()

        result = Subscription.fast_forward_lifecycle(subscription, end)
        self.assertEqual(len(result), 2)
        sub1 = result[0]
        sub2 = result[1]
        self.assertEqual(sub1.completed, True)
        self.assertEqual(sub2.completed, False)
        self.assertEqual(
            sub2.created_at,
            datetime(2016, 11, 8, 8, 1, tzinfo=pytz.UTC))

    def test_fast_foward_lifecycle_complete_with_initial(self):
        messageset_data = {
            'short_name': 'messageset_pre',
            'notes': None,
            'next_set': self.messageset,
            'default_schedule': self.schedule,
            'content_type': 'text'
        }
        first_ms = MessageSet.objects.create(**messageset_data)
        self.make_messageset_content(first_ms)
        self.make_messageset_content(self.messageset)

        subscription = self.make_subscription()
        end = datetime(2016, 11, 30, 7, 0, tzinfo=pytz.UTC)
        subscription.created_at = datetime(2016, 11, 1, 0, 0, tzinfo=pytz.UTC)
        subscription.messageset = first_ms
        subscription.initial_sequence_number = 2
        subscription.save()

        result = Subscription.fast_forward_lifecycle(subscription, end)
        self.assertEqual(len(result), 2)
        sub1 = result[0]
        sub2 = result[1]
        self.assertEqual(sub1.completed, True)
        self.assertEqual(sub2.completed, True)
        self.assertEqual(
            sub2.created_at,
            datetime(2016, 11, 3, 8, 1, tzinfo=pytz.UTC))

    def test_fast_foward_lifecycle_incomplete_with_initial(self):
        messageset_data = {
            'short_name': 'messageset_pre',
            'notes': None,
            'next_set': self.messageset,
            'default_schedule': self.schedule,
            'content_type': 'text'
        }
        first_ms = MessageSet.objects.create(**messageset_data)
        self.make_messageset_content(first_ms)
        self.make_messageset_content(self.messageset)

        subscription = self.make_subscription()
        end = datetime(2016, 11, 11, 7, 0, tzinfo=pytz.UTC)
        subscription.created_at = datetime(2016, 11, 1, 0, 0, tzinfo=pytz.UTC)
        subscription.messageset = first_ms
        subscription.initial_sequence_number = 2
        subscription.save()

        result = Subscription.fast_forward_lifecycle(subscription, end)
        self.assertEqual(len(result), 2)
        sub1 = result[0]
        sub2 = result[1]
        self.assertEqual(sub1.completed, True)
        self.assertEqual(sub2.completed, False)
        self.assertEqual(
            sub2.created_at,
            datetime(2016, 11, 3, 8, 1, tzinfo=pytz.UTC))


class TestFixSubscriptionLifecycle(AuthenticatedAPITestCase):

    def setUp(self):
        super(TestFixSubscriptionLifecycle, self).setUp()

        self.messageset_second = self.make_messageset_second()
        self.messageset.next_set = self.messageset_second
        self.messageset.save()

        self.make_messages()

    def make_messages(self):
        for sequence in range(0, 3):
            message_data = {
                'messageset': self.messageset,
                'sequence_number': sequence + 1,
                'lang': 'eng_ZA',
                'text_content': 'This is a test message %s.' % (sequence + 1),
            }
            Message.objects.create(**message_data)

    def make_messageset_second(self):
        messageset_data = {
            'short_name': 'messageset_second',
            'notes': None,
            'next_set': None,
            'default_schedule': self.schedule,
            'content_type': 'audio'
        }
        return MessageSet.objects.create(**messageset_data)

    def test_noop(self):
        stdout, stderr = StringIO(), StringIO()

        self.make_subscription()

        call_command('fix_subscription_lifecycle', '--action', 'send',
                     stdout=stdout, stderr=stderr)

        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(
            stdout.getvalue().strip(),
            "0 subscriptions behind schedule.\n"
            "0 subscriptions fast forwarded to end date.\n"
            "Message sent to 0 subscriptions.")

    def test_noop_no_action_flag(self):
        stdout, stderr = StringIO(), StringIO()

        self.make_subscription()
        sub1 = self.make_subscription()
        sub1.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub1.save()

        sub2 = self.make_subscription()
        sub2.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub2.active = False
        sub2.save()

        call_command('fix_subscription_lifecycle', '--verbose', 'True',
                     stdout=stdout, stderr=stderr)

        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(
            stdout.getvalue().strip(),
            "{}: 2\n"
            "1 subscription behind schedule.\n"
            "0 subscriptions fast forwarded to end date.\n"
            "Message sent to 0 subscriptions.".format(sub1.id))

    @responses.activate
    def test_subscriptions_lifecycle_send(self):
        stdout, stderr = StringIO(), StringIO()

        self.make_subscription()
        sub1 = self.make_subscription()
        sub1.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub1.save()

        sub2 = self.make_subscription()
        sub2.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub2.active = False
        sub2.save()

        # mock identity lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/" % (sub1.identity, ),  # noqa
            json={
                "foo": sub1.identity,
                "version": 1,
                "details": {
                    "default_addr_type": "msisdn",
                    "addresses": {
                        "msisdn": {
                            "+2345059992222": {}
                        }
                    },
                    "receiver_role": "mother",
                    "linked_to": None,
                    "preferred_msg_type": "text",
                    "preferred_language": "eng_ZA"
                },
                "created_at": "2015-07-10T06:13:29.693272Z",
                "updated_at": "2015-07-10T06:13:29.693298Z"
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # mock identity address lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/addresses/msisdn?default=True&use_communicate_through=True" % (sub1.identity, ),  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"address": "+2345059992222"}]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # Create message sender call
        responses.add(
            responses.POST,
            "http://seed-message-sender/api/v1/outbound/",
            json={
                "url": "http://seed-message-sender/api/v1/outbound/c7f3c839-2bf5-42d1-86b9-ccb886645fb4/",  # noqa
                "id": "c7f3c839-2bf5-42d1-86b9-ccb886645fb4",
                "version": 1,
                "to_addr": "+2345059992222",
                "to_identity": "8646b7bc-b511-4965-a90b-e1145e398703",
                "vumi_message_id": None,
                "content": "This is message 1",
                "delivered": False,
                "attempts": 0,
                "metadata": {},
                "created_at": "2016-03-24T13:43:43.614952Z",
                "updated_at": "2016-03-24T13:43:43.614921Z"
            },
            status=200, content_type='application/json'
        )

        # metrics call
        self.session = None
        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json; charset=utf-8'
        )

        call_command('fix_subscription_lifecycle', '--action', 'send',
                     stdout=stdout, stderr=stderr)

        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(
            stdout.getvalue().strip(),
            "1 subscription behind schedule.\n"
            "0 subscriptions fast forwarded to end date.\n"
            "Message sent to 1 subscription.")

    @responses.activate
    def test_subscription_lifecycle_send_with_args(self):
        stdout, stderr = StringIO(), StringIO()

        sub1 = self.make_subscription()
        sub1.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub1.save()

        sub2 = self.make_subscription()
        sub2.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub2.save()

        # mock identity lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/" % (sub1.identity, ),  # noqa
            json={
                "foo": sub1.identity,
                "version": 1,
                "details": {
                    "default_addr_type": "msisdn",
                    "addresses": {
                        "msisdn": {
                            "+2345059992222": {}
                        }
                    },
                    "receiver_role": "mother",
                    "linked_to": None,
                    "preferred_msg_type": "text",
                    "preferred_language": "eng_ZA"
                },
                "created_at": "2015-07-10T06:13:29.693272Z",
                "updated_at": "2015-07-10T06:13:29.693298Z"
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # mock identity address lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/addresses/msisdn?default=True&use_communicate_through=True" % (sub1.identity, ),  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"address": "+2345059992222"}]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # Create message sender call
        responses.add(
            responses.POST,
            "http://seed-message-sender/api/v1/outbound/",
            json={
                "url": "http://seed-message-sender/api/v1/outbound/c7f3c839-2bf5-42d1-86b9-ccb886645fb4/",  # noqa
                "id": "c7f3c839-2bf5-42d1-86b9-ccb886645fb4",
                "version": 1,
                "to_addr": "+2345059992222",
                "to_identity": "8646b7bc-b511-4965-a90b-e1145e398703",
                "vumi_message_id": None,
                "content": "This is message 1",
                "delivered": False,
                "attempts": 0,
                "metadata": {},
                "created_at": "2016-03-24T13:43:43.614952Z",
                "updated_at": "2016-03-24T13:43:43.614921Z"
            },
            status=200, content_type='application/json'
        )

        # metrics call
        self.session = None
        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json; charset=utf-8'
        )

        call_command('fix_subscription_lifecycle', '--action', 'send',
                     '--end_date', '20170101',
                     stdout=stdout, stderr=stderr)

        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(
            stdout.getvalue().strip(),
            "2 subscriptions behind schedule.\n"
            "0 subscriptions fast forwarded to end date.\n"
            "Message sent to 2 subscriptions.")

    @responses.activate
    def test_subscriptions_lifecycle_fast_forward(self):
        stdout, stderr = StringIO(), StringIO()

        self.make_subscription()
        sub1 = self.make_subscription()
        sub1.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub1.save()

        sub2 = self.make_subscription()
        sub2.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub2.active = False
        sub2.save()

        # mock identity lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/" % (sub1.identity, ),  # noqa
            json={
                "foo": sub1.identity,
                "version": 1,
                "details": {
                    "default_addr_type": "msisdn",
                    "addresses": {
                        "msisdn": {
                            "+2345059992222": {}
                        }
                    },
                    "receiver_role": "mother",
                    "linked_to": None,
                    "preferred_msg_type": "text",
                    "preferred_language": "eng_ZA"
                },
                "created_at": "2015-07-10T06:13:29.693272Z",
                "updated_at": "2015-07-10T06:13:29.693298Z"
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # mock identity address lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/addresses/msisdn?default=True&use_communicate_through=True" % (sub1.identity, ),  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"address": "+2345059992222"}]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # Create message sender call
        responses.add(
            responses.POST,
            "http://seed-message-sender/api/v1/outbound/",
            json={
                "url": "http://seed-message-sender/api/v1/outbound/c7f3c839-2bf5-42d1-86b9-ccb886645fb4/",  # noqa
                "id": "c7f3c839-2bf5-42d1-86b9-ccb886645fb4",
                "version": 1,
                "to_addr": "+2345059992222",
                "to_identity": "8646b7bc-b511-4965-a90b-e1145e398703",
                "vumi_message_id": None,
                "content": "This is message 1",
                "delivered": False,
                "attempts": 0,
                "metadata": {},
                "created_at": "2016-03-24T13:43:43.614952Z",
                "updated_at": "2016-03-24T13:43:43.614921Z"
            },
            status=200, content_type='application/json'
        )

        # metrics call
        self.session = None
        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json; charset=utf-8'
        )

        call_command('fix_subscription_lifecycle', '--action', 'fast_forward',
                     stdout=stdout, stderr=stderr)

        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(
            stdout.getvalue().strip(),
            "1 subscription behind schedule.\n"
            "1 subscription fast forwarded to end date.\n"
            "Message sent to 0 subscriptions.")
        updated_sub = Subscription.objects.get(pk=sub1.id)
        self.assertEqual(updated_sub.next_sequence_number, 3)

    @responses.activate
    def test_subscription_lifecycle_fast_forward_with_args(self):
        stdout, stderr = StringIO(), StringIO()

        sub1 = self.make_subscription()
        sub1.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub1.save()

        sub2 = self.make_subscription()
        sub2.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub2.save()

        # mock identity lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/" % (sub1.identity, ),  # noqa
            json={
                "foo": sub1.identity,
                "version": 1,
                "details": {
                    "default_addr_type": "msisdn",
                    "addresses": {
                        "msisdn": {
                            "+2345059992222": {}
                        }
                    },
                    "receiver_role": "mother",
                    "linked_to": None,
                    "preferred_msg_type": "text",
                    "preferred_language": "eng_ZA"
                },
                "created_at": "2015-07-10T06:13:29.693272Z",
                "updated_at": "2015-07-10T06:13:29.693298Z"
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # mock identity address lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/%s/addresses/msisdn?default=True&use_communicate_through=True" % (sub1.identity, ),  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"address": "+2345059992222"}]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        # Create message sender call
        responses.add(
            responses.POST,
            "http://seed-message-sender/api/v1/outbound/",
            json={
                "url": "http://seed-message-sender/api/v1/outbound/c7f3c839-2bf5-42d1-86b9-ccb886645fb4/",  # noqa
                "id": "c7f3c839-2bf5-42d1-86b9-ccb886645fb4",
                "version": 1,
                "to_addr": "+2345059992222",
                "to_identity": "8646b7bc-b511-4965-a90b-e1145e398703",
                "vumi_message_id": None,
                "content": "This is message 1",
                "delivered": False,
                "attempts": 0,
                "metadata": {},
                "created_at": "2016-03-24T13:43:43.614952Z",
                "updated_at": "2016-03-24T13:43:43.614921Z"
            },
            status=200, content_type='application/json'
        )

        # metrics call
        self.session = None
        responses.add(
            responses.POST,
            "http://metrics-url/metrics/",
            json={"foo": "bar"},
            status=200, content_type='application/json; charset=utf-8'
        )

        call_command('fix_subscription_lifecycle', '--action', 'fast_forward',
                     '--end_date', '20170101',
                     stdout=stdout, stderr=stderr)

        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(
            stdout.getvalue().strip(),
            "2 subscriptions behind schedule.\n"
            "2 subscriptions fast forwarded to end date.\n"
            "Message sent to 0 subscriptions.")
        updated_sub = Subscription.objects.get(pk=sub2.id)
        self.assertEqual(updated_sub.next_sequence_number, 3)

    def test_diff_action(self):
        stdout, stderr = StringIO(), StringIO()

        self.make_subscription()
        sub1 = self.make_subscription()
        sub1.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub1.save()

        sub2 = self.make_subscription()
        sub2.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub2.active = False
        sub2.save()

        self.assertEqual(Subscription.objects.count(), 3)

        call_command(
            'fix_subscription_lifecycle', '--action', 'diff',
            '--end_date', '20170101', stdout=stdout, stderr=stderr)

        self.assertEqual(stderr.getvalue(), '')
        [diff, _, _, _] = stdout.getvalue().strip().split('\n')
        diff = json.loads(diff)
        self.assertEqual(
            diff, {
                'identity': "8646b7bc-b511-4965-a90b-e1145e398703",
                'language': "eng_ZA",
                'current_messageset_id': self.messageset.pk,
                'current_sequence_number': 1,
                'expected_messageset_id': self.messageset_second.pk,
                'expected_sequence_number': 0,
            })

        # Ensure that we haven't changed any of the subscriptions
        self.assertEqual(Subscription.objects.count(), 3)
        for sub in Subscription.objects.all():
            self.assertEqual(sub.next_sequence_number, 1)

    def test_filter_by_messageset(self):
        stdout, stderr = StringIO(), StringIO()

        self.make_subscription()
        sub1 = self.make_subscription()
        sub1.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub1.messageset = self.messageset_second
        sub1.save()

        sub2 = self.make_subscription()
        sub2.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub2.save()

        sub3 = self.make_subscription()
        sub3.created_at = datetime(2016, 1, 1, 0, 0, tzinfo=pytz.UTC)
        sub3.save()

        call_command(
            'fix_subscription_lifecycle', '--end_date', '20170101',
            '--message-set', str(self.messageset.pk),
            stdout=stdout, stderr=stderr)
        self.assertEqual(stderr.getvalue(), '')
        output = stdout.getvalue().strip().split('\n')
        self.assertEqual(output, [
            '2 subscriptions behind schedule.',
            '0 subscriptions fast forwarded to end date.',
            'Message sent to 0 subscriptions.',
        ])


class TestMarkInvalidSubscription(AuthenticatedAPITestCase):

    @responses.activate
    def test_mark_invalid_subscription(self):
        """
        If there are subscriptions with a process_status of 5 the
        registrations and identity linked to them should be updated
        """
        stdout, stderr = StringIO(), StringIO()

        registration = {
            "id": "8646b7bc-b511-4965-a90b-1111111111111",
            "data": {}
        }

        # mock registration lookup
        responses.add(
            responses.GET,
            "http://seed-hub/api/v1/registrations/?mother_id=8646b7bc-b511-4965-a90b-e1145e398703",  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [registration]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        registration['data']['exclude_report'] = True
        # mock registration update
        responses.add(
            responses.PATCH,
            "http://seed-hub/api/v1/registration/8646b7bc-b511-4965-a90b-1111111111111/",  # noqa
            json.dumps(registration),
            status=200, content_type='application/json')

        identity = {
            "id": "8646b7bc-b511-4965-a90b-e1145e398703",
            "version": 1,
            "details": {
                "default_addr_type": "msisdn",
                "addresses": {
                    "msisdn": {
                        "+2345059992222": {}
                    }
                },
                "receiver_role": "mother",
                "linked_to": None,
                "preferred_msg_type": "text",
                "preferred_language": "eng_ZA"
            },
            "created_at": "2015-07-10T06:13:29.693272Z",
            "updated_at": "2015-07-10T06:13:29.693298Z"
        }

        # mock identity lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/8646b7bc-b511-4965-a90b-e1145e398703/",  # noqa
            json=identity,
            status=200, content_type='application/json',
            match_querystring=True
        )

        identity['details']['exclude_report'] = True
        # mock identity update
        responses.add(
            responses.PATCH,
            "http://seed-identity-store/api/v1/identities/8646b7bc-b511-4965-a90b-e1145e398703/",  # noqa
            json.dumps(identity),
            status=200, content_type='application/json')

        # Sub that should be updated
        sub = self.make_subscription()
        sub.process_status = 5
        sub.save()

        # Sub that should not be
        self.make_subscription()

        call_command('mark_invalid_subscriptions', '--hub-url',
                     'http://seed-hub/api/v1/', '--hub-token', 'HUBTOKEN',
                     stdout=stdout, stderr=stderr)

        output = stdout.getvalue().strip()
        self.assertEqual(output, "Updated 1 identities and 1 registrations.")

    @responses.activate
    def test_mark_invalid_subscription_server_error(self):
        """
        If there are subscriptions with a process_status of 5 the
        registrations and identity linked to them should be updated, this tests
        handling errors when updating
        """
        stdout, stderr = StringIO(), StringIO()

        registration = {
            "id": "8646b7bc-b511-4965-a90b-1111111111111",
            "data": {}
        }

        # mock registration lookup
        responses.add(
            responses.GET,
            "http://seed-hub/api/v1/registrations/?mother_id=8646b7bc-b511-4965-a90b-e1145e398703",  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [registration]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        registration['data']['exclude_report'] = True
        # mock registration update
        responses.add(
            responses.PATCH,
            "http://seed-hub/api/v1/registration/8646b7bc-b511-4965-a90b-1111111111111/",  # noqa
            json.dumps(registration),
            status=500, content_type='application/json')

        identity = {
            "id": "8646b7bc-b511-4965-a90b-e1145e398703",
            "version": 1,
            "details": {
                "default_addr_type": "msisdn",
                "addresses": {
                    "msisdn": {
                        "+2345059992222": {}
                    }
                },
                "receiver_role": "mother",
                "linked_to": None,
                "preferred_msg_type": "text",
                "preferred_language": "eng_ZA"
            },
            "created_at": "2015-07-10T06:13:29.693272Z",
            "updated_at": "2015-07-10T06:13:29.693298Z"
        }

        # mock identity lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/8646b7bc-b511-4965-a90b-e1145e398703/",  # noqa
            json=identity,
            status=200, content_type='application/json',
            match_querystring=True
        )

        identity['details']['exclude_report'] = True
        # mock identity update
        responses.add(
            responses.PATCH,
            "http://seed-identity-store/api/v1/identities/8646b7bc-b511-4965-a90b-e1145e398703/",  # noqa
            json.dumps(identity),
            status=500, content_type='application/json')

        # Sub that should be updated
        sub = self.make_subscription()
        sub.process_status = 5
        sub.save()

        # Sub that should not be
        self.make_subscription()

        call_command('mark_invalid_subscriptions', '--hub-url',
                     'http://seed-hub/api/v1/', '--hub-token', 'HUBTOKEN',
                     stdout=stdout, stderr=stderr)

        output = stdout.getvalue().strip()
        self.assertTrue(
            output.find("Updated 0 identities and 0 registrations.") != -1)
        self.assertTrue(
            output.find("Invalid Identity Store API response(500)") != -1)
        self.assertTrue(output.find("Invalid Hub API response(500)") != -1)

    @responses.activate
    def test_mark_invalid_subscription_connection_error(self):
        """
        If there are subscriptions with a process_status of 5 the
        registrations and identity linked to them should be updated, this tests
        handling errors when updating
        """
        stdout, stderr = StringIO(), StringIO()

        registration = {
            "id": "8646b7bc-b511-4965-a90b-1111111111111",
            "data": {}
        }

        # mock registration lookup
        responses.add(
            responses.GET,
            "http://seed-hub/api/v1/registrations/?mother_id=8646b7bc-b511-4965-a90b-e1145e398703",  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [registration]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        identity = {
            "id": "8646b7bc-b511-4965-a90b-e1145e398703",
            "version": 1,
            "details": {
                "default_addr_type": "msisdn",
                "addresses": {
                    "msisdn": {
                        "+2345059992222": {}
                    }
                },
                "receiver_role": "mother",
                "linked_to": None,
                "preferred_msg_type": "text",
                "preferred_language": "eng_ZA"
            },
            "created_at": "2015-07-10T06:13:29.693272Z",
            "updated_at": "2015-07-10T06:13:29.693298Z"
        }

        # mock identity lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/8646b7bc-b511-4965-a90b-e1145e398703/",  # noqa
            json=identity,
            status=200, content_type='application/json',
            match_querystring=True
        )

        # Sub that should be updated
        sub = self.make_subscription()
        sub.process_status = 5
        sub.save()

        # Sub that should not be
        self.make_subscription()

        call_command('mark_invalid_subscriptions', '--hub-url',
                     'http://seed-hub/api/v1/', '--hub-token', 'HUBTOKEN',
                     stdout=stdout, stderr=stderr)

        output = stdout.getvalue().strip()
        self.assertTrue(
            output.find("Updated 0 identities and 0 registrations.") != -1)
        self.assertTrue(
            output.find("Connection error to Identity API") != -1)
        self.assertTrue(output.find("Connection error to Hub API") != -1)

    @responses.activate
    def test_mark_invalid_subscription_unchanged(self):
        """
        If there are subscriptions with with a process_status of 5 the
        registrations and identity linked to them should be updated, this test
        simulates a case where they have already been updated
        """
        stdout, stderr = StringIO(), StringIO()

        registration = {
            "id": "8646b7bc-b511-4965-a90b-1111111111111",
            "data": {
                "exclude_report": True
            }
        }

        # mock registration lookup
        responses.add(
            responses.GET,
            "http://seed-hub/api/v1/registrations/?mother_id=8646b7bc-b511-4965-a90b-e1145e398703",  # noqa
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [registration]
            },
            status=200, content_type='application/json',
            match_querystring=True
        )

        identity = {
            "id": "8646b7bc-b511-4965-a90b-e1145e398703",
            "version": 1,
            "details": {
                "default_addr_type": "msisdn",
                "addresses": {
                    "msisdn": {
                        "+2345059992222": {}
                    }
                },
                "receiver_role": "mother",
                "linked_to": None,
                "preferred_msg_type": "text",
                "preferred_language": "eng_ZA",
                "exclude_report": True
            },
            "created_at": "2015-07-10T06:13:29.693272Z",
            "updated_at": "2015-07-10T06:13:29.693298Z"
        }

        # mock identity lookup
        responses.add(
            responses.GET,
            "http://seed-identity-store/api/v1/identities/8646b7bc-b511-4965-a90b-e1145e398703/",  # noqa
            json=identity,
            status=200, content_type='application/json',
            match_querystring=True
        )

        # Sub that should be updated
        sub = self.make_subscription()
        sub.process_status = 5
        sub.save()

        # Sub that should not be
        self.make_subscription()

        call_command('mark_invalid_subscriptions', '--hub-url',
                     'http://seed-hub/api/v1/', '--hub-token', 'HUBTOKEN',
                     stdout=stdout, stderr=stderr)

        output = stdout.getvalue().strip()
        self.assertEqual(output, "Updated 0 identities and 0 registrations.")

    @responses.activate
    def test_mark_invalid_subscription_noop(self):
        """
        If there are no subscription with process_astatus of 5 nothing
        should be updated
        """
        stdout, stderr = StringIO(), StringIO()

        self.make_subscription()

        call_command('mark_invalid_subscriptions', '--hub-url',
                     'http://seed-hub/api/v1/', '--hub-token', 'HUBTOKEN',
                     stdout=stdout, stderr=stderr)

        output = stdout.getvalue().strip()
        self.assertEqual(output, "Updated 0 identities and 0 registrations.")

    @responses.activate
    def test_mark_invalid_subscription_missing_args(self):
        """
        If the command is called without the correct arguments, it should give
        a appropriate error message
        """

        stdout, stderr = StringIO(), StringIO()
        call_command(
            'mark_invalid_subscriptions', stdout=stdout, stderr=stderr)

        error = stdout.getvalue().strip()
        self.assertEqual(error, "hub-url and hub-token is required.")


class TestFailedTaskAPI(AuthenticatedAPITestCase):

    @responses.activate
    def test_failed_tasks_requeue(self):
        # mock schedule sending
        responses.add(
            responses.POST,
            "http://seed-scheduler/api/v1/schedule/",
            json={
                "id": "1234"
            },
            status=201, content_type='application/json'
        )
        # Setup
        existing = self.make_subscription()

        SubscriptionSendFailure.objects.create(
            subscription=existing,
            task_id=uuid4(),
            initiated_at=timezone.now(),
            reason='Error')

        response = self.client.post('/api/v1/failed-tasks/',
                                    content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["requeued_failed_tasks"], True)
        self.assertEqual(SubscriptionSendFailure.objects.all().count(), 0)
