from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """A lecturer, as DevPerf knows them.

    DevPerf owns the account; this row exists so LiveGrade's own data (sessions
    a lecturer created) has something to point at. It is matched on ``sub``,
    DevPerf's stable id for the person, never on email, which can change or be
    shared. There is no password here: sign-in is always through DevPerf.
    """

    sub = models.CharField(
        max_length=64, unique=True,
        help_text="DevPerf's id for this person (the token's `sub` claim).")

    def save(self, *args, **kwargs):
        # Only when a usable one was set: an unusable password is random each
        # time it is made, and changing it on every save would change the
        # session hash and sign the person out of their other sessions.
        if self.has_usable_password():
            self.set_unusable_password()
        super().save(*args, **kwargs)

    def display_name(self):
        return self.get_full_name() or self.username


class OrganizationLecturer(models.Model):
    """One person who may run LiveGrade in one organization, as DevPerf last said.

    LiveGrade keeps no organization table of its own, so this is the only way it
    can offer "hand this session to a colleague": the people DevPerf has told it
    about, refreshed at each of their sign-ins. Somebody who has never signed in
    here is not on it, and somebody removed in DevPerf stays until they next
    sign in (their sign-in then deletes the row).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='organization_roles')
    organization_id = models.BigIntegerField(db_index=True)
    organization_name = models.CharField(max_length=200, blank=True)
    last_seen = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'organization_id'], name='one_row_per_person_and_organization'),
        ]

    def __str__(self):
        return f'{self.user_id} in {self.organization_id}'
