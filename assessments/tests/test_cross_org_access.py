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
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import serializers
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (AssessmentSession, PresentationGroup,
                                RubricCategory, SessionTemplate, Student, TemplateRubricCategory)
from service_domains.models import (Competition, PanelCandidate, PanelCriterion, PanelHearing,
                                    PanelInvitation, PanelResultAdjustment, PanelResultSnapshot,
                                    PanelScore, ShowcaseEvent, ShowcaseExhibit,
                                    CombinedPresentationAssessment, TrainingSeries)
from portal.services import is_released

from .test_access_matrix import named_patterns

GET, POST = 'get', 'post'

#: Planted in every text field of Org A, so a leak is visible in a response
#: body whatever the template renders.
MARKER = 'ORG-A-CONFIDENTIAL'

#: URL name -> (method, {url kwarg: object key in ``Fixture.objects``}).
#: A bare string stands for {every kwarg: that object}.
TARGETS = {
    'assessment_polls': (GET, 'session'),
    'assessment_poll_questions_sample': (GET, 'session'),
    'assessment_leaderboard_scores': (POST, 'session'),
    'showcase_event_labels': (GET, 'showcase_event'),
    'competition_manage': (GET, 'competition'),
    'panel_hearing_manage': (GET, 'panel_hearing'),
    'panel_candidate_import': (GET, 'draft_panel_hearing'),
    'panel_candidate_import_sample': (GET, 'draft_panel_hearing'),
    'panel_rubric_import': (GET, 'draft_panel_hearing'),
    'panel_rubric_import_sample': (GET, 'draft_panel_hearing'),
    'panel_invitation_import': (GET, 'draft_panel_hearing'),
    'panel_invitation_import_sample': (GET, 'draft_panel_hearing'),
    'panel_results_review': (GET, 'panel_hearing'),
    'panel_recusal_export': (GET, 'panel_hearing'),
    'panel_results_export': (GET, 'panel_hearing'),
    'combined_assessment_detail': (GET, 'combined_assessment'),
    'showcase_live_monitor': (GET, 'showcase_event'),
    'showcase_live_activity': (GET, 'showcase_event'),
    'training_series_roster_import': (GET, 'training_series'),
    'training_series_roster_preview': (GET, 'training_series'),
    'training_series_roster_sample': (GET, 'training_series'),
    'assessment_session_detail':   (GET,  'session'),
    'assessment_session_duplicate': (POST, 'session'),
    'assessment_template_save': (POST, 'session'),
    'assessment_template_delete': (POST, 'template'),
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
    'assessment_public_results':   (POST, 'session'),
    'assessment_participation':    (GET,  'session'),
    'assessment_participation_refresh': (GET, 'session'),
    'assessment_results':          (GET,  'session'),
    'assessment_results_refresh':  (GET,  'session'),
    'assessment_results_export':   (GET,  'session'),
    'assessment_results_export_csv': (GET, 'session'),
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
    'assessment_rubric_bulk_delete': (POST, 'session'),
}

