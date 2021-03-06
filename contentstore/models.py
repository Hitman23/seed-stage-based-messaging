from datetime import datetime
import os.path
from rest_framework.serializers import ValidationError
from django.db import models
from django.utils.translation import ugettext_lazy as _
from django.utils.encoding import python_2_unicode_compatible
from croniter import croniter


@python_2_unicode_compatible
class Schedule(models.Model):

    """
    Schedules (sometimes referred to as Protocols) are the method used to
    define the rate and frequency at which the messages are sent to
    the recipient
    """
    minute = models.CharField(_('minute'), max_length=64, default='*')
    hour = models.CharField(_('hour'), max_length=64, default='*')
    day_of_week = models.CharField(
        _('day of week'), max_length=64, default='*',
    )
    day_of_month = models.CharField(
        _('day of month'), max_length=64, default='*',
    )
    month_of_year = models.CharField(
        _('month of year'), max_length=64, default='*',
    )

    class Meta:
        verbose_name = _('schedule')
        verbose_name_plural = _('schedules')
        ordering = ['month_of_year', 'day_of_month',
                    'day_of_week', 'hour', 'minute']

    @property
    def cron_string(self):
        return '{minute} {hour} {dom} {moy} {dow}'.format(
            minute=self.rfield(self.minute), hour=self.rfield(self.hour),
            dom=self.rfield(self.day_of_month),
            moy=self.rfield(self.month_of_year),
            dow=self.rfield(self.day_of_week)
        )

    def rfield(self, s):
        return s and str(s).replace(' ', '') or '*'

    def __str__(self):
        return '{0} {1} {2} {3} {4} (m/h/d/dM/MY)'.format(
            self.rfield(self.minute), self.rfield(self.hour),
            self.rfield(self.day_of_week), self.rfield(self.day_of_month),
            self.rfield(self.month_of_year),
        )

    def get_run_times_between(self, start, end):
        """Gets a list of datetimes for when this cron schedule would be
        run between the given start and end datetimes.
        """
        dates = []
        for dt in croniter(self.cron_string, start, ret_type=datetime):
            if dt > end:
                break
            dates.append(dt)
        return dates


@python_2_unicode_compatible
class MessageSet(models.Model):

    """
        Details about a set of messages that a recipient can be sent on
        a particular schedule
    """
    CONTENT_TYPES = (
        ("text", 'Text'),
        ("audio", 'Audio')
    )

    short_name = models.CharField(_('Short name'), max_length=100, unique=True)
    notes = models.TextField(_('Notes'), null=True, blank=True)
    channel = models.CharField(_('Channel'), max_length=64, null=True,
                               blank=True)
    next_set = models.ForeignKey('self',
                                 null=True,
                                 blank=True)
    default_schedule = models.ForeignKey(Schedule,
                                         related_name='message_sets',
                                         null=False)
    content_type = models.CharField(choices=CONTENT_TYPES, max_length=20,
                                    default='text')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "%s" % self.short_name

    def get_messageset_max(self, lang):
        return self.messages.filter(lang=lang).count()

    def get_all_run_dates(self, start, lang, schedule=None, initial=None):
        """Returns the complete list of dates this MessageSet would run on given
        a start datetime, language, Schedule and initial message. If no
        Schedule is passed it will use the configured default_schedule. If no
        Initial is passed it assumes the full set will run.
        """
        if schedule is None:
            schedule = self.default_schedule
        dates = []
        set_max = self.get_messageset_max(lang)
        iters = initial if initial else 1  # start iterator at initial message
        for dt in croniter(schedule.cron_string, start, ret_type=datetime):
            if iters > set_max:
                break
            iters = iters + 1
            dates.append(dt)
        return dates


def generate_new_filename(instance, filename):
    ext = os.path.splitext(filename)[-1]  # get file extension
    return "%s%s" % (datetime.now().strftime("%Y%m%d%H%M%S%f"), ext)


@python_2_unicode_compatible
class BinaryContent(models.Model):
    """
        File store for reference in messages. Storage method handle by
        settings file.
    """

    content = models.FileField(upload_to=generate_new_filename,
                               max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "%s" % (self.content.path.split('/')[-1])


@python_2_unicode_compatible
class Message(models.Model):

    """
        Messages that a recipient can be sent
    """
    messageset = models.ForeignKey(MessageSet,
                                   related_name='messages',
                                   null=False)
    sequence_number = models.IntegerField(null=False, blank=False)
    lang = models.CharField(max_length=6, null=False, blank=False)
    text_content = models.TextField(null=True, blank=True)
    binary_content = models.ForeignKey(BinaryContent,
                                       related_name='message',
                                       null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sequence_number']
        unique_together = ('messageset', 'sequence_number', 'lang')

    def clean(self):
        # Don't allow messages to have neither a text or binary content
        if any([self.text_content, self.binary_content]) is False:
            raise ValidationError(
                _('Messages must have text or file attached'))

    def save(self, *args, **kwargs):
        self.clean()
        super(Message, self).save(*args, **kwargs)

    def __str__(self):
        return _("Message %s in %s from %s") % (
            self.sequence_number, self.lang, self.messageset.short_name)
