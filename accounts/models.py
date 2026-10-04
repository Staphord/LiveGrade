from django.contrib.auth.models import AbstractUser
from django.db import models


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
