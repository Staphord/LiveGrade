"""Can a lecturer in one organization reach another organization's records by id?

The classic multi-tenant bug: a lecturer of Org B opening Org A's object by
guessing its id, through a view that scopes the list but not the detail or the
POST. Here Org A owns one of everything reachable by id, and a lecturer of Org B
requests each of those URLs. For every (URL, method) three things must hold:

1. **No existence oracle.** The response is the same one Org B gets for an id
   that does not exist. A 403 for a real row against a 404 for a missing one
   would confirm the row is there, which is why views here answer 404.
2. **No data.** Nothing that identifies Org A's rows is in the response.
3. **No side effect.** Org A's rows are byte-for-byte what they were; a POST
   that was refused must not have written, deleted or toggled anything.

A URL that takes a row id must have an entry in ``TARGETS``; the coverage test
at the bottom fails until it does, so a new route cannot skip this check.
"""
import re

from django.core import serializers
from django.test import TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (AssessmentSession, PresentationGroup,
                                RubricCategory, Student)

from .test_access_matrix import named_patterns

GET, POST = 'get', 'post'

#: Planted in every text field of Org A, so a leak is visible in a response
#: body whatever the template renders.
MARKER = 'ORG-A-CONFIDENTIAL'

#: URL name -> (method, {url kwarg: object key in ``Fixture.objects``}).
#: A bare string stands for {every kwarg: that object}.
TARGETS = {
    'assessment_session_detail':   (GET,  'session'),
    'assessment_session_edit':     (GET,  'session'),
    'assessment_live_state':       (GET,  'session'),
    'assessment_session_delete':   (POST, 'session'),
    'assessment_roster':           (GET,  'session'),
    'assessment_roster_search':    (GET,  'session'),
    'assessment_roster_edit':      (POST, {'pk': 'session', 'student_pk': 'student'}),
    'assessment_roster_delete':    (POST, {'pk': 'session', 'student_pk': 'student'}),
    'assessment_rubric':           (GET,  'session'),
    'assessment_rubric_edit':      (POST, {'pk': 'session', 'category_pk': 'category'}),
    'assessment_rubric_delete':    (POST, {'pk': 'session', 'category_pk': 'category'}),
    'assessment_groups':           (GET,  'session'),
    'assessment_group_import':     (POST, 'session'),
    'assessment_group_edit':       (POST, {'pk': 'session', 'group_pk': 'group'}),
    'assessment_group_members':    (POST, {'pk': 'session', 'group_pk': 'group'}),
    'assessment_group_delete':     (POST, {'pk': 'session', 'group_pk': 'group'}),
    'assessment_go_live':          (POST, 'session'),
    'assessment_activate_group':   (POST, {'pk': 'session', 'group_pk': 'group'}),
    'assessment_set_transition_gap':        (POST, 'session'),
    'assessment_set_default_turn_seconds':  (POST, 'session'),
    'assessment_set_presentation_timer':    (POST, 'session'),
    'assessment_set_next_group':   (POST, 'session'),
    'assessment_toggle_pause':     (POST, 'session'),
    'assessment_toggle_joining':   (POST, 'session'),
    'assessment_adjust_timer':     (POST, 'session'),
    'assessment_close_turn':       (POST, 'session'),
    'assessment_close_session':    (POST, 'session'),
    'assessment_participation':    (GET,  'session'),
    'assessment_participation_refresh': (GET, 'session'),
    'assessment_results':          (GET,  'session'),
    'assessment_results_refresh':  (GET,  'session'),
    'assessment_results_export':   (GET,  'session'),
    'assessment_qr':               (GET,  'session'),
    'assessment_transfer':         (POST, 'session'),
    'assessment_setup':            (GET,  'session'),
    'assessment_group_import_preview': (GET, 'session'),
    'assessment_group_import_sample':  (GET, 'session'),
    'assessment_groups_bulk_delete':   (POST, 'session'),
    'assessment_rubric_import':    (POST, 'session'),
    'assessment_rubric_import_preview': (GET, 'session'),
    'assessment_rubric_import_sample':  (GET, 'session'),
    'assessment_rubric_table':     (GET,  'session'),
}