#: Routes that take an id but are deliberately outside this check, and why.
EXEMPT = {
    'classroom_poll': 'public UUID identifies a published poll; roster or personal-link identity is required to vote or see totals',
    'classroom_poll_results': 'public poll UUID reveals aggregate results only; classroom poll result privacy is covered by poll tests',
    'classroom_poll_qr': 'published poll UUID QR contains only the shared poll URL',
    'classroom_poll_invitation': 'revocable personal poll invitation; roster, token and poll binding are tested in test_classroom_polls',
    'standalone_poll_live_results': 'public standalone poll UUID reveals aggregate results only; standalone poll tests cover publication and privacy',
    'standalone_poll_live_state': 'public standalone poll UUID reveals aggregate state only; standalone poll tests cover result projection',
    'standalone_poll_public': 'public standalone poll UUID is a voting page; anonymous ballot uniqueness and identity privacy are covered by poll tests',
    'standalone_poll_qr': 'public standalone poll UUID QR contains only its shared voting URL',
    'standalone_poll_state': 'owner-only standalone poll state is scoped by organization and creator; standalone poll tests cover ownership',
    'student_join': 'session link, no login',
    'student_privacy_notice': 'session link, no login; notice version is scoped to the joined participant',
    'student_evaluate': 'session link, no login',
    'student_submitted': 'session link, no login',
    'student_check_expiry': 'session link, no login',
    'student_grading_ping': 'session link, no login',
    'assessment_join_screen': 'session link, no login',
    'assessment_join_screen_state': 'session link, no login',
    'assessment_join_screen_qr': 'session link, no login',
    'showcase_rate_exhibit': 'public exhibit UUID link; event-bound HttpOnly device cookie and per-exhibit uniqueness tests',
    'showcase_exhibit_label': 'public exhibit UUID link used to print its visitor QR label',
    'showcase_exhibit_qr': 'public exhibit UUID link embeds only its public rating URL in a QR image',
    'showcase_public_results': 'public event UUID results are available only after explicit publication',
    'showcase_certificate_verify': 'public certificate UUID verifies only the immutable award record it identifies',
    'training_anonymous_feedback': 'high-entropy Team demo UUID with an event-bound signed HttpOnly token; feedback has no owner or learner identity field',
    'training_certificate_verify': 'public certificate UUID verifies a minimal immutable outcome snapshot; Team certificate tests verify current versus superseded status',
    'competition_audience_vote': 'high-entropy competition UUID plus round/track, event-bound signed HttpOnly token, one anonymous ballot per browser for the round and track',
    'competition_judge_scorecard': 'opaque one-track bearer invitation, scoped to one active round and revoked or expired with owner controls; judge-flow tests verify it',
    'competition_public_leaderboard': 'public competition UUID; route reveals only frozen judge scores after an owner transition, verified by Competition workspace tests',
    'panel_examiner': 'high-entropy bearer invitation; only its own hearing/candidates are reachable and expiry/revocation/recusal are enforced by Panel examiner tests',
    'showcase_judge_workspace': 'opaque per-event judge bearer invitation; only assigned exhibits are exposed, and expired/revoked links are denied in Showcase judge-flow tests',
    'panel_entrant_results': 'opaque revocable bearer capability; exposes only its candidate released aggregate results and is covered by Panel entrant route tests',
    'panel_oversight_detail': 'organization-wide read-only permission; dedicated same-organization and cross-organization oversight tests cover it',
    'showcase_event_manage': 'unreleased service owner route; owner/org denial is exercised by service-domain tests',
    'showcase_event_transition': 'unreleased service owner route; owner/org denial is exercised by service-domain tests',
    'showcase_exhibit_import': 'unreleased service owner route; owner/org denial is exercised by service-domain tests',
    'showcase_exhibit_import_sample': 'unreleased service owner route; owner/org denial is exercised by service-domain tests',
    'training_series_manage': 'unreleased service owner route; owner/org denial is exercised by service-domain tests',
    'training_session_competencies': 'Team-only session outcome route; owner/org/service denial is exercised by service-domain tests',
    'training_series_trends': 'Team-only series trend route; owner/org/service denial is exercised by service-domain tests',
    'training_series_export': 'Team-only competency export; owner/org/service denial is exercised by service-domain tests',
    'service_entry': 'a service slug from the registry, not a row id',
    'service_interest': 'public notification collection keyed by registry slug, no session row; portal interest tests',
    'service_switch_organization': 'registry slug, with DevPerf membership checked separately; portal shell tests',
    'legacy_assessments_path': 'redirect only, no row access; destination retains all owner scopes; portal legacy tests',
}

