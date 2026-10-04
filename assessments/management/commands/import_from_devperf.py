"""Copy LiveGrade's data out of DevPerf's database into this one.

LiveGrade used to be part of DevPerf, so its sessions, rosters, rubrics, groups,
turns and grades are still in DevPerf's ``assessments_*`` tables. This copies
them here, keeping every primary key and every session ``uuid`` so a QR code or
join link already handed out still points at the same session.

DevPerf's database is only ever *read*: the command issues SELECTs against it and
nothing else. It is reached through a second database alias, ``devperf``,
configured with the ``DEVPERF_DB_*`` environment variables.

- Lecturers: a session's ``created_by`` was a DevPerf user. Here it is a row keyed
  on DevPerf's user id (the same ``sub`` the sign-in token carries), created from
  DevPerf's name and email, so the lecturer is recognised the first time they sign in.
- Organizations: the ``organization_id`` column is copied as it is; it is DevPerf's id.
- Safe to run again. By default rows already here are left as they are and only
  missing ones are added, so a rehearsal and the real cutover can both be run.
  ``--replace`` instead empties LiveGrade's assessment tables first and copies
  everything fresh (the final sync at cutover); it needs ``--yes``.
- ``--dry-run`` does all of it inside a transaction and rolls it back.
- It finishes by checking that every source row is here, and exits with an error if
  any is not.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connection, connections, transaction
from django.db.models import Sum

from accounts.models import User
from assessments.models import (AssessmentSession, Evaluation, EvaluationScore,
                                GroupMembership, ParticipationRecord,
                                PresentationGroup, PresentationTurn,
                                RubricCategory, Student)

# Parents before children (constraints are deferred to commit anyway).
MODELS = (AssessmentSession, RubricCategory, Student, PresentationGroup,
          GroupMembership, PresentationTurn, ParticipationRecord, Evaluation,
          EvaluationScore)


class Command(BaseCommand):
    help = "Copy LiveGrade's sessions and grades from DevPerf's database into this one."

    def add_arguments(self, parser):
        parser.add_argument('--source', default='devperf',
                            help='Database alias for DevPerf (default: devperf).')
        parser.add_argument('--replace', action='store_true',
                            help="Empty LiveGrade's assessment tables first, then copy everything.")
        parser.add_argument('--yes', action='store_true',
                            help='Confirm --replace.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Do everything, then roll it back.')

    def handle(self, *args, source, replace, yes, dry_run, **options):
        if source == 'default':
            raise CommandError("The source must be DevPerf's database, not LiveGrade's own.")
        if source not in connections.databases:
            raise CommandError(
                f"No database '{source}'. Set DEVPERF_DB_NAME (and DEVPERF_DB_USER, "
                'DEVPERF_DB_PASSWORD, DEVPERF_DB_HOST, DEVPERF_DB_PORT) to reach DevPerf.')
        if replace and not (yes or dry_run):
            raise CommandError('--replace empties LiveGrade\'s assessment data first; add --yes to confirm.')

        src = connections[source]
        with transaction.atomic():
            if replace:
                for model in reversed(MODELS):
                    model.objects.all().delete()
            users = self.copy_lecturers(src)
            copied = {}
            for model in MODELS:
                copied[model] = self.copy_model(src, model, users)
            self.reset_sequences()
            problems = self.verify(src)
            for line in self.report(copied, problems, dry_run):
                self.stdout.write(line)
            if dry_run:
                transaction.set_rollback(True)
        if problems:
            raise CommandError('Some DevPerf rows are missing here; nothing should be cut over yet.')

    # -- lecturers ---------------------------------------------------------

    def copy_lecturers(self, src):
        """DevPerf user id -> the local user, created from DevPerf's own record."""
        with src.cursor() as cursor:
            cursor.execute(
                f'SELECT DISTINCT created_by_id FROM {AssessmentSession._meta.db_table} '
                'WHERE created_by_id IS NOT NULL')
            ids = [row[0] for row in cursor.fetchall()]
        users = {}
        for devperf_id in ids:
            with src.cursor() as cursor:
                cursor.execute(
                    'SELECT username, first_name, last_name, email FROM auth_user WHERE id = %s',
                    [devperf_id])
                found = cursor.fetchone()
            username, first, last, email = found or (f'devperf-{devperf_id}', '', '', '')
            user, _ = User.objects.get_or_create(
                sub=str(devperf_id),
                defaults={'username': f'devperf-{devperf_id}', 'first_name': first,
                          'last_name': last, 'email': email})
            users[devperf_id] = user.pk
        return users

    # -- rows --------------------------------------------------------------

    def copy_model(self, src, model, users):
        fields = list(model._meta.concrete_fields)
        columns = ', '.join(src.ops.quote_name(f.column) for f in fields)
        with src.cursor() as cursor:
            cursor.execute(
                f'SELECT {columns} FROM {src.ops.quote_name(model._meta.db_table)} '
                f'ORDER BY {src.ops.quote_name(model._meta.pk.column)}')
            rows = cursor.fetchall()
        objects = []
        for row in rows:
            values = {f.attname: value for f, value in zip(fields, row)}
            if 'created_by_id' in values and values['created_by_id'] is not None:
                values['created_by_id'] = users[values['created_by_id']]
            objects.append(model(**values))
        before = model.objects.count()
        model.objects.bulk_create(objects, ignore_conflicts=True)
        return model.objects.count() - before

    def reset_sequences(self):
        """After inserting explicit ids, point each Postgres sequence past them so
        the next new row does not collide. A no-op on SQLite."""
        statements = connection.ops.sequence_reset_sql(no_style(), MODELS)
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

    # -- checking ----------------------------------------------------------

    def verify(self, src):
        """Every source row present here, and the grades add up the same."""
        problems = []
        for model in MODELS:
            pk = model._meta.pk.column
            with src.cursor() as cursor:
                cursor.execute(f'SELECT {src.ops.quote_name(pk)} FROM {src.ops.quote_name(model._meta.db_table)}')
                wanted = {row[0] for row in cursor.fetchall()}
            have = set(model.objects.filter(pk__in=wanted).values_list('pk', flat=True))
            if wanted - have:
                problems.append(f'{model._meta.label}: {len(wanted - have)} of {len(wanted)} rows missing')
        with src.cursor() as cursor:
            cursor.execute(f'SELECT COALESCE(SUM(value), 0) FROM {EvaluationScore._meta.db_table}')
            source_total = Decimal(str(cursor.fetchone()[0]))
        # Compared over the rows that came from the source (same ids), so grades
        # entered here before the copy do not make a correct copy look wrong.
        with src.cursor() as cursor:
            cursor.execute(f'SELECT id FROM {EvaluationScore._meta.db_table}')
            ids = [row[0] for row in cursor.fetchall()]
        local_total = (EvaluationScore.objects.filter(pk__in=ids)
                       .aggregate(total=Sum('value'))['total'] or Decimal('0'))
        if Decimal(str(local_total)) != source_total:
            problems.append(f'EvaluationScore values differ: DevPerf {source_total}, here {local_total}')
        return problems

    def report(self, copied, problems, dry_run):
        yield ('DRY RUN - nothing was kept.' if dry_run else 'Copied from DevPerf:')
        for model, added in copied.items():
            yield f'  {model._meta.label:<34} {added:>6} added, {model.objects.count():>6} in total'
        for problem in problems:
            yield f'PROBLEM: {problem}'
        if not problems:
            yield 'Check passed: every DevPerf row is here and the grades add up the same.'
