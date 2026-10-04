"""Hand a session to another lecturer from the command line.

The break-glass route for when a session's owner is unavailable (away, left, or
locked out) and so cannot use the Transfer button themselves. Recorded in the
transfer log like any other hand-over, with no "by" person.

    python manage.py transfer_session --session 12 --to-sub 21

``--to-sub`` is the new owner's DevPerf user id (the same id DevPerf puts in the
sign-in token). They must already be known here: have signed in once, or been
copied in by the import. ``--force`` skips the check that they are known to run
LiveGrade in the session's organization, for the case where they have not signed
in yet but the operator is certain.
"""
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from assessments.models import AssessmentSession
from assessments.transfers import TransferError, transfer_session


class Command(BaseCommand):
    help = "Transfer an assessment session to another lecturer (operator use)."

    def add_arguments(self, parser):
        parser.add_argument('--session', type=int, required=True, help='The session id.')
        parser.add_argument('--to-sub', required=True, help="The new owner's DevPerf user id.")
        parser.add_argument('--force', action='store_true',
                            help='Do not require the new owner to be known in the session\'s organization.')

    def handle(self, *args, session, to_sub, force, **options):
        try:
            target = AssessmentSession.objects.get(pk=session)
        except AssessmentSession.DoesNotExist:
            raise CommandError(f'There is no session {session}.')
        try:
            new_owner = User.objects.get(sub=str(to_sub))
        except User.DoesNotExist:
            raise CommandError(
                f'No lecturer with DevPerf id {to_sub} is known here. They must sign in to '
                'LiveGrade once (or be imported) before a session can be given to them.')
        try:
            transfer_session(target, new_owner, note='operator command', require_known=not force)
        except TransferError as error:
            raise CommandError(str(error))
        self.stdout.write(
            f'Session {target.pk} "{target.name}" now belongs to '
            f'{new_owner.get_full_name() or new_owner.username} (DevPerf id {new_owner.sub}).')