TRACKED = (AssessmentSession, Student, PresentationGroup, RubricCategory, SessionTemplate,
           TemplateRubricCategory, PanelHearing, PanelCandidate, PanelCriterion,
           PanelInvitation, PanelScore, PanelResultSnapshot, PanelResultAdjustment,
           Competition, ShowcaseEvent, ShowcaseExhibit)


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
            'template': SessionTemplate.objects.create(organization_id=cls.org_a.pk, service='classroom',
                                                       created_by=owner, name=MARKER),
            'student': Student.objects.create(assessment_session=session, full_name=MARKER),
            'group': PresentationGroup.objects.create(assessment_session=session, name=MARKER),
            'category': RubricCategory.objects.create(assessment_session=session, name=MARKER),
            'panel_hearing': PanelHearing.objects.create(organization_id=cls.org_a.pk,
                created_by=owner, name=MARKER, status=PanelHearing.Status.REVIEW),
            'draft_panel_hearing': PanelHearing.objects.create(organization_id=cls.org_a.pk,
                created_by=owner, name='Draft candidate import', status=PanelHearing.Status.DRAFT),
            'competition': Competition.objects.create(organization_id=cls.org_a.pk,
                created_by=owner, name=MARKER),
            'showcase_event': ShowcaseEvent.objects.create(organization_id=cls.org_a.pk,
                created_by=owner, name=MARKER, status=ShowcaseEvent.Status.OPEN),
            'training_series': TrainingSeries.objects.create(
                organization_id=cls.org_a.pk, created_by=owner, name=MARKER),
        }
        cls.objects['combined_assessment'] = CombinedPresentationAssessment.objects.create(
            organization_id=cls.org_a.pk, created_by=owner, name=MARKER,
            classroom_session=session, panel_hearing=cls.objects['panel_hearing'])
        cls.objects['panel_candidate'] = PanelCandidate.objects.create(
            organization_id=cls.org_a.pk, created_by=owner,
            hearing=cls.objects['panel_hearing'], display_name='Panel candidate marker')
        cls.objects['panel_criterion'] = PanelCriterion.objects.create(
            organization_id=cls.org_a.pk, created_by=owner,
            hearing=cls.objects['panel_hearing'], name='Panel criterion marker')
        cls.objects['panel_snapshot'] = PanelResultSnapshot.objects.create(
            organization_id=cls.org_a.pk, created_by=owner,
            hearing=cls.objects['panel_hearing'], candidate=cls.objects['panel_candidate'],
            criterion=cls.objects['panel_criterion'], score=Decimal('82.5'), rater_count=1,
            aggregation=PanelHearing.Aggregation.MEAN)
        first_adjustment = PanelResultAdjustment.objects.create(
            organization_id=cls.org_a.pk, created_by=owner,
            snapshot=cls.objects['panel_snapshot'], adjusted_score=Decimal('83'),
            reason='Initial correction marker', revision=1, adjusted_by=owner)
        cls.objects['panel_adjustment'] = PanelResultAdjustment.objects.create(
            organization_id=cls.org_a.pk, created_by=owner,
            snapshot=cls.objects['panel_snapshot'], adjusted_score=Decimal('83.5'),
            reason='Latest correction marker', revision=2, supersedes=first_adjustment,
            adjusted_by=owner)
        invitation = PanelInvitation.objects.create(
            organization_id=cls.org_a.pk, created_by=owner,
            hearing=cls.objects['panel_hearing'], email='EXAMINER-PRIVATE@example.test',
            expires_at=timezone.now() + timedelta(hours=1))
        cls.objects['panel_score'] = PanelScore.objects.create(
            organization_id=cls.org_a.pk, created_by=owner,
            invitation=invitation, candidate=cls.objects['panel_candidate'],
            criterion=cls.objects['panel_criterion'], score=Decimal('82.5'),
            comment='EXAMINER-COMMENT-PRIVATE')
        cls.lecturer_b = make_user('b_lecturer')
        cls.colleague = make_user('a_colleague')

    def request(self, name, method, kwargs):
        self.client.logout()
        sign_in(self.client, self.intruder, self.intruder_org)
        with patch('portal.middleware.is_released',
                   side_effect=lambda slug: slug in ('panel', 'competition', 'showcase', 'team') or is_released(slug)):
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

    def test_competition_advance_post_is_scoped_like_a_missing_competition(self):
        before = snapshot()
        real = self.request('competition_manage', POST,
            {'competition_id': self.objects['competition'].pk})
        after = snapshot()
        missing = self.request('competition_manage', POST, {'competition_id': 999999})
        self.assertEqual(shape(real), shape(missing))
        self.assertNotIn(MARKER.encode(), real.content)
        self.assertEqual(before, after)


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
                with patch('portal.middleware.is_released',
                           side_effect=lambda slug: slug in ('panel', 'competition', 'showcase', 'team') or is_released(slug)):
                    response = self.client.get(reverse(name, kwargs=kwargs))
                self.assertNotEqual(response.status_code, 404)

    def test_other_organization_cannot_release_public_classroom_results(self):
        before = snapshot()
        self.client.logout()
        sign_in(self.client, self.intruder, self.intruder_org)
        real = self.client.post(reverse('assessment_public_results',
            args=[self.objects['session'].pk]), {'action': 'release'})
        after = snapshot()
        missing = self.client.post(reverse('assessment_public_results', args=[999999]),
            {'action': 'release'})
        self.assertEqual(shape(real), shape(missing))
        self.assertNotIn(MARKER.encode(), real.content)
        self.assertEqual(before, after)


