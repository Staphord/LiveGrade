"""Who can open what: every URL, as every kind of visitor.

Three visitors: somebody not signed in, somebody signed in whom DevPerf says may
not run LiveGrade anywhere, and a lecturer. Ids point at nothing, so a lecturer
getting through is a 404 (or a page, for the URLs without an id) and being
stopped is a login redirect or a 403.

A URL cannot slip past: ``MatrixCoversEveryUrlTests`` fails until each named URL
has a row here.
"""
import uuid

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import URLResolver, get_resolver, reverse

from accounts.testing import make_org, make_user, sign_in

GET, POST = 'get', 'post'
LOGIN = 'login'  # redirected to the sign-in page
ACTORS = ('anonymous', 'unauthorised', 'lecturer')

MISSING = {'int': 999999, 'uuid': uuid.UUID('00000000-0000-4000-8000-000000000000')}

LECTURER_PAGES = {
    # name: (method, anonymous, unauthorised, lecturer)
    'assessment_session_list':   (GET, LOGIN, 403, 200),
    'assessment_session_create': (GET, LOGIN, 403, 200),
}
LECTURER_ROW_ROUTES = {
    'assessment_session_edit': GET, 'assessment_session_detail': GET,
    'assessment_live_state': GET, 'assessment_session_delete': POST,
    'assessment_roster': GET, 'assessment_roster_search': GET,
    'assessment_roster_edit': POST,
    'assessment_roster_delete': POST, 'assessment_rubric': GET,
    'assessment_rubric_edit': POST, 'assessment_rubric_delete': POST,
    'assessment_groups': GET, 'assessment_group_import': POST,
    'assessment_group_edit': POST, 'assessment_group_members': POST,
    'assessment_group_delete': POST, 'assessment_go_live': POST,
    'assessment_activate_group': POST, 'assessment_set_transition_gap': POST,
    'assessment_set_default_turn_seconds': POST, 'assessment_set_presentation_timer': POST,
    'assessment_set_next_group': POST, 'assessment_toggle_joining': POST,
    'assessment_toggle_pause': POST, 'assessment_adjust_timer': POST,
    'assessment_close_turn': POST, 'assessment_close_session': POST,
    'assessment_participation': GET, 'assessment_participation_refresh': GET,
    'assessment_results': GET, 'assessment_results_refresh': GET,
    'assessment_results_export': GET, 'assessment_qr': GET, 'assessment_transfer': GET,
    'assessment_setup': GET, 'assessment_group_import_preview': GET,
    'assessment_group_import_sample': GET, 'assessment_groups_bulk_delete': POST,
    'assessment_rubric_import': POST, 'assessment_rubric_import_preview': GET,
    'assessment_rubric_import_sample': GET, 'assessment_rubric_table': GET,
}
STUDENT_ROUTES = {
    # Reached by session link, no login: an unknown link is a 404 for everybody.
    'assessment_join_screen': GET, 'assessment_join_screen_state': GET,
    'assessment_join_screen_qr': GET, 'student_join': GET, 'student_evaluate': GET,
    'student_submitted': GET, 'student_check_expiry': POST, 'student_grading_ping': POST,
}
ACCOUNT_ROUTES = {
    # LiveGrade's own sign-in and housekeeping pages.
    'home': (GET, 200, 403, '/assessments/'),
    'no_access': (GET, 403, 403, 403),
    'switch_account': (GET, 405, 405, 405),
    'switch_organization': (GET, LOGIN, 403, 405),
    'health': (GET, 200, 200, 200),
    'oidc_authentication_callback': (GET, '/no-access/', '/no-access/', '/no-access/'),
    'oidc_authentication_init': (GET, 'idp', 'idp', 'idp'),
    'oidc_logout': (GET, 405, 405, 405),
}

URL_MATRIX = {**LECTURER_PAGES}
URL_MATRIX.update({name: (method, LOGIN, 403, 404) for name, method in LECTURER_ROW_ROUTES.items()})
URL_MATRIX.update({name: (method, 404, 404, 404) for name, method in STUDENT_ROUTES.items()})
URL_MATRIX.update(ACCOUNT_ROUTES)


def named_patterns(resolver=None):
    """Every named URL in the project."""
    for pattern in (resolver or get_resolver()).url_patterns:
        if isinstance(pattern, URLResolver):
            yield from named_patterns(pattern)
        elif pattern.name:
            yield pattern.name, pattern


def url_arguments(pattern):
    """The pattern's arguments, each pointing at nothing."""
    return {
        name: MISSING.get(type(converter).__name__.replace('Converter', '').lower(), 'x')
        for name, converter in pattern.pattern.converters.items()
    }


def outcome(response):
    if response.status_code not in (301, 302):
        return response.status_code
    target = response['Location'].split('?')[0]
    if target == reverse('oidc_authentication_init'):
        return LOGIN
    if target.startswith(settings.OIDC_OP_AUTHORIZATION_ENDPOINT):
        return 'idp'
    return target


class AccessMatrix:
    actor = None

    def setUp(self):
        self.user = None
        if self.actor != 'anonymous':
            self.user = make_user(self.actor)
        self.org = make_org('Matrix Org')

    def fetch(self, method, url):
        self.client.logout()
        if self.actor == 'unauthorised':
            sign_in(self.client, self.user)
        elif self.actor == 'lecturer':
            sign_in(self.client, self.user, self.org)
        return outcome(getattr(self.client, method)(url))

    def test_the_door_of_every_url(self):
        patterns = dict(named_patterns())
        moved = []
        for name, (method, *outcomes) in URL_MATRIX.items():
            expected = dict(zip(ACTORS, outcomes))[self.actor]
            url = reverse(name, kwargs=url_arguments(patterns[name]))
            got = self.fetch(method, url)
            if got != expected:
                moved.append(f'{method.upper()} {name}: expected {expected}, got {got}')
        self.assertEqual(moved, [], f'Access moved for {self.actor}')


class AnonymousAccessMatrixTests(AccessMatrix, TestCase):
    actor = 'anonymous'


class UnauthorisedAccessMatrixTests(AccessMatrix, TestCase):
    actor = 'unauthorised'


class LecturerAccessMatrixTests(AccessMatrix, TestCase):
    actor = 'lecturer'


class MatrixCoversEveryUrlTests(SimpleTestCase):
    """A new URL cannot slip past the matrix: it fails here until it has a row."""

    def test_every_named_url_has_a_row(self):
        self.assertEqual(
            sorted(set(dict(named_patterns())) - set(URL_MATRIX)), [],
            'Add these URLs to URL_MATRIX in assessments/tests/test_access_matrix.py')

    def test_no_row_names_a_url_that_is_gone(self):
        self.assertEqual(sorted(set(URL_MATRIX) - set(dict(named_patterns()))), [])
