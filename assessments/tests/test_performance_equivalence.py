"""The bulk-loaded results must equal the one-student-at-a-time originals, and stay cheap.

`session_results`, `group_score_percents` and the vote counts replaced per-student and
per-group queries. The originals (`group_score_percent`, `individual_score_percent`,
`ungraded_group_count`, `final_score_percent`) are kept, so they are the oracle here:
random data, every number compared.
"""
import random
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from assessments import scoring
from assessments.models import (Evaluation, EvaluationScore, ParticipationRecord, PresentationGroup,
                                PresentationTurn, RubricCategory)
from assessments.tests.test_edges_core import SessionFixture


class RandomSession(SessionFixture):
    """A session with many groups, a mix of scopes and weights, patchy grading, a loner and
    a group with no members - the shapes that make a shortcut disagree with the original."""

    def setUp(self):
        super().setUp()
        rng = random.Random(7)
        self.rng = rng
        RubricCategory.objects.create(
            assessment_session=self.session, name='Delivery', scope=RubricCategory.Scope.GROUP,
            max_points=7, weight=Decimal('13.5'))
        RubricCategory.objects.create(
            assessment_session=self.session, name='Effort', scope=RubricCategory.Scope.INDIVIDUAL,
            max_points=3, weight=Decimal('9.5'))
        RubricCategory.objects.create(
            assessment_session=self.session, name='Unscored', scope=RubricCategory.Scope.GROUP,
            max_points=5, weight=Decimal('1'))
        self.students = [self.ann, self.bob, self.cat, self.dan]
        for index in range(5, 24):
            self.students.append(self.student(f'Pupil {index}', f'P{index}'))
        self.loner = self.student('Loner Lee', 'LL')
        self.students.append(self.loner)
        self.groups = [self.red, self.blue]
        rest = self.students[4:-1]
        for order, size in enumerate((3, 4, 2, 5, 5), start=3):
            self.groups.append(self.group(f'G{order}', order, *rest[:size]))
            rest = rest[size:]
        self.empty = self.group('Empty', 9)
        self.groups.append(self.empty)
        self.grade()

    def grade(self):
        now = timezone.now()
        group_cats = list(self.session.rubric_categories.filter(scope=RubricCategory.Scope.GROUP))
        individual_cats = list(self.session.rubric_categories.filter(scope=RubricCategory.Scope.INDIVIDUAL))
        for group in self.groups[:-2]:
            turn = PresentationTurn.objects.create(
                assessment_session=self.session, group=group, status=PresentationTurn.Status.CLOSED,
                opened_at=now, closed_at=now, voting_opened_at=now)
            own = group.member_ids()
            for student in self.students:
                if student.pk in own or self.rng.random() < 0.3:
                    continue
                evaluation = Evaluation.objects.create(presentation_turn=turn, evaluator=student)
                for category in group_cats[:2]:
                    EvaluationScore.objects.create(
                        evaluation=evaluation, rubric_category=category,
                        value=Decimal(self.rng.randint(0, int(category.max_points) * 4)) / 4)
                for membership in group.memberships.all():
                    if self.rng.random() < 0.7:
                        target = Evaluation.objects.create(
                            presentation_turn=turn, evaluator=student, target_student=membership.student)
                        for category in individual_cats:
                            EvaluationScore.objects.create(
                                evaluation=target, rubric_category=category,
                                value=Decimal(self.rng.randint(0, int(category.max_points) * 4)) / 4)


