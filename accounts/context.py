from django.conf import settings
from django.urls import reverse

from .access import ORGS_KEY, active_organization, can_oversee
from portal.services import dashboard_url, get_service, is_released


PERSONAL = 'Personal'
CLASSROOM_WALKTHROUGHS_KEY = 'livegrade_classroom_walkthroughs'


def _classroom_walkthrough(request, organization):
    if (getattr(request, 'service', None) != 'classroom'
            or not getattr(request.user, 'is_authenticated', False)
            or not organization):
        return None
    walkthroughs = request.session.get(CLASSROOM_WALKTHROUGHS_KEY, {})
    if not isinstance(walkthroughs, dict):
        return None
    state = walkthroughs.get(str(organization['id']))
    if not isinstance(state, dict):
        return None
    from assessments.models import AssessmentSession

    session_id = state.get('session_id')
    session = None
    if isinstance(session_id, int) and not isinstance(session_id, bool):
        # The browser session only points to a candidate. Reapply owner,
        # organization and service scope before showing any session detail.
        session = AssessmentSession.objects.filter(
            organization_id=organization['id'], created_by_id=request.user.pk,
            service='classroom', pk=session_id,
        ).first()
    if session is None:
        progress = [False, False, False, False]
        missing = session_id is not None
    else:
        has_roster_and_group = (session.students.exists()
                                and session.groups.filter(is_practice=False).exists())
        ready_to_launch = session.can_go_live()
        completed = session.status in (AssessmentSession.Status.LIVE,
                                       AssessmentSession.Status.CLOSED)
        progress = [True, has_roster_and_group, ready_to_launch, completed]
        missing = False
    current_step = next((i for i, complete in enumerate(progress, 1) if not complete), 4)
    if progress == [True, True, True, True]:
        current_step = 4
    steps = [
        {'number': 1, 'title': 'Create your first session', 'done': progress[0],
        'url': reverse('assessment_session_create'), 'action': 'Create session'},
        {'number': 2, 'title': 'Add your learners and groups', 'done': progress[1],
         'url': reverse('assessment_setup', args=[session.pk]) if session else reverse('assessment_session_create'),
         'action': 'Add learners and groups'},
        {'number': 3, 'title': 'Set your scoring rubric', 'done': progress[2],
         'url': reverse('assessment_rubric', args=[session.pk]) if session else reverse('assessment_session_create'),
         'action': 'Build the rubric'},
        {'number': 4, 'title': 'Review and start the session', 'done': progress[3],
         'url': reverse('assessment_session_detail', args=[session.pk]) if session else reverse('assessment_session_create'),
         'action': 'Review session'},
    ]
    current = steps[current_step - 1]
    return {
        'session': session, 'steps': steps, 'current_step': current_step,
        'current_title': current['title'], 'current_action_url': current['url'],
        'current_action': current['action'], 'completed': progress == [True, True, True, True],
        'progress_percent': round(sum(progress) * 100 / len(progress)),
        'session_missing': missing,
    }


def _sessions_home(request, organization):
    """Where the shared assessment pages send "back": the classroom session list
    for Classroom, and the demo's own workshop (or the series list) for Team, so a
    Team page never links into another service's records."""
    if getattr(request, 'service', None) != 'team':
        return {'url': reverse('assessment_session_list'), 'label': 'All sessions'}
    match = getattr(request, 'resolver_match', None)
    pk = match.kwargs.get('pk') if match and (match.url_name or '').startswith('assessment_') else None
    if pk and organization and getattr(request.user, 'is_authenticated', False):
        from service_domains.models import TrainingSeriesSession

        link = (TrainingSeriesSession.objects.filter(
            organization_id=organization['id'], created_by_id=request.user.pk,
            assessment_session_id=pk).first())
        if link is not None:
            return {'url': reverse('training_session_live', args=(link.series_id, link.pk)),
                    'label': 'Back to live workshop'}
    return {'url': reverse('training_series_list'), 'label': 'Training series'}


def livegrade(request):
    """The pieces every page's header needs.

    The shared workspace for self-registered lecturers is not an organization
    to them: on its own it is not shown at all, and next to a real one it is
    called "Personal".
    """
    organizations = request.session.get(ORGS_KEY) or []
    slug = getattr(request, 'service', None)
    service = get_service(slug) if is_released(slug) else None
    organization = active_organization(request)
    return {
        'current_service': service,
        'service_vocabulary': service.vocabulary if service else {},
        'service_dashboard': dashboard_url(service) if service else None,
        # The nonce that lets this response's inline scripts run under the CSP.
        'csp_nonce': getattr(request, 'csp_nonce', ''),
        'devperf_url': settings.DEVPERF_URL,
        'active_org': organization,
        'can_oversee_panel': can_oversee(request),
        'classroom_walkthrough': _classroom_walkthrough(request, organization),
        'sessions_home': _sessions_home(request, organization),
        'my_orgs': [{**org, 'display_name': PERSONAL if org.get('public') else org['name']}
                    for org in organizations],
        # nosemgrep: python.django.security.nan-injection.nan-injection (bool() of a list, not a number cast)
        'only_personal': bool(organizations) and all(org.get('public') for org in organizations),
    }