#: Routes that take an id but are deliberately outside this check, and why.
EXEMPT = {
    'student_join': 'session link, no login',
    'student_evaluate': 'session link, no login',
    'student_submitted': 'session link, no login',
    'student_check_expiry': 'session link, no login',
    'student_grading_ping': 'session link, no login',
    'assessment_join_screen': 'session link, no login',
    'assessment_join_screen_state': 'session link, no login',
    'assessment_join_screen_qr': 'session link, no login',
}

TRACKED = (AssessmentSession, Student, PresentationGroup, RubricCategory)


def snapshot():
    """Every tracked row, serialized - any write, delete or toggle changes it."""
    return {model._meta.label: serializers.serialize('json', model.objects.order_by('pk'))
            for model in TRACKED}


def shape(response):
    """What a caller can tell apart: status, and where a redirect goes with
    the numbers masked, so /x/7/ and /x/999999/ compare equal."""
    location = response.get('Location', '')
    return response.status_code, re.sub(r'\d+', '#', location.split('?')[0])


class RowsAreUnreachable:
    """Every id route, requested by somebody who must not reach Org A's rows:
    the answer is the one a missing id gets, carries none of the data, and
    changes nothing."""

    @classmethod
    def setUpTestData(cls):
        cls.org_a = make_org('Org A')
        cls.org_b = make_org('Org B')
        owner = cls.owner = make_user('a_lecturer')
        session = AssessmentSession.objects.create(
            organization_id=cls.org_a.pk, name=MARKER, created_by=owner)
        cls.objects = {
            'session': session,
            'student': Student.objects.create(assessment_session=session, full_name=MARKER),
            'group': PresentationGroup.objects.create(assessment_session=session, name=MARKER),
            'category': RubricCategory.objects.create(assessment_session=session, name=MARKER),
        }
        cls.lecturer_b = make_user('b_lecturer')
        cls.colleague = make_user('a_colleague')

    def request(self, name, method, kwargs):
        self.client.logout()
        sign_in(self.client, self.intruder, self.intruder_org)
        return getattr(self.client, method)(reverse(name, kwargs=kwargs))

    #: The routes that must be unreachable. All of them, unless a subclass is
    #: about somebody who is meant to reach some.
    targets = TARGETS

    def test_other_rows_are_indistinguishable_from_missing_ones(self):
        patterns = dict(named_patterns())
        problems = []
        for name, (method, mapping) in self.targets.items():
            argument_names = list(patterns[name].pattern.converters)
            if isinstance(mapping, str):
                mapping = {arg: mapping for arg in argument_names}
            real = {arg: self.objects[key].pk for arg, key in mapping.items()}
            missing = {arg: 999999 for arg in mapping}

            before = snapshot()
            got = self.request(name, method, real)
            after = snapshot()
            baseline = self.request(name, method, missing)

            if shape(got) != shape(baseline):
                problems.append(
                    f'{method.upper()} {name}: a real row answered {shape(got)}, '
                    f'a missing one {shape(baseline)} - tells the caller it exists')
            if MARKER.encode() in got.content:
                problems.append(f'{method.upper()} {name}: the owner\'s data in the response')
            if before != after:
                changed = [label for label in before if before[label] != after[label]]
                problems.append(f'{method.upper()} {name}: changed {changed}')
        self.assertEqual(problems, [], f'{self.intruder.username} reached the owner\'s session')