class SessionResultsMatchTheOriginalsTests(RandomSession):

    def test_every_student_and_group_number_is_the_same(self):
        results = scoring.session_results(self.session)
        self.assertGreater(len(results['student_rows']), 15)
        for row in results['student_rows']:
            student, group = row['student'], row['group']
            self.assertEqual(row['group_percent'], scoring.group_score_percent(group, self.session))
            self.assertEqual(row['individual_percent'], scoring.individual_score_percent(student, self.session))
            self.assertEqual(row['penalty'], scoring.ungraded_group_count(student, group, self.session))
            self.assertEqual(row['final_percent'], scoring.final_score_percent(student, group, self.session))
        for row in results['group_rows']:
            self.assertEqual(row['percent'], scoring.group_score_percent(row['group'], self.session))

    def test_a_session_with_no_rubric_still_matches(self):
        self.session.rubric_categories.all().delete()
        results = scoring.session_results(self.session)
        for row in results['student_rows']:
            self.assertIsNone(row['group_percent'])
            self.assertIsNone(row['individual_percent'])
            self.assertEqual(row['final_percent'], scoring.final_score_percent(
                row['student'], row['group'], self.session))

    def test_a_category_with_no_points_is_skipped_like_the_original(self):
        RubricCategory.objects.filter(pk=self.quality.pk).update(max_points=0)
        for row in scoring.session_results(self.session)['group_rows']:
            self.assertEqual(row['percent'], scoring.group_score_percent(row['group'], self.session))

    def test_a_single_group_session_has_no_penalty(self):
        PresentationGroup.objects.exclude(pk=self.red.pk).delete()
        rows = scoring.session_results(self.session)['student_rows']
        self.assertTrue(rows)
        self.assertEqual({row['penalty'] for row in rows}, {0})

    def test_results_cost_a_handful_of_queries_however_big_the_session(self):
        with CaptureQueriesContext(connection) as queries:
            scoring.session_results(self.session)
        self.assertLessEqual(len(queries), 8)


class GroupFiguresTests(RandomSession):

    def test_group_percents_match_the_original_for_every_group(self):
        groups = list(self.session.groups.all())
        percents = scoring.group_score_percents(self.session, groups)
        for group in groups:
            self.assertEqual(percents[group.pk], scoring.group_score_percent(group, self.session))

    def test_group_percents_are_none_without_a_group_rubric(self):
        self.session.rubric_categories.filter(scope=RubricCategory.Scope.GROUP).delete()
        groups = list(self.session.groups.all())
        self.assertEqual(set(scoring.group_score_percents(self.session, groups).values()), {None})

    def test_vote_counts_match_a_distinct_evaluator_count_per_group_and_per_turn(self):
        by_group = scoring.group_vote_counts(self.session)
        for group in self.session.groups.all():
            expected = Evaluation.objects.filter(
                presentation_turn__group=group, target_student__isnull=True,
            ).values('evaluator').distinct().count()
            self.assertEqual(by_group.get(group.pk, 0), expected)
        turns = list(self.session.presentation_turns.all())
        by_turn = scoring.turn_vote_counts([turn.pk for turn in turns])
        for turn in turns:
            expected = Evaluation.objects.filter(
                presentation_turn=turn, target_student__isnull=True,
            ).values('evaluator').distinct().count()
            self.assertEqual(by_turn.get(turn.pk, 0), expected)


class LecturerPagesCostTests(RandomSession):

    def setUp(self):
        super().setUp()
        from accounts.testing import make_user, sign_in
        self.session.status = self.session.Status.LIVE
        self.session.save(update_fields=['status'])
        for student in self.students[:12]:
            ParticipationRecord.objects.get_or_create(assessment_session=self.session, student=student)
        self.user = make_user('lect')
        self.session.created_by = self.user
        self.session.save(update_fields=['created_by'])
        sign_in(self.client, self.user, self.org)

    def queries(self, name, **params):
        from django.urls import reverse
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse(name, args=[self.session.pk]), params)
        self.assertEqual(response.status_code, 200)
        return len(ctx)

    def test_participation_does_not_query_per_student_or_group(self):
        self.assertLessEqual(self.queries('assessment_participation'), 14)
        self.assertLessEqual(self.queries('assessment_participation', per_page=100), 14)

    def test_results_do_not_query_per_student(self):
        self.assertLessEqual(self.queries('assessment_results'), 16)

    def test_the_groups_step_does_not_query_per_group(self):
        self.assertLessEqual(self.queries('assessment_groups'), 24)

    def test_live_control_does_not_query_per_group(self):
        self.assertLessEqual(self.queries('assessment_session_detail'), 40)