class ColleagueInTheSameOrganizationTests(RowsAreUnreachable, TestCase):
    """Another lecturer - or an admin, who is the same thing here - in the SAME
    organization tries to reach it. A session is private to its creator."""

    @property
    def intruder(self):
        return self.colleague

    @property
    def intruder_org(self):
        return self.org_a

    def test_panel_oversight_list_requires_devperf_oversight_permission(self):
        sign_in(self.client, self.colleague, self.org_a)
        with patch('portal.middleware.is_released',
                   side_effect=lambda slug: slug == 'panel' or is_released(slug)):
            response = self.client.get(reverse('panel_oversight_list'))
            owner_workspace = self.client.get(reverse('panel_hearing_list'))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(owner_workspace, 'View compiled hearings for this organization')

    def test_panel_oversight_detail_requires_devperf_oversight_permission(self):
        sign_in(self.client, self.colleague, self.org_a)
        url = reverse('panel_oversight_detail', args=[self.objects['panel_hearing'].pk])
        with patch('portal.middleware.is_released',
                   side_effect=lambda slug: slug == 'panel' or is_released(slug)):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


#: What somebody with oversight is meant to reach on a session that is not theirs.
OVERSIGHT_ROUTES = {
    'assessment_results', 'assessment_results_refresh',
    'assessment_participation', 'assessment_participation_refresh',
    'assessment_transfer',
    'panel_oversight_detail',
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

    def test_panel_oversight_cannot_cross_organization_boundary(self):
        sign_in(self.client, self.lecturer_b, self.overseeing_b)
        url = reverse('panel_oversight_detail', args=[self.objects['panel_hearing'].pk])
        with patch('portal.middleware.is_released',
                   side_effect=lambda slug: slug == 'panel' or is_released(slug)):
            real = self.client.get(url)
            missing = self.client.get(reverse('panel_oversight_detail', args=[999999]))
        self.assertEqual(shape(real), shape(missing))
        self.assertNotIn(b'Panel candidate marker', real.content)


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

    def test_panel_oversight_shows_organization_aggregates_without_examiner_data(self):
        sign_in(self.client, self.colleague, self.overseeing_a)
        detail_url = reverse('panel_oversight_detail',
            args=[self.objects['panel_hearing'].pk])
        with patch('portal.middleware.is_released',
                   side_effect=lambda slug: slug == 'panel' or is_released(slug)):
            listing = self.client.get(reverse('panel_oversight_list'))
            detail = self.client.get(detail_url)
            owner_workspace = self.client.get(reverse('panel_hearing_list'))
            denied_write = self.client.post(detail_url, {'action': 'release'})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(owner_workspace, reverse('panel_oversight_list'))
        self.assertContains(detail, 'Panel candidate marker')
        self.assertContains(detail, 'Panel criterion marker')
        self.assertContains(detail, '82.500000')
        self.assertContains(detail, '83.500000')
        self.assertContains(detail, 'Latest correction marker')
        self.assertContains(detail, '<td>1</td>', html=False)
        self.assertNotContains(detail, 'EXAMINER-PRIVATE@example.test')
        self.assertNotContains(detail, 'EXAMINER-COMMENT-PRIVATE')
        self.assertIn('private', detail['Cache-Control'])
        self.assertIn('no-store', detail['Cache-Control'])
        self.assertEqual(detail['Referrer-Policy'], 'no-referrer')
        self.assertEqual(denied_write.status_code, 405)

    def test_panel_oversight_list_is_scoped_and_only_lists_compiled_records(self):
        sign_in(self.client, self.colleague, self.overseeing_a)
        url = reverse('panel_oversight_list')
        with patch('portal.middleware.is_released',
                   side_effect=lambda slug: slug == 'panel' or is_released(slug)):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, MARKER)
        self.assertNotContains(response, 'Draft candidate import')
        self.assertNotContains(response, 'EXAMINER-PRIVATE@example.test')


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