class OtherOrganizationLecturerTests(RowsAreUnreachable, TestCase):
    """A lecturer of Org B tries to reach Org A's session."""

    @property
    def intruder(self):
        return self.lecturer_b

    @property
    def intruder_org(self):
        return self.org_b

    def test_the_owner_can_reach_every_one_of_them(self):
        """The check above means something only if the rows are really there."""
        patterns = dict(named_patterns())
        for name, (method, mapping) in TARGETS.items():
            if method != GET:
                continue
            if isinstance(mapping, str):
                mapping = {arg: mapping for arg in patterns[name].pattern.converters}
            kwargs = {arg: self.objects[key].pk for arg, key in mapping.items()}
            self.client.logout()
            sign_in(self.client, self.owner, self.org_a)
            with self.subTest(name):
                response = self.client.get(reverse(name, kwargs=kwargs))
                self.assertNotEqual(response.status_code, 404)


class ColleagueInTheSameOrganizationTests(RowsAreUnreachable, TestCase):
    """Another lecturer - or an admin, who is the same thing here - in the SAME
    organization tries to reach it. A session is private to its creator."""

    @property
    def intruder(self):
        return self.colleague

    @property
    def intruder_org(self):
        return self.org_a


#: What somebody with oversight is meant to reach on a session that is not theirs.
OVERSIGHT_ROUTES = {
    'assessment_results', 'assessment_results_refresh',
    'assessment_participation', 'assessment_participation_refresh',
    'assessment_transfer',
}


class OverseerOfAnotherOrganizationTests(RowsAreUnreachable, TestCase):
    """Oversight is for ONE organization: an overseer of Org B reaches none of
    Org A's rows, not even the read-only ones."""

    @property
    def intruder(self):
        return self.lecturer_b

    @property
    def intruder_org(self):
        return self.__class__.overseeing_b

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.overseeing_b = make_org('Org B overseers', oversee=True)


class OverseerInTheSameOrganizationTests(RowsAreUnreachable, TestCase):
    """An overseer in the SAME organization reaches only the read-only pages and
    the hand-over. Everything else about someone else's session stays private."""

    targets = {name: spec for name, spec in TARGETS.items() if name not in OVERSIGHT_ROUTES}

    @property
    def intruder(self):
        return self.colleague

    @property
    def intruder_org(self):
        return self.__class__.overseeing_a

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.overseeing_a = make_org('Org A overseers', oversee=True)
        # Same organization number as Org A, with oversight switched on.
        cls.overseeing_a.pk = cls.overseeing_a.id = cls.org_a.pk

    def test_the_read_only_pages_are_reachable(self):
        sign_in(self.client, self.colleague, self.overseeing_a)
        session = self.objects['session']
        for name in ('assessment_results', 'assessment_results_refresh',
                     'assessment_participation', 'assessment_participation_refresh',
                     'assessment_transfer'):
            with self.subTest(name):
                self.assertEqual(
                    self.client.get(reverse(name, args=[session.pk])).status_code, 200)


class EveryRowRouteIsCheckedTests(TestCase):
    """A new URL that takes a row id fails here until it is in ``TARGETS``
    (checked) or ``EXEMPT`` (with the reason)."""

    def test_every_route_with_an_id_is_covered(self):
        takes_an_id = {name for name, pattern in named_patterns() if pattern.pattern.converters}
        self.assertEqual(
            sorted(takes_an_id - set(TARGETS) - set(EXEMPT)), [],
            'Add these to TARGETS (or EXEMPT, with a reason) in '
            'assessments/tests/test_cross_org_access.py')

    def test_no_entry_names_a_route_that_is_gone(self):
        names = {name for name, _ in named_patterns()}
        self.assertEqual(sorted((set(TARGETS) | set(EXEMPT)) - names), [])

    def test_targets_and_exempt_do_not_overlap(self):
        self.assertEqual(sorted(set(TARGETS) & set(EXEMPT)), [])

    def test_every_target_names_real_url_arguments(self):
        patterns = dict(named_patterns())
        for name, (method, mapping) in TARGETS.items():
            arguments = set(patterns[name].pattern.converters)
            if isinstance(mapping, dict):
                self.assertEqual(set(mapping), arguments, name)