class BulkImportTests(SessionFixture):

    def test_a_big_import_writes_students_and_members_in_bulk_and_in_file_order(self):
        from assessments.group_import import apply_import, parse_groups
        from assessments.tests.xlsx_helpers import xlsx_buffer

        self.session.identify_by = self.session.IdentifyBy.FULL_NAME
        self.session.save(update_fields=['identify_by'])
        rows = [['Group Name', 'Name']]
        for number in range(8):
            rows.append([f'Team {number}', ', '.join(f'Person {number}-{n}' for n in range(5))])
        with CaptureQueriesContext(connection) as queries:
            counts = apply_import(self.session, parse_groups(xlsx_buffer(*rows)))
        self.assertEqual((counts['groups_created'], counts['students_created'], counts['members_added']),
                         (8, 40, 40))
        self.assertLessEqual(len(queries), 40)
        team = self.session.groups.get(name='Team 3')
        names = [m.student.full_name for m in team.memberships.order_by('pk')]
        self.assertEqual(names, [f'Person 3-{n}' for n in range(5)])
        self.assertTrue(all(m.student_id for g in self.session.groups.all() for m in g.memberships.all()))


class GradingNowCountTests(SessionFixture):

    def test_the_public_state_counts_students_flagged_as_grading_in_one_lookup(self):
        from django.core.cache import cache
        from django.urls import reverse

        for student in (self.ann, self.cat):
            cache.set(f'assessment-grading:{self.session.uuid}:{student.pk}', True, timeout=15)
        self.addCleanup(cache.clear)
        data = self.client.get(reverse('assessment_join_screen_state', args=[self.session.uuid])).json()
        self.assertEqual(data['public_grading_now_count'], 2)


class DragOnAPresentedGroupTests(RandomSession):
    """The in-place reply for a refused move carries the lock, the reason and the change log."""

    def setUp(self):
        super().setUp()
        from accounts.testing import make_user, sign_in
        self.session.status = self.session.Status.LIVE
        self.session.save(update_fields=['status'])
        user = make_user('lect')
        self.session.created_by = user
        self.session.save(update_fields=['created_by'])
        sign_in(self.client, user, self.org)

    def test_a_refused_move_returns_the_fixed_card_the_reason_and_the_changes_list(self):
        from django.urls import reverse
        member = self.red.memberships.first().student
        url = reverse('assessment_group_members', args=[self.session.pk, self.red.pk])
        data = self.client.post(url, {'action': 'remove', 'student_id': member.pk},
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertFalse(data['ok'])
        self.assertIn('fixed', data['error'])
        self.assertIn('group-lock', data['group_html'])
        self.assertNotIn('draggable="true"', data['group_html'])
        self.assertIn('id="recent-changes"', data['changes_html'])
        self.assertTrue(self.red.memberships.filter(student=member).exists())
        follow = self.client.get(reverse('assessment_groups', args=[self.session.pk]))
        self.assertNotContains(follow, data['error'])

    def test_an_allowed_move_lists_itself_in_recent_changes_at_once(self):
        from django.urls import reverse
        group = self.empty
        student = self.loner
        url = reverse('assessment_group_members', args=[self.session.pk, group.pk])
        data = self.client.post(url, {'action': 'add', 'student_id': student.pk},
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertTrue(data['ok'])
        self.assertIn('Added', data['changes_html'])

    def test_a_fixed_group_card_carries_its_reason_so_the_page_can_refuse_the_drop_up_front(self):
        from django.urls import reverse
        data = self.client.post(
            reverse('assessment_group_members', args=[self.session.pk, self.red.pk]),
            {'action': 'add', 'student_id': self.loner.pk}, HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertIn('data-members-lock="', data['group_html'])
        self.assertIn('so its members are fixed', data['group_html'])
