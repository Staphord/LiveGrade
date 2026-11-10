"""The services LiveGrade offers, and which of them are open.

LiveGrade is one platform. A visitor chooses a service at its entry address
(``/classroom/``, ``/panel/`` ...), signs in or registers, and lands on that
service's dashboard; nothing inside a service lists the others. The services are
data, defined once here (docs/adr/ADR-001-service-model.md): their names, promises and
vocabulary can change without touching the code that routes and scopes them.

A service is *open* only when it is switched on in configuration and has a
real dashboard to land on. Everything else is shown as coming soon and cannot
be entered.
"""
from dataclasses import dataclass
from types import MappingProxyType

from django.conf import settings
from django.urls import reverse


# The server-side choice made at a service entry address, carried through DevPerf
# sign-in and consumed by the callback. This is never populated from a request field.
CHOICE_KEY = 'livegrade_service'


def _words(session, participants, entries, criteria, owner):
    """The nouns a service uses for the same parts of the engine."""
    return MappingProxyType({
        'session': session, 'participants': participants, 'entries': entries,
        'criteria': criteria, 'owner': owner,
    })


@dataclass(frozen=True)
class Service:
    slug: str
    name: str
    promise: str
    audience: str
    vocabulary: MappingProxyType
    #: URL name of the page a person lands on, or None while the service has none.
    dashboard: str = None


SERVICES = (
    Service('classroom', 'Classroom',
            'Grade presentations in your class, live, from your students’ phones.',
            'Teachers',
            _words('session', 'students', 'groups', 'rubric', 'lecturer'),
            dashboard='assessment_dashboard'),
    Service('panel', 'Panel',
            'Run a fair, recorded panel decision: invite examiners, score against agreed criteria, keep the record.',
            'Chairs and coordinators of defences, committees and interviews',
            _words('hearing', 'candidates', 'candidates', 'criteria', 'chair'),
            dashboard='panel_hearing_list'),
    Service('showcase', 'Showcase',
            'Let visitors and judges rate exhibits and posters, and announce winners the same day.',
            'Organisers of fairs, expos, conferences and open days',
            _words('showcase', 'visitors', 'exhibits', 'criteria', 'organiser'),
            dashboard='showcase_event_list'),
    Service('team', 'Team and Training',
            'Run recurring demos and capstones: structured feedback, progress over time, competency sign-off.',
            'Team leaders, trainers and academies',
            _words('series', 'team members', 'demos', 'criteria', 'team leader'),
            dashboard='training_series_list'),
    Service('competition', 'Competition',
            'Judge a competition with confidence: private judge scoring, audience vote, live leaderboard, rounds.',
            'Directors of hackathons, pitch events, talent shows and festivals',
            _words('competition', 'contestants', 'entries', 'scorecard', 'director'),
            dashboard='competition_list'),
    Service('polls', 'Anonymous Polls',
            'Create and share anonymous polls, then review live aggregate results.',
            'Teachers, facilitators and event organisers',
            _words('poll', 'respondents', 'responses', 'questions', 'organiser'),
            dashboard='standalone_poll_list'),
)

_BY_SLUG = {service.slug: service for service in SERVICES}
SLUGS = tuple(_BY_SLUG)


def get_service(slug):
    """The service with this slug, or None. Anything that is not a registered slug is None."""
    return _BY_SLUG.get(slug) if isinstance(slug, str) else None


def is_released(slug):
    """Whether the service can be entered and has a dashboard to land on."""
    service = get_service(slug)
    return bool(service and service.dashboard and slug in settings.LIVEGRADE_LIVE_SERVICES)


def released_services():
    return [service for service in SERVICES if is_released(service.slug)]


def dashboard_url(service):
    """Where a person lands in ``service``; the service must be released."""
    return reverse(service.dashboard)
