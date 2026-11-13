import csv
import hashlib
import hmac
import io
import logging
import re
import secrets
from collections import Counter, defaultdict
from datetime import time
from decimal import Decimal
from email.utils import formataddr, parseaddr
from urllib.parse import quote
from openpyxl import Workbook

from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Count, Avg, Q, OuterRef, Subquery, Value, Sum
from django.db.models.functions import Coalesce
from django.db import transaction
from functools import wraps

from django.core import signing
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse, QueryDict
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe, require_POST
from django.utils import timezone
from django.utils.cache import patch_vary_headers

from accounts.access import can_oversee, lecturer_required
from assessments.models import AssessmentSession, SessionTemplate
from assessments.pagination import PAGE_SIZE, PAGE_SIZES, paginate
from assessments.qr import public_url
from assessments.session_templates import starters_for
from .forms import ShowcaseRatingForm
from .models import (
    SHOWCASE_DEVICE_TOKEN_MAX_AGE, ShowcaseCategory, ShowcaseCertificate, ShowcaseEvent,
    ShowcaseConsentAudit, ShowcaseEventAudit, ShowcaseExhibit, ShowcaseRating, TrainingFeedback,
    TrainingSeriesSession, TrainingSessionCompetency,
    ShowcaseJudgeInvitation, ShowcaseJudgeExhibitAssignment, ShowcaseJudgeCriterion,
    ShowcaseJudgeEvaluation, ShowcaseJudgeConflict,
)
from .models import (Competition, CompetitionAudienceBallot, CompetitionRound,
                     CompetitionRoundEntry, CompetitionTrack)
from .models import (CombinedPresentationAssessment,
                     PanelAbstention, PanelAuditEvent, PanelCandidate, PanelConflict, PanelCriterion,
                     PanelCriterionLevel,
                     PanelCommentRedaction, PanelEntrantResultLink, PanelLiveDisplayLink,
                     PanelHearing, PanelInvitation,
                     PanelScoringPanel, PanelResultAdjustment,
                     PanelInvitationAttempt, PanelResultSnapshot, PanelScore,
                     PanelDecision, PanelVote, PanelWrittenReview)
from .forms import (
    ShowcaseEventForm, ShowcaseEventTransitionForm, ShowcaseImportApplyForm,
    ShowcaseRubricImportForm, SpreadsheetImportForm,
    ShowcaseImportForm, ShowcaseMinorConsentForm,
    ShowcaseJudgeInvitationForm, ShowcaseJudgeCriterionForm, ShowcaseJudgeScorecardForm,
    ShowcaseJudgeConflictForm,
    CompetitionAudienceBallotForm,
)
from .forms import (PanelCandidateForm, PanelCandidateImportApplyForm, PanelCandidateImportForm,
                    PanelInvitationImportForm, PanelInvitationImportApplyForm,
                    PanelRubricImportForm, PanelRubricImportApplyForm,
                    PanelCriterionForm, PanelHearingForm, PanelScoringPanelForm,
                    PanelCommentRedactionForm, PanelCommentReleaseForm,
                    PanelInvitationForm, PanelResultAdjustmentForm)
from .forms import PanelDecisionForm, PanelVoteForm, PanelWrittenReviewForm
from .operations import (confirm_showcase_minor_consent,
                         delete_panel_hearings,
                         delete_showcase_events,
                         showcase_judge_coverage,
                         submit_showcase_rating, transition_showcase_event,
                         issue_showcase_judge_invitation,
                         replace_showcase_invited_judge_assignments,
                         save_showcase_judge_criterion, showcase_rubric_total,
                         resolve_showcase_judge_invitation,
                         submit_showcase_judge_evaluation, showcase_judge_progress,
                         revoke_showcase_judge_invitation, declare_showcase_judge_conflict,
                         get_showcase_live_context, toggle_showcase_floor_visitor_voting,
                         toggle_showcase_floor_judge_scoring, assign_showcase_roving_judge,
                         stage_set_showcase_exhibit, stage_adjust_showcase_timer,
                         stage_toggle_showcase_pause, set_showcase_projector_mode,
                         stage_clear_showcase_exhibit, showcase_exhibit_on_stage,
                         SHOWCASE_NOT_ON_STAGE)
from .operations import showcase_awards
from .throttling import (allow_panel_examiner_request, allow_panel_examiner_state_request,
                         allow_unknown_bearer_request,
                         allow_showcase_judge_request, allow_public_event_request)
from .realtime import (broadcast_panel_hearing_event, broadcast_showcase_event,
                       broadcast_training_session_event)
from .operations import (abstain_panel_score, declare_panel_conflict, resolve_panel_invitation,
                         bind_panel_invitation_device, issue_panel_device_cookie,
                         valid_panel_device_cookie,
                         submit_panel_score, open_panel_hearing, queue_panel_invitation,
                         activate_panel_candidate, deactivate_panel_candidate,
                         decline_panel_invitation, issue_panel_live_display_link,
                         revoke_panel_live_display_link, resolve_panel_live_display_link,
                         request_panel_invitation_resend, mark_panel_invitation_enqueue_failed,
                         panel_invitation_resend_available,
                         apply_panel_invitation_import,
                         revoke_panel_invitation, compile_panel_results,
                         submit_panel_vote, submit_panel_written_review, record_panel_decision,
                         confirm_panel_signoff, release_panel_results,
                         adjust_panel_result, issue_panel_entrant_result_link,
                         resolve_panel_entrant_result_link, revoke_panel_entrant_result_link,
                         redact_panel_comment, release_panel_comment, record_panel_invitation_attempt,
                         get_panel_live_state_context, set_panel_chamber_active_candidate,
                         set_panel_chamber_phase, adjust_panel_chamber_timer,
                         toggle_panel_chamber_pause, conclude_panel_chamber_candidate,
                         toggle_panel_chamber_display_mode, set_panel_phase_durations)
from .operations import (create_next_training_session, record_training_competency_result,
                         training_series_competency_trends,
                         training_series_feedback_summary, submit_training_feedback,
                         issue_training_certificates,
                         submit_competition_audience_ballot,
                         training_series_competency_export,
                         get_training_session_live_state,
                         set_training_drill_phase,
                         adjust_training_drill_timer,
                         toggle_training_drill_pause,
                         mark_training_pod_visited,
                         save_training_observation_note)
from .panel_import import (apply_candidate_import, parse_candidates, plan_candidates,
                           sample_workbook as panel_candidate_sample_workbook)
from .panel_invitation_import import (parse_examiner_emails, plan_examiner_import,
    sample_workbook as panel_invitation_sample_workbook)
from .panel_rubric_import import (apply_rubric_import, parse_rubric, plan_rubric,
    sample_workbook as panel_rubric_sample_workbook)
from .panel_reporting import panel_live_activity
from .tasks import send_panel_invitation_email
from .team_forms import (CreateTrainingSessionForm, TrainingCompetencyForm,
                         TrainingCompetencyResultForm, TrainingParticipantForm,
                         TrainingFeedbackForm, TrainingRosterImportForm,
                         TrainingFeedbackPrivacyForm, TrainingSeriesDetailsForm)
from .models import (TrainingCompetency, TrainingCompetencyResult, TrainingParticipant,
                     TrainingSeries, TrainingSeriesSession, TrainingCertificate)
from .showcase_import import parse_exhibits, plan_import, apply_import, sample_workbook
from . import embedded_import
from .training_competency_import import (
    apply_competencies, parse_competencies, plan_competencies,
    sample_workbook as training_competency_sample)
from .showcase_rubric_import import (
    apply_rubric_import, parse_rubric as parse_showcase_rubric,
    plan_rubric as plan_showcase_rubric, sample_workbook as showcase_rubric_sample,
)
from .showcase_monitor import showcase_live_activity
from .onboarding import service_onboarding
from assessments.qr import qr_png
from portal.stepper import build_steps, steps_from_setup, unsaved_steps
from .training_roster_import import (apply_training_roster_import,
    parse_training_roster, plan_training_roster_import, sample_training_roster)
from .team_series import (
    InvalidSeriesSelection, TrainingSeriesUnavailable, build_deletion_export,
    dashboard_data, delete_series_selection, deletion_redirect_query,
    get_owned_series, organization_id, parse_series_ids, series_list_data,
    selected_series_names, verify_deletion_export_token,
)


DEVICE_COOKIE_PREFIX = 'livegrade_showcase_device_'
DEVICE_COOKIE_PATH = '/showcase/exhibits/'
SHOWCASE_RESULTS_CACHE_SECONDS = 300
logger = logging.getLogger(__name__)
SHOWCASE_IMPORT_TOKEN_SALT = 'livegrade.showcase.import.preview.v1'
SHOWCASE_IMPORT_TOKEN_MAX_AGE = 15 * 60
PANEL_CANDIDATE_IMPORT_TOKEN_SALT = 'livegrade.panel.candidate-import.preview.v1'
PANEL_CANDIDATE_IMPORT_TOKEN_MAX_AGE = 15 * 60
PANEL_INVITATION_IMPORT_TOKEN_SALT = 'livegrade.panel.invitation-import.preview.v1'
PANEL_INVITATION_IMPORT_TOKEN_MAX_AGE = 15 * 60
PANEL_RUBRIC_IMPORT_TOKEN_SALT = 'livegrade.panel.rubric-import.preview.v1'
PANEL_RUBRIC_IMPORT_TOKEN_MAX_AGE = 15 * 60
PANEL_CANDIDATE_IMPORT_SESSION_KEY = 'panel_candidate_import_preview'
PANEL_RUBRIC_IMPORT_SESSION_KEY = 'panel_rubric_import_preview'
PANEL_INVITATION_IMPORT_SESSION_KEY = 'panel_invitation_import_preview'
SHOWCASE_IMPORT_SESSION_KEY = 'showcase_exhibit_import_preview'
SHOWCASE_IMPORT_ERRORS_SESSION_KEY = 'showcase_exhibit_import_errors'
SHOWCASE_RUBRIC_IMPORT_TOKEN_SALT = 'livegrade.showcase.rubric.import.preview.v1'
SHOWCASE_RUBRIC_IMPORT_SESSION_KEY = 'showcase_rubric_import_preview'
SHOWCASE_RUBRIC_IMPORT_ERRORS_SESSION_KEY = 'showcase_rubric_import_errors'
TEAM_FEEDBACK_COOKIE_PREFIX = 'livegrade_team_feedback_'
COMPETITION_AUDIENCE_COOKIE_PREFIX = 'livegrade_competition_audience_'
PANEL_EXAMINER_COOKIE_PREFIX = 'livegrade_panel_examiner_'
TEAM_ROSTER_IMPORT_SESSION_KEY = 'team_roster_import_key'
TEAM_ROSTER_IMPORT_TTL = 10 * 60


def _showcase_owner(view):
    """Keep owner routes unavailable unless the registered service grants scope."""
    authenticated_view = lecturer_required(view)

    @wraps(view)
    def scoped(request, *args, **kwargs):
        if getattr(request, 'service', None) != 'showcase':
            raise Http404
        return authenticated_view(request, *args, **kwargs)
    return scoped


def _owned_showcase_event(request, event_id):
    event = (ShowcaseEvent.objects.for_organization(request.organization)
             .owned_by(request.user).filter(pk=event_id).first())
    if event is None:
        raise Http404
    return event


def _team_owner(view):
    """Keep Team routes scoped to the registered service and signed-in owner."""
    authenticated_view = lecturer_required(view)

    @wraps(view)
    def scoped(request, *args, **kwargs):
        if getattr(request, 'service', None) != 'team':
            raise Http404
        return authenticated_view(request, *args, **kwargs)
    return scoped


def _panel_owner(view):
    authenticated_view = lecturer_required(view)

    @wraps(view)
    def scoped(request, *args, **kwargs):
        if getattr(request, 'service', None) != 'panel':
            raise Http404
        return authenticated_view(request, *args, **kwargs)
    return scoped


def _annotate_panel_hearing(hearing, now=None):
    if now is None:
        now = timezone.now()
    hearing.candidate_count = len(hearing.candidates.all())
    invitations = list(hearing.invitations.all())
    hearing.invitation_count = len(invitations)
    hearing.accepted_examiner_count = sum(
        1 for invitation in invitations
        if invitation.confirmed_at and not invitation.accepted_at
        and not invitation.declined_at and not invitation.revoked_at
        and invitation.expires_at > now)
    hearing.failed_invitation_count = sum(
        1 for invitation in invitations
        if invitation.delivery_status == PanelInvitation.DeliveryStatus.FAILED
        and not invitation.revoked_at)
    panels = list(hearing.panels.all())
    criteria = list(hearing.criteria.all())
    scoring_required = hearing.workflow in (PanelHearing.Workflow.SCORED,
                                            PanelHearing.Workflow.HYBRID)
    examiner_required = hearing.workflow in (
        PanelHearing.Workflow.SCORED, PanelHearing.Workflow.VOTE,
        PanelHearing.Workflow.HYBRID, PanelHearing.Workflow.REVIEW,
        PanelHearing.Workflow.REVIEW_VOTE)
    panel_shares_ready = bool(panels) and sum(
        (panel.share for panel in panels), Decimal('0')) == Decimal('100.00')
    panel_criteria_ready = bool(panels) and all(
        panel.criteria.exists() for panel in panels) if scoring_required else True
    criteria_assigned = (all(any(panel.share > 0 and criterion in panel.criteria.all()
                                  for panel in panels) for criterion in criteria)
                         if scoring_required else True)
    accepted_ready = True
    if examiner_required:
        required_panels = [panel for panel in panels
                           if not scoring_required or panel.share > 0]
        accepted_ready = bool(required_panels) and all(
            sum(1 for invitation in invitations
                if invitation.panel_id == panel.pk and invitation.confirmed_at
                and not invitation.accepted_at and not invitation.declined_at
                and not invitation.revoked_at
                and invitation.expires_at > now) >= panel.minimum_examiners
            for panel in required_panels)
    if hearing.status == PanelHearing.Status.DRAFT:
        if hearing.candidate_count == 0:
            hearing.dashboard_next_action = 'Add presenters or entries'
        elif scoring_required and not criteria:
            hearing.dashboard_next_action = 'Add scoring criteria'
        elif scoring_required and (not panel_shares_ready or not panel_criteria_ready or not criteria_assigned):
            hearing.dashboard_next_action = 'Check scoring panel setup'
        elif not accepted_ready:
            hearing.dashboard_next_action = 'Invite and confirm examiners'
        else:
            hearing.dashboard_next_action = 'Ready to open'
    elif hearing.status == PanelHearing.Status.OPEN and not hearing.active_candidate:
        hearing.dashboard_next_action = 'Choose the next presenter or entry'
    elif hearing.status == PanelHearing.Status.REVIEW:
        hearing.dashboard_next_action = 'Review results'
    elif hearing.status == PanelHearing.Status.SIGNED_OFF:
        hearing.dashboard_next_action = 'Release results'
    else:
        hearing.dashboard_next_action = ''
    if hearing.failed_invitation_count and hearing.status in (
            PanelHearing.Status.DRAFT, PanelHearing.Status.OPEN):
        hearing.dashboard_invitation_issue = (
            f'{hearing.failed_invitation_count} invitation email'
            f'{"s" if hearing.failed_invitation_count != 1 else ""} could not be delivered')
    else:
        hearing.dashboard_invitation_issue = ''
    return hearing


@require_safe
@never_cache
@_panel_owner
def panel_hearing_list(request):
    """Panel dashboard overview: key metrics, active hearing, stage distribution, and recent assessments."""
    hearings_qs = (PanelHearing.objects.for_organization(request.organization)
                   .owned_by(request.user)
                   .select_related('active_candidate')
                   .prefetch_related('candidates', 'criteria', 'invitations', 'panels__criteria'))
    now = timezone.now()
    all_hearings = [_annotate_panel_hearing(h, now) for h in hearings_qs.order_by('-created_at', '-pk')]

    dashboard_stats = {
        'total': len(all_hearings),
        'live': sum(h.status == PanelHearing.Status.OPEN for h in all_hearings),
        'drafts': sum(h.status == PanelHearing.Status.DRAFT for h in all_hearings),
        'review': sum(h.status == PanelHearing.Status.REVIEW for h in all_hearings),
        'release': sum(h.status == PanelHearing.Status.SIGNED_OFF for h in all_hearings),
        'completed': sum(h.status == PanelHearing.Status.RELEASED for h in all_hearings),
        'total_candidates': sum(h.candidate_count for h in all_hearings),
        'total_examiners': sum(h.accepted_examiner_count for h in all_hearings),
    }

    attention_hearings = []
    for h in all_hearings:
        if h.dashboard_next_action or h.dashboard_invitation_issue:
            attention_hearings.append(h)

    stage_colors = {
        'draft': '#d28a08',
        'open': 'var(--success)',
        'review': 'var(--accent)',
        'signed_off': 'var(--text-muted)',
        'released': 'var(--text-subtle)',
    }
    stage_distribution = []
    pie_segments = []
    cumulative = 0
    stage_items = [
        ('draft', 'In setup', dashboard_stats['drafts']),
        ('open', 'Live now', dashboard_stats['live']),
        ('review', 'Under review', dashboard_stats['review']),
        ('signed_off', 'Signed off', dashboard_stats['release']),
        ('released', 'Released', dashboard_stats['completed']),
    ]
    for key, label, count in stage_items:
        seg_start = cumulative * 360 / dashboard_stats['total'] if dashboard_stats['total'] else 0
        cumulative += count
        seg_end = cumulative * 360 / dashboard_stats['total'] if dashboard_stats['total'] else 0
        percentage = round(count * 100 / dashboard_stats['total'], 1) if dashboard_stats['total'] else 0
        stage_distribution.append({
            'key': key,
            'label': label,
            'count': count,
            'percentage': percentage,
        })
        if count:
            pie_segments.append(f'{stage_colors[key]} {seg_start:.3f}deg {seg_end:.3f}deg')
    stage_pie_background = f"conic-gradient({', '.join(pie_segments)})" if pie_segments else ''

    live_hearings = [h for h in all_hearings if h.status == PanelHearing.Status.OPEN]
    recent_hearings = all_hearings[:5]

    candidate_hearings = sorted(all_hearings, key=lambda h: (-h.candidate_count, -h.created_at.timestamp()))[:5]
    largest_candidates = max((h.candidate_count for h in candidate_hearings), default=0)
    for h in candidate_hearings:
        h.candidate_bar_percent = round(h.candidate_count * 100 / largest_candidates, 1) if largest_candidates else 0

    return render(request, 'service_domains/panel_hearing_list.html', {
        'dashboard_stats': dashboard_stats,
        'attention_hearings': attention_hearings,
        'stage_distribution': stage_distribution,
        'stage_pie_background': stage_pie_background,
        'live_hearings': live_hearings,
        'recent_hearings': recent_hearings,
        'candidate_hearings': candidate_hearings,
        'largest_candidates': largest_candidates,
        'can_oversee_panel': can_oversee(request),
    })


@require_safe
@never_cache
@_panel_owner
def panel_hearing_table(request):
    """Full searchable and paginated table of owned panel assessments."""
    base_qs = (PanelHearing.objects.for_organization(request.organization)
               .owned_by(request.user))

    status_counts = {
        'total': base_qs.count(),
        'draft': base_qs.filter(status=PanelHearing.Status.DRAFT).count(),
        'open': base_qs.filter(status=PanelHearing.Status.OPEN).count(),
        'review': base_qs.filter(status=PanelHearing.Status.REVIEW).count(),
        'signed_off': base_qs.filter(status=PanelHearing.Status.SIGNED_OFF).count(),
        'released': base_qs.filter(status=PanelHearing.Status.RELEASED).count(),
    }

    filtered_qs = base_qs.select_related('active_candidate').prefetch_related(
        'candidates', 'criteria', 'invitations', 'panels__criteria')

    search_query = (request.GET.get('q') or '').strip()[:100]
    if search_query:
        filtered_qs = filtered_qs.filter(name__icontains=search_query)

    status_filter = request.GET.get('status', '').strip()
    if status_filter in PanelHearing.Status.values:
        filtered_qs = filtered_qs.filter(status=status_filter)
    else:
        status_filter = ''

    filtered_qs = filtered_qs.order_by('-created_at', '-pk')
    filtered_count = filtered_qs.count()

    paginator, page_obj = paginate(request, filtered_qs)
    now = timezone.now()
    annotated_page_hearings = [_annotate_panel_hearing(h, now) for h in page_obj.object_list]

    query_params = QueryDict('', mutable=True)
    if search_query:
        query_params['q'] = search_query
    if status_filter:
        query_params['status'] = status_filter
    query_params['per_page'] = str(paginator.per_page)
    querystring = query_params.urlencode()

    combined_count = (CombinedPresentationAssessment.objects.for_organization(request.organization)
                      .owned_by(request.user).count())

    context = {
        'hearings': annotated_page_hearings,
        'status_counts': status_counts,
        'search_query': search_query,
        'status_filter': status_filter,
        'filtered_count': filtered_count,
        'paginator': paginator,
        'page_obj': page_obj,
        'per_page': paginator.per_page,
        'page_sizes': PAGE_SIZES,
        'querystring': querystring,
        'can_oversee_panel': can_oversee(request),
        'active_tab': 'hearings',
        'hearing_count': status_counts['total'],
        'combined_count': combined_count,
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        response = render(request, 'service_domains/_partials/panel_hearing_results.html', context)
        response['Vary'] = 'X-Requested-With'
        return response

    return render(request, 'service_domains/panel_hearing_table.html', context)


@require_http_methods(['GET', 'POST'])
@never_cache
@_panel_owner
def panel_hearing_create(request):
    """Create a new Panel hearing and proceed directly to setup steps."""
    org_id = request.organization.pk if hasattr(request.organization, 'pk') else (
        request.organization['id'] if isinstance(request.organization, dict) else request.organization)

    if request.method == 'POST':
        form = PanelHearingForm(request.POST, auto_id='hearing_%s')
        if form.is_valid():
            hearing = form.save(commit=False)
            hearing.organization_id = org_id
            hearing.created_by = request.user
            try:
                hearing.full_clean()
            except ValidationError as exc:
                form.add_error(None, '; '.join(exc.messages))
                messages.error(request, '; '.join(exc.messages))
                return render(request, 'service_domains/panel_hearing_create.html',
                      {'form': form, 'stepper_steps': _HEARING_CREATE_STEPS})
            with transaction.atomic():
                hearing.save(force_insert=True)
                panel = PanelScoringPanel(
                    hearing=hearing, name='Default panel', share=100,
                    aggregation=hearing.aggregation, minimum_examiners=hearing.minimum_examiners,
                    trim_fraction=hearing.trim_fraction,
                    organization_id=hearing.organization_id, created_by=request.user)
                panel.full_clean()
                panel.save(force_insert=True)
                event = PanelAuditEvent(hearing=hearing,
                    action=PanelAuditEvent.Action.HEARING_CREATED,
                    reason='Panel hearing created.', actor_user=request.user,
                    organization_id=hearing.organization_id, created_by=request.user)
                event.full_clean()
                event.save(force_insert=True)
            messages.success(request, 'Panel assessment created. Add candidates to continue setup.')
            if request.POST.get('advance') == '1':
                return redirect(f"{reverse('panel_hearing_manage', args=[hearing.pk])}?step=candidates")
            return redirect('panel_hearing_manage', hearing_id=hearing.pk)
        messages.error(request, 'Please correct the highlighted fields and try again.')
        return render(request, 'service_domains/panel_hearing_create.html',
                      {'form': form, 'stepper_steps': _HEARING_CREATE_STEPS})

    form = PanelHearingForm(auto_id='hearing_%s', initial={'workflow': PanelHearing.Workflow.SCORED})
    return render(request, 'service_domains/panel_hearing_create.html',
                      {'form': form, 'stepper_steps': _HEARING_CREATE_STEPS})


@require_POST
@never_cache
@_panel_owner
def panel_hearing_delete(request, hearing_id):
    """Delete a single owned panel assessment."""
    hearing = (PanelHearing.objects.for_organization(request.organization)
               .owned_by(request.user).filter(pk=hearing_id).first())
    if hearing is None:
        raise Http404
    name = hearing.name
    try:
        delete_panel_hearings([hearing])
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc))
    else:
        messages.success(request, f'"{name}" deleted.')
    return redirect('panel_hearing_table')


@require_POST
@never_cache
@_panel_owner
def panel_hearing_bulk_delete(request):
    """Delete an explicitly selected set of owned panel assessments."""
    raw_ids = request.POST.getlist('hearing_ids')
    if (not raw_ids or len(raw_ids) > max(PAGE_SIZES)
            or any(not re.fullmatch(r'[1-9][0-9]*', value)
                   or len(value) > 19
                   or (len(value) == 19 and value > '9223372036854775807')
                   for value in raw_ids)):
        messages.error(request, 'Select one or more valid assessments to delete.')
        return _panel_bulk_delete_redirect(request)

    hearing_ids = {int(value) for value in raw_ids}
    owned_hearings = (PanelHearing.objects.for_organization(request.organization)
                      .owned_by(request.user))
    with transaction.atomic():
        selected = list(owned_hearings.select_for_update().filter(pk__in=hearing_ids))
        if len(selected) != len(hearing_ids):
            messages.error(request, 'One or more selected assessments are no longer available to delete.')
            return _panel_bulk_delete_redirect(request)
        try:
            delete_panel_hearings(selected)
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc))
            return _panel_bulk_delete_redirect(request)
        count = len(selected)

    messages.success(request, f'{count} assessment{"s" if count != 1 else ""} deleted.')
    return _panel_bulk_delete_redirect(request)


def _panel_bulk_delete_redirect(request):
    """Return to the filtered assessment list preserving parameters."""
    params = QueryDict('', mutable=True)
    query = (request.POST.get('q') or '').strip()[:100]
    status_filter = request.POST.get('status', '').strip()
    if query:
        params['q'] = query
    if status_filter in PanelHearing.Status.values:
        params['status'] = status_filter
    try:
        per_page = int(request.POST.get('per_page', PAGE_SIZE))
    except (TypeError, ValueError):
        per_page = PAGE_SIZE
    if per_page not in PAGE_SIZES:
        per_page = PAGE_SIZE
    params['per_page'] = str(per_page)
    url = reverse('panel_hearing_table')
    return redirect(f'{url}?{params.urlencode()}' if params else url)


@require_http_methods(['GET', 'POST'])
@never_cache
@_panel_owner
def panel_hearing_manage(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
               .owned_by(request.user).filter(pk=hearing_id).first())
    if hearing is None:
        raise Http404
    if request.method == 'GET' and request.GET.get('activity') == '1':
        if hearing.status != PanelHearing.Status.OPEN:
            raise Http404
        response = JsonResponse(panel_live_activity(hearing))
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'same-origin'
        return response
    requires_examiners = hearing.workflow != PanelHearing.Workflow.OUTCOME
    from_param = request.GET.get('from') or request.POST.get('from', '')
    if from_param == 'results':
        back_url = reverse('panel_results_review', args=(hearing.pk,))
        back_label = 'Back to Results & Deliberation'
    elif from_param == 'participants':
        back_url = reverse('panel_hearing_participants', args=(hearing.pk,))
        back_label = 'Back to Examiners & Candidates'
    elif from_param == 'live' or hearing.status == PanelHearing.Status.OPEN:
        back_url = reverse('panel_hearing_live', args=(hearing.pk,))
        back_label = 'Back to Live Chamber'
    else:
        back_url = reverse('panel_hearing_list')
        back_label = 'Panel assessments'
    from_query = f'&from={quote(from_param)}' if from_param else ''
    forms = {
        'candidate': PanelCandidateForm(auto_id='candidate_%s'),
        'criterion': PanelCriterionForm(auto_id='criterion_%s'),
        'panel': PanelScoringPanelForm(auto_id='panel_add_%s'),
        'panel_edit': PanelScoringPanelForm(auto_id='panel_edit_%s'),
        'invitation': PanelInvitationForm(auto_id='invitation_setup_%s',
            panels=hearing.panels.order_by('name', 'pk')),
        'invitation_live': PanelInvitationForm(auto_id='invitation_live_%s',
            panels=hearing.panels.order_by('name', 'pk')),
        'hearing': PanelHearingForm(instance=hearing, auto_id='hearing_%s'),
    }
    invitation_notice = ''
    action_error = ''
    one_time_display_url = ''
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'update_hearing':
            form = PanelHearingForm(request.POST, instance=hearing, auto_id='hearing_%s')
            forms['hearing'] = form
            if form.is_valid():
                with transaction.atomic():
                    locked = (PanelHearing.objects.for_organization(request.organization)
                        .owned_by(request.user).select_for_update().filter(pk=hearing.pk).first())
                    if locked is None:
                        raise Http404
                    if locked.status in (PanelHearing.Status.SIGNED_OFF, PanelHearing.Status.RELEASED):
                        action_error = 'Hearing details cannot change once results are released.'
                    else:
                        item = form.save(commit=False)
                        item.organization_id = locked.organization_id
                        item.created_by = locked.created_by
                        try:
                            item.full_clean()
                        except ValidationError as exc:
                            form.add_error(None, '; '.join(exc.messages))
                        else:
                            item.save()
                            if request.POST.get('advance') == '1':
                                messages.success(request, 'Assessment details saved.')
                                return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=candidates{from_query}")
                            return_step = request.POST.get('return_step')
                            valid_return_steps = {'details', 'candidates', 'ready'}
                            if item.workflow != PanelHearing.Workflow.OUTCOME:
                                valid_return_steps.update({'scoring', 'examiners'})
                            if return_step in valid_return_steps:
                                messages.success(request, 'Assessment details saved.')
                                return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step={return_step}{from_query}")
                            leave_to = request.POST.get('leave_to', '')
                            if leave_to and url_has_allowed_host_and_scheme(
                                    leave_to, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                                messages.success(request, 'Assessment details saved.')
                                return redirect(leave_to)
                            return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=details&saved=1{from_query}")
        elif action == 'remove_candidate':
            with transaction.atomic():
                locked_hearing = (PanelHearing.objects.for_organization(request.organization)
                    .owned_by(request.user).select_for_update().filter(pk=hearing.pk).first())
                if locked_hearing is None:
                    raise Http404
                if locked_hearing.status != PanelHearing.Status.DRAFT:
                    action_error = 'Candidates can only be removed while the hearing is a draft.'
                else:
                    cand = locked_hearing.candidates.filter(pk=request.POST.get('candidate_id')).first()
                    if cand:
                        cand.delete()
                        messages.success(request, 'Candidate removed.')
                        return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=candidates{from_query}")
        elif action == 'remove_criterion':
            with transaction.atomic():
                locked_hearing = (PanelHearing.objects.for_organization(request.organization)
                    .owned_by(request.user).select_for_update().filter(pk=hearing.pk).first())
                if locked_hearing is None:
                    raise Http404
                if locked_hearing.status != PanelHearing.Status.DRAFT:
                    action_error = 'Criteria can only be removed while the hearing is a draft.'
                else:
                    crit = locked_hearing.criteria.filter(pk=request.POST.get('criterion_id')).first()
                    if crit:
                        crit.delete()
                        messages.success(request, 'Criterion removed.')
                        return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=scoring{from_query}")
        elif action in ('activate_candidate', 'end_candidate'):
            try:
                if action == 'activate_candidate':
                    candidate = PanelCandidate.objects.filter(hearing=hearing,
                        pk=request.POST.get('candidate_id')).first()
                    if candidate is None:
                        raise Http404
                    activate_panel_candidate(hearing, candidate,
                        organization=request.organization, user=request.user)
                else:
                    deactivate_panel_candidate(hearing,
                        organization=request.organization, user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = '; '.join(getattr(exc, 'messages', [])) or 'Candidate scoring could not be changed.'
            else:
                messages.success(request, 'Presenter activated for scoring.' if action == 'activate_candidate'
                                 else 'Presenter scoring ended.')
                return redirect('panel_hearing_manage', hearing_id=hearing.pk)
        elif action in ('candidate', 'criterion', 'panel', 'update_panel'):
            form_class = {'candidate': PanelCandidateForm, 'criterion': PanelCriterionForm,
                          'panel': PanelScoringPanelForm,
                          'update_panel': PanelScoringPanelForm}[action]
            panel_instance = None
            if action == 'update_panel':
                panel_instance = PanelScoringPanel.objects.filter(hearing=hearing,
                    pk=request.POST.get('panel_id')).first()
                if panel_instance is None:
                    raise Http404
            if action == 'criterion':
                form = form_class(request.POST, auto_id='criterion_%s',
                    panels=hearing.panels.order_by('name', 'pk'))
            elif action in ('panel', 'update_panel'):
                auto_id = f'panel_edit_{panel_instance.pk}_%s' if panel_instance else 'panel_add_%s'
                form = form_class(request.POST, instance=panel_instance, auto_id=auto_id)
            else:
                form = form_class(request.POST, auto_id='candidate_%s')
            forms[action] = form
            if action == 'update_panel':
                forms['panel_edit'] = form
            if form.is_valid():
                item = form.save(commit=False)
                with transaction.atomic():
                    locked_hearing = (PanelHearing.objects.for_organization(request.organization)
                        .owned_by(request.user).select_for_update().filter(pk=hearing.pk).first())
                    if locked_hearing is None:
                        raise Http404
                    if locked_hearing.status != PanelHearing.Status.DRAFT:
                        action_error = 'Panels, candidates and criteria can only change while the hearing is a draft.'
                    else:
                        item.hearing = locked_hearing
                        item.organization_id = locked_hearing.organization_id
                        item.created_by = request.user
                        if action == 'candidate':
                            last_order = locked_hearing.candidates.order_by(
                                '-order', '-pk').values_list('order', flat=True).first()
                            item.order = 1 if last_order is None else last_order + 1
                        elif action == 'criterion':
                            last_order = locked_hearing.criteria.order_by(
                                '-order', '-pk').values_list('order', flat=True).first()
                            item.order = 0 if last_order is None else last_order + 1
                        if action in ('panel', 'update_panel'):
                            item.configuration_managed = True
                        candidate_reference_error = (
                            'A candidate with this reference already exists. '
                            'Enter a unique reference or leave it blank.')
                        duplicate_candidate_reference = (
                            action == 'candidate' and item.reference
                            and locked_hearing.candidates.filter(reference=item.reference).exists())
                        if duplicate_candidate_reference:
                            form.add_error('reference', candidate_reference_error)
                        else:
                            try:
                                item.full_clean()
                            except ValidationError as exc:
                                form.add_error(None, '; '.join(exc.messages))
                            else:
                                try:
                                    # Use a savepoint so a concurrent duplicate-reference insert can
                                    # be reported on the field without breaking the outer transaction.
                                    with transaction.atomic():
                                        item.save(force_insert=action != 'update_panel')
                                except IntegrityError:
                                    if (action != 'candidate' or not item.reference
                                            or not locked_hearing.candidates.filter(
                                                reference=item.reference).exists()):
                                        raise
                                    form.add_error('reference', candidate_reference_error)
                                else:
                                    if action == 'criterion':
                                        selected_panels = form.cleaned_data['panels']
                                        if not selected_panels:
                                            selected_panels = locked_hearing.panels.all()
                                        for panel in selected_panels:
                                            item.panels.add(panel)
                                        PanelCriterionLevel.objects.bulk_create([
                                            PanelCriterionLevel(criterion=item, score=row['score'],
                                                description=row['description'],
                                                organization_id=locked_hearing.organization_id,
                                                created_by=request.user)
                                            for row in form._level_descriptors])
                                    audit = PanelAuditEvent(hearing=locked_hearing,
                                        action=({'candidate': PanelAuditEvent.Action.CANDIDATE_ADDED,
                                                 'criterion': PanelAuditEvent.Action.CRITERION_ADDED,
                                                 'panel': PanelAuditEvent.Action.PANEL_ADDED,
                                                 'update_panel': PanelAuditEvent.Action.PANEL_UPDATED}[action]),
                                        reason=({'candidate': 'Candidate added to draft hearing.',
                                                 'criterion': 'Scoring criterion added to draft hearing.',
                                                 'panel': 'Scoring panel added to draft hearing.',
                                                 'update_panel': 'Scoring panel configuration updated in draft hearing.'}[action]),
                                        actor_user=request.user,
                                        candidate=item if action == 'candidate' else None,
                                        organization_id=locked_hearing.organization_id,
                                        created_by=request.user)
                                    audit.full_clean()
                                    audit.save(force_insert=True)
                                    messages.success(request, {
                                        'candidate': 'Presenter or entry added.',
                                        'criterion': 'Scoring criterion added.',
                                        'panel': 'Examiner group added.',
                                        'update_panel': 'Examiner group settings saved.',
                                    }[action])
                                    step_target = 'candidates' if action == 'candidate' else 'scoring'
                                    return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step={step_target}{from_query}")
        elif action == 'open':
            try:
                open_panel_hearing(hearing, organization=request.organization, user=request.user)
            except ValidationError as exc:
                action_error = '; '.join(exc.messages)
            else:
                messages.success(request,
                    'Assessment opened for the chair to record outcomes.'
                    if not requires_examiners
                    else 'Assessment opened for examiners.')
                return redirect('panel_hearing_live', hearing_id=hearing.pk)
        elif action == 'review':
            try:
                compile_panel_results(hearing, organization=request.organization, user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = ('; '.join(getattr(exc, 'messages', [])) or
                                'The hearing could not be closed for results review.')
            else:
                messages.success(request,
                    'Assessment closed. The chair can now record outcomes.'
                    if not requires_examiners
                    else 'Scoring closed. Results are ready for review.')
                return redirect('panel_results_review', hearing_id=hearing.pk)
        elif action == 'issue_display_link':
            try:
                _display_link, raw_display_token = issue_panel_live_display_link(hearing,
                    organization=request.organization, user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = '; '.join(getattr(exc, 'messages', [])) or 'The display link could not be created.'
            else:
                display_path = reverse('panel_live_display', kwargs={'token': raw_display_token})
                one_time_display_url = public_url(request, display_path)
                messages.success(request, 'The public live display is ready. Copy the link now.')
        elif action == 'revoke_display_link':
            display_link = (PanelLiveDisplayLink.objects.for_organization(request.organization)
                .owned_by(request.user).filter(hearing=hearing,
                    pk=request.POST.get('display_link_id')).first())
            if display_link is None:
                raise Http404
            try:
                revoke_panel_live_display_link(hearing, display_link,
                    organization=request.organization, user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = '; '.join(getattr(exc, 'messages', [])) or 'The display link could not be revoked.'
            else:
                messages.success(request, 'The public live display link was revoked.')
                step_target = '?step=ready' if hearing.status == PanelHearing.Status.DRAFT else ''
                return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}{step_target}")
        elif action == 'invite':
            if not requires_examiners:
                action_error = 'This chair-recorded process does not use examiner invitations.'
            else:
                form = PanelInvitationForm(request.POST,
                    auto_id='invitation_setup_%s' if hearing.status == PanelHearing.Status.DRAFT
                    else 'invitation_live_%s',
                    panels=hearing.panels.order_by('name', 'pk'))
                forms['invitation' if hearing.status == PanelHearing.Status.DRAFT
                      else 'invitation_live'] = form
            if requires_examiners and form.is_valid():
                try:
                    invitation = queue_panel_invitation(hearing,
                        form.cleaned_data['email'], form.cleaned_data['expires_at'],
                        organization=request.organization, user=request.user,
                        panel=form.cleaned_data['panel'])
                except (PermissionDenied, ValidationError) as exc:
                    action_error = ('; '.join(getattr(exc, 'messages', [])) or
                                    'This hearing cannot accept an invitation.')
                else:
                    invitation_origin = public_url(request, '/').rstrip('/')
                    def enqueue_invitation():
                        try:
                            send_panel_invitation_email.delay(invitation.pk, invitation_origin)
                        except Exception:
                            mark_panel_invitation_enqueue_failed(invitation.pk)
                    transaction.on_commit(enqueue_invitation)
                    messages.success(request, 'Examiner invitation queued for delivery.')
                    step_target = '?step=examiners' if hearing.status == PanelHearing.Status.DRAFT else ''
                    return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}{step_target}")
        elif action == 'resend':
            invitation = PanelInvitation.objects.filter(hearing=hearing,
                pk=request.POST.get('invitation_id')).first()
            if invitation is None:
                raise Http404
            try:
                invitation = request_panel_invitation_resend(hearing, invitation,
                    organization=request.organization, user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = ('; '.join(getattr(exc, 'messages', [])) or
                                'This invitation cannot be resent.')
            else:
                invitation_origin = public_url(request, '/').rstrip('/')
                def enqueue_resend():
                    try:
                        send_panel_invitation_email.delay(invitation.pk, invitation_origin)
                    except Exception:
                        mark_panel_invitation_enqueue_failed(invitation.pk)
                transaction.on_commit(enqueue_resend)
                messages.success(request, 'Invitation resend queued for delivery.')
                step_target = '?step=examiners' if hearing.status == PanelHearing.Status.DRAFT else ''
                return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}{step_target}")
        elif action == 'revoke':
            invitation = PanelInvitation.objects.filter(hearing=hearing,
                pk=request.POST.get('invitation_id')).first()
            if invitation is None:
                raise Http404
            try:
                revoke_panel_invitation(hearing, invitation, request.POST.get('reason', ''),
                    organization=request.organization, user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = ('; '.join(getattr(exc, 'messages', [])) or
                                'This invitation cannot be revoked.')
            else:
                messages.success(request, 'Invitation revoked.')
                step_target = '?step=examiners' if hearing.status == PanelHearing.Status.DRAFT else ''
                return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}{step_target}")
        else:
            return HttpResponse('Invalid Panel organizer action.', status=400)

    candidates = hearing.candidates.order_by('order', 'pk')
    criteria = hearing.criteria.order_by('order', 'pk')
    for criterion in criteria:
        criterion.level_rows = list(criterion.levels.all())
    invitations = hearing.invitations.order_by('-created_at', 'pk')
    now = timezone.now()
    for invitation in invitations:
        invitation.can_resend = panel_invitation_resend_available(invitation, at=now)
    panel_rows = [{'panel': panel, 'form': PanelScoringPanelForm(
        instance=panel, auto_id=f'panel_edit_{panel.pk}_%s')}
        for panel in hearing.panels.order_by('name', 'pk')]
    needs_scoring = hearing.workflow in (PanelHearing.Workflow.SCORED,
                                          PanelHearing.Workflow.HYBRID)
    details_complete = bool(hearing.name and hearing.workflow)
    candidates_complete = candidates.exists()
    examiner_panels = [row['panel'] for row in panel_rows
        if not needs_scoring or row['panel'].share > 0]
    panel_total = sum((row['panel'].share for row in panel_rows), Decimal('0'))
    setup_checklist = [
        {'label': 'Choose a workflow', 'complete': details_complete,
         'anchor': 'id_workflow'},
        {'label': 'Add at least one candidate', 'complete': candidates_complete,
         'anchor': 'candidates-heading'},
    ]
    scoring_checks = []
    if needs_scoring:
        scoring_checks = [
            {'label': 'Add at least one scoring criterion',
             'complete': criteria.exists(), 'anchor': 'criteria-heading'},
            {'label': 'Set panel shares to exactly 100%',
             'complete': bool(panel_rows) and panel_total == Decimal('100.00'),
             'anchor': 'panels-heading'},
            {'label': 'Assign at least one criterion to every panel',
             'complete': bool(panel_rows) and all(row['panel'].criteria.exists()
                                                  for row in panel_rows),
             'anchor': 'panels-heading'},
            {'label': 'Assign every scoring criterion to a panel with a share',
             'complete': all(any(row['panel'].share > 0 and
                                 row['panel'].criteria.filter(pk=criterion.pk).exists()
                                 for row in panel_rows) for criterion in criteria),
             'anchor': 'panels-heading'},
        ]
        setup_checklist.extend(scoring_checks)
    examiner_check = None
    if requires_examiners:
        examiner_check = {
         'label': 'Have enough examiners accept their invitations',
         'complete': bool(examiner_panels) and all(
             hearing.invitations.filter(panel=panel, confirmed_at__isnull=False,
                 accepted_at__isnull=True, declined_at__isnull=True,
                 revoked_at__isnull=True, expires_at__gt=now).count()
             >= panel.minimum_examiners for panel in examiner_panels),
         'anchor': 'invitations-heading'}
        setup_checklist.append(examiner_check)
    setup_ready = all(step['complete'] for step in setup_checklist)
    step_completion = {
        'details': details_complete,
        'candidates': candidates_complete,
        'scoring': all(check['complete'] for check in scoring_checks),
        'examiners': examiner_check['complete'] if examiner_check else True,
        'ready': setup_ready,
    }
    workflow_descriptions = {
        PanelHearing.Workflow.SCORED:
            'Examiners score each active candidate independently. LiveGrade calculates criterion results using the configured panel rules; the chair reviews, signs off and releases them.',
        PanelHearing.Workflow.OUTCOME:
            'The panel discusses candidates outside the scoring form. The chair records one reasoned outcome per candidate, including any required conditions or revisions.',
        PanelHearing.Workflow.VOTE:
            'Examiners cast private support, oppose or abstain ballots for each active candidate. After quorum, the chair records the final outcome and rationale.',
        PanelHearing.Workflow.HYBRID:
            'Examiners independently score and cast a private ballot for each active candidate. After score coverage and vote quorum, the chair records the outcome and rationale.',
        PanelHearing.Workflow.REVIEW:
            'Examiners submit separate evidence-based written reviews for each active candidate. The chair reads these reports privately, then records the outcome, rationale and any conditions.',
        PanelHearing.Workflow.REVIEW_VOTE:
            'Examiners submit independent written reviews and private support, oppose or abstain ballots. After report coverage and vote quorum, the chair records the final outcome and rationale.',
    }
    if request.method == 'POST' and request.POST.get('action') == 'update_panel' and forms['panel_edit'].errors:
        target_id = request.POST.get('panel_id')
        for row in panel_rows:
            if str(row['panel'].pk) == target_id:
                row['form'] = forms['panel_edit']
    conflicts = (PanelConflict.objects.for_organization(request.organization)
        .owned_by(request.user).filter(candidate__hearing=hearing)
        .select_related('candidate').order_by('declared_at', 'pk'))
    active_panel_step = (request.GET.get('step') or request.POST.get('setup_step') or 'details')
    if active_panel_step not in {'details', 'candidates', 'scoring', 'examiners', 'ready'}:
        active_panel_step = 'details'
    candidate_import_form = PanelCandidateImportForm()
    rubric_import_form = PanelRubricImportForm()
    invitation_import_form = PanelInvitationImportForm(
        panels=hearing.panels.order_by('name', 'pk'))
    candidate_import_token = request.session.get(PANEL_CANDIDATE_IMPORT_SESSION_KEY, '')
    candidate_import_plan = None
    rubric_import_token = request.session.get(PANEL_RUBRIC_IMPORT_SESSION_KEY, '')
    rubric_import_plan = None
    invitation_import_token = request.session.get(PANEL_INVITATION_IMPORT_SESSION_KEY, '')
    invitation_import_plan = None
    try:
        if candidate_import_token:
            payload = signing.loads(candidate_import_token, salt=PANEL_CANDIDATE_IMPORT_TOKEN_SALT,
                max_age=PANEL_CANDIDATE_IMPORT_TOKEN_MAX_AGE)
            if (payload.get('hearing') != hearing.pk
                    or payload.get('organization') != request.organization['id']
                    or payload.get('user') != request.user.pk):
                raise ValueError('preview scope mismatch')
            parsed = payload['parsed']
            for row in parsed.get('rows', []):
                if isinstance(row.get('scheduled_time'), str):
                    row['scheduled_time'] = time.fromisoformat(row['scheduled_time'])
            candidate_import_plan = plan_candidates(hearing, parsed)
        if rubric_import_token:
            payload = signing.loads(rubric_import_token, salt=PANEL_RUBRIC_IMPORT_TOKEN_SALT,
                max_age=PANEL_RUBRIC_IMPORT_TOKEN_MAX_AGE)
            if (payload.get('hearing') != hearing.pk
                    or payload.get('organization') != request.organization['id']
                    or payload.get('user') != request.user.pk):
                raise ValueError('preview scope mismatch')
            rubric_import_plan = plan_rubric(hearing, payload['parsed'])
        if invitation_import_token:
            payload = signing.loads(invitation_import_token, salt=PANEL_INVITATION_IMPORT_TOKEN_SALT,
                max_age=PANEL_INVITATION_IMPORT_TOKEN_MAX_AGE)
            if (payload.get('hearing') != hearing.pk
                    or payload.get('organization') != request.organization['id']
                    or payload.get('user') != request.user.pk):
                raise ValueError('preview scope mismatch')
            invitation_import_plan = plan_examiner_import(hearing, payload['parsed'])
    except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError,
            KeyError, AttributeError):
        request.session.pop(PANEL_CANDIDATE_IMPORT_SESSION_KEY, None)
        request.session.pop(PANEL_RUBRIC_IMPORT_SESSION_KEY, None)
        request.session.pop(PANEL_INVITATION_IMPORT_SESSION_KEY, None)
        candidate_import_token = rubric_import_token = invitation_import_token = ''
        messages.info(request, 'An import preview expired. Please upload the spreadsheet again.')
    if 'step' not in request.GET and 'setup_step' not in request.POST:
        if candidate_import_plan:
            active_panel_step = 'candidates'
        elif rubric_import_plan:
            active_panel_step = 'scoring'
        elif invitation_import_plan:
            active_panel_step = 'examiners'
    response = render(request, 'service_domains/panel_hearing_manage.html', {
        'hearing': hearing, 'candidates': candidates, 'criteria': criteria,
        'active_panel_step': active_panel_step,
        'back_url': back_url, 'back_label': back_label, 'from_param': from_param,
        'workflow_description': workflow_descriptions.get(hearing.workflow, ''),
        'needs_scoring': needs_scoring,
        'requires_examiners': requires_examiners,
        'invitations': invitations, 'conflicts': conflicts, 'forms': forms,
        'panel_rows': panel_rows,
        'setup_checklist': setup_checklist,
        'step_completion': step_completion,
        'stepper_steps': _hearing_steps(step_completion, requires_examiners, active_panel_step),
        'setup_ready': setup_ready,
        'invitation_notice': invitation_notice, 'action_error': action_error,
        'candidate_import_form': candidate_import_form,
        'candidate_import_plan': candidate_import_plan,
        'candidate_import_token': candidate_import_token,
        'rubric_import_form': rubric_import_form,
        'rubric_import_plan': rubric_import_plan,
        'rubric_import_token': rubric_import_token,
        'invitation_import_form': invitation_import_form,
        'invitation_import_plan': invitation_import_plan,
        'invitation_import_token': invitation_import_token,
        'live_activity': panel_live_activity(hearing)
            if hearing.status == PanelHearing.Status.OPEN else None,
        'active_display_link': (PanelLiveDisplayLink.objects.for_organization(request.organization)
            .owned_by(request.user).filter(hearing=hearing, revoked_at__isnull=True)
            .order_by('-issued_at', 'pk').first() if hearing.status == PanelHearing.Status.OPEN else None),
        'one_time_display_url': one_time_display_url,
    }, status=400 if action_error or (request.method == 'POST'
    and any(form.errors for form in forms.values())) else 200)
    response['Cache-Control'] = 'private, no-store'
    # Keep the origin on same-origin form POSTs. Chrome serializes Origin as
    # "null" for ordinary form submissions under no-referrer, which Django's
    # CSRF middleware correctly rejects. Same-origin still withholds the
    # referrer from cross-origin destinations.
    response['Referrer-Policy'] = 'same-origin'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
@_panel_owner
def panel_candidate_import(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
        .owned_by(request.user).filter(pk=hearing_id, status=PanelHearing.Status.DRAFT).first())
    if hearing is None:
        raise Http404
    plan = None
    preview_token = ''
    status = 200
    embedded = request.GET.get('embedded') == '1'
    if request.method == 'POST' and request.POST.get('action') == 'cancel':
        request.session.pop(PANEL_CANDIDATE_IMPORT_SESSION_KEY, None)
        return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=candidates")
    if request.method == 'POST' and request.POST.get('action') == 'apply':
        form = PanelCandidateImportApplyForm(request.POST)
        if form.is_valid():
            token = form.cleaned_data['preview_token']
            if len(token) > 150_000:
                raise Http404
            try:
                preview = signing.loads(token, salt=PANEL_CANDIDATE_IMPORT_TOKEN_SALT,
                    max_age=PANEL_CANDIDATE_IMPORT_TOKEN_MAX_AGE)
            except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError):
                form.add_error(None, 'This preview expired or is invalid. Upload the spreadsheet again.')
                status = 400
            else:
                if (not isinstance(preview, dict) or preview.get('hearing') != hearing.pk
                        or preview.get('organization') != request.organization['id']
                        or preview.get('user') != request.user.pk
                        or not isinstance(preview.get('parsed'), dict)):
                    raise Http404
                # JSON signing cannot encode datetime.time values from parsed
                # spreadsheet cells. Restore the validated ISO representation
                # only after checking the signed preview's ownership.
                parsed = preview['parsed']
                try:
                    for row in parsed.get('rows', []):
                        scheduled_time = row.get('scheduled_time')
                        if isinstance(scheduled_time, str):
                            row['scheduled_time'] = time.fromisoformat(scheduled_time)
                except (AttributeError, TypeError, ValueError):
                    raise Http404
                try:
                    apply_candidate_import(hearing, parsed,
                        organization=request.organization, user=request.user)
                except PermissionDenied:
                    raise Http404
                except ValidationError as exc:
                    form.add_error(None, '; '.join(exc.messages))
                    status = 400
                else:
                    request.session.pop(PANEL_CANDIDATE_IMPORT_SESSION_KEY, None)
                    messages.success(request, 'Presenter and entry list imported.')
                    return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=candidates")
        else:
            status = 400
        if embedded:
            messages.error(request, 'The presenter list could not be imported. Review the errors and try again.')
            return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=candidates")
        messages.error(request, 'The presenter list could not be imported. Review the errors and try again.')
        return render(request, 'service_domains/panel_candidate_import.html', {
            'hearing': hearing, 'form': form, 'plan': plan,
        }, status=status)

    form = PanelCandidateImportForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        parsed = parse_candidates(form.cleaned_data['file'])
        plan = plan_candidates(hearing, parsed)
        if plan['ok'] or embedded:
            # Django's JSON signing serializer cannot encode datetime.time.
            # Store scheduled times as ISO strings in the client-side token.
            token_parsed = {
                **parsed,
                'rows': [
                    {**row, 'scheduled_time': row['scheduled_time'].isoformat()
                     if row.get('scheduled_time') is not None else None}
                    for row in parsed['rows']
                ],
            }
            preview_token = signing.dumps({
                'hearing': hearing.pk,
                'organization': request.organization['id'],
                'user': request.user.pk,
                'parsed': token_parsed,
            }, salt=PANEL_CANDIDATE_IMPORT_TOKEN_SALT, compress=True)
            if embedded:
                request.session[PANEL_CANDIDATE_IMPORT_SESSION_KEY] = preview_token
                request.session.modified = True
                return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=candidates")
            messages.info(request, 'List checked. Review the preview before importing.')
        else:
            messages.error(request, 'Some rows need attention. Review the import preview.')
    elif request.method == 'POST':
        status = 400
        if embedded:
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            if not form.errors:
                messages.error(request, 'Please choose a valid .xlsx spreadsheet file.')
            return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=candidates")
        messages.error(request, 'Choose a valid spreadsheet to preview.')
    response = render(request, 'service_domains/panel_candidate_import.html', {
        'hearing': hearing, 'form': form, 'plan': plan, 'preview_token': preview_token,
    }, status=status)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    return response


@require_safe
@never_cache
@_panel_owner
def panel_candidate_import_sample(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
        .owned_by(request.user).filter(pk=hearing_id, status=PanelHearing.Status.DRAFT).first())
    if hearing is None:
        raise Http404
    workbook = panel_candidate_sample_workbook()
    response = HttpResponse(workbook.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="panel-candidates-sample.xlsx"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
@_panel_owner
def panel_rubric_import(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
        .owned_by(request.user).filter(pk=hearing_id, status=PanelHearing.Status.DRAFT).first())
    if hearing is None:
        raise Http404
    plan, preview_token, status = None, '', 200
    embedded = request.GET.get('embedded') == '1'
    if request.method == 'POST' and request.POST.get('action') == 'cancel':
        request.session.pop(PANEL_RUBRIC_IMPORT_SESSION_KEY, None)
        return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=scoring")
    if request.method == 'POST' and request.POST.get('action') == 'apply':
        form = PanelRubricImportApplyForm(request.POST)
        if form.is_valid():
            token = form.cleaned_data['preview_token']
            if len(token) > 150_000:
                raise Http404
            try:
                preview = signing.loads(token, salt=PANEL_RUBRIC_IMPORT_TOKEN_SALT,
                    max_age=PANEL_RUBRIC_IMPORT_TOKEN_MAX_AGE)
            except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError):
                form.add_error(None, 'This preview expired or is invalid. Upload the spreadsheet again.')
                status = 400
            else:
                if (not isinstance(preview, dict) or preview.get('hearing') != hearing.pk
                        or preview.get('organization') != request.organization['id']
                        or preview.get('user') != request.user.pk
                        or not isinstance(preview.get('parsed'), dict)):
                    raise Http404
                try:
                    apply_rubric_import(hearing, preview['parsed'],
                        organization=request.organization, user=request.user)
                except PermissionDenied:
                    raise Http404
                except ValidationError as exc:
                    form.add_error(None, '; '.join(exc.messages))
                    status = 400
                else:
                    request.session.pop(PANEL_RUBRIC_IMPORT_SESSION_KEY, None)
                    messages.success(request, 'Scoring rubric imported.')
                    return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=scoring")
        else:
            status = 400
        if embedded:
            messages.error(request, 'The scoring rubric could not be imported. Review the errors and try again.')
            return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=scoring")
        messages.error(request, 'The scoring rubric could not be imported. Review the errors and try again.')
        response = render(request, 'service_domains/panel_rubric_import.html', {
            'hearing': hearing, 'form': form, 'plan': plan,
        }, status=status)
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'same-origin'
        return response

    form = PanelRubricImportForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        parsed = parse_rubric(form.cleaned_data['file'])
        plan = plan_rubric(hearing, parsed)
        if plan['ok'] or embedded:
            preview_token = signing.dumps({'hearing': hearing.pk,
                'organization': request.organization['id'], 'user': request.user.pk,
                'parsed': parsed}, salt=PANEL_RUBRIC_IMPORT_TOKEN_SALT, compress=True)
            if embedded:
                request.session[PANEL_RUBRIC_IMPORT_SESSION_KEY] = preview_token
                request.session.modified = True
                return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=scoring")
            messages.info(request, 'Rubric checked. Review the preview before importing.')
        else:
            messages.error(request, 'Some rubric rows need attention. Review the import preview.')
    elif request.method == 'POST':
        status = 400
        if embedded:
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            if not form.errors:
                messages.error(request, 'Please choose a valid .xlsx spreadsheet file.')
            return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=scoring")
        messages.error(request, 'Choose a valid rubric spreadsheet to preview.')
    response = render(request, 'service_domains/panel_rubric_import.html', {
        'hearing': hearing, 'form': form, 'plan': plan, 'preview_token': preview_token,
    }, status=status)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    return response


@require_safe
@never_cache
@_panel_owner
def panel_rubric_import_sample(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
        .owned_by(request.user).filter(pk=hearing_id, status=PanelHearing.Status.DRAFT).first())
    if hearing is None:
        raise Http404
    workbook = panel_rubric_sample_workbook()
    response = HttpResponse(workbook.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="panel-rubric-sample.xlsx"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
@_panel_owner
def panel_invitation_import(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
        .owned_by(request.user).filter(pk=hearing_id, status=PanelHearing.Status.DRAFT)
        .exclude(workflow=PanelHearing.Workflow.OUTCOME).first())
    if hearing is None:
        raise Http404
    panels = hearing.panels.order_by('name', 'pk')
    plan = None
    preview_token = ''
    status = 200
    embedded = request.GET.get('embedded') == '1'
    if request.method == 'POST' and request.POST.get('action') == 'cancel':
        request.session.pop(PANEL_INVITATION_IMPORT_SESSION_KEY, None)
        return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=examiners")
    if request.method == 'POST' and request.POST.get('action') == 'apply':
        form = PanelInvitationImportApplyForm(request.POST)
        if form.is_valid():
            token = form.cleaned_data['preview_token']
            if len(token) > 150_000:
                raise Http404
            try:
                preview = signing.loads(token, salt=PANEL_INVITATION_IMPORT_TOKEN_SALT,
                    max_age=PANEL_INVITATION_IMPORT_TOKEN_MAX_AGE)
            except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError):
                form.add_error(None, 'This preview expired or is invalid. Upload the spreadsheet again.')
                status = 400
            else:
                if (not isinstance(preview, dict) or preview.get('hearing') != hearing.pk
                        or preview.get('organization') != request.organization['id']
                        or preview.get('user') != request.user.pk
                        or not isinstance(preview.get('parsed'), dict)):
                    raise Http404
                panel = panels.filter(pk=preview.get('panel')).first() if preview.get('panel') else None
                if preview.get('panel') and panel is None:
                    raise Http404
                try:
                    invitations = apply_panel_invitation_import(hearing, preview['parsed'],
                        preview['expires_at'], organization=request.organization,
                        user=request.user, panel=panel)
                except PermissionDenied:
                    raise Http404
                except ValidationError as exc:
                    form.add_error(None, '; '.join(exc.messages))
                    status = 400
                else:
                    request.session.pop(PANEL_INVITATION_IMPORT_SESSION_KEY, None)
                    invitation_origin = public_url(request, '/').rstrip('/')
                    def enqueue_imported_invitations():
                        for invitation in invitations:
                            try:
                                send_panel_invitation_email.delay(invitation.pk, invitation_origin)
                            except Exception:
                                mark_panel_invitation_enqueue_failed(invitation.pk)
                    transaction.on_commit(enqueue_imported_invitations)
                    messages.success(request, f'{len(invitations)} examiner invitation(s) queued for delivery.')
                    return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=examiners")
        else:
            status = 400
        if embedded:
            messages.error(request, 'The examiner list could not be imported. Review the errors and try again.')
            return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=examiners")
        messages.error(request, 'The examiner list could not be imported. Review the errors and try again.')
        response = render(request, 'service_domains/panel_invitation_import.html', {
            'hearing': hearing, 'form': form, 'plan': plan,
        }, status=status)
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'same-origin'
        return response

    form = PanelInvitationImportForm(request.POST or None, request.FILES or None, panels=panels)
    if request.method == 'POST' and form.is_valid():
        parsed = parse_examiner_emails(form.cleaned_data['file'])
        plan = plan_examiner_import(hearing, parsed)
        if plan['ok'] or embedded:
            panel = form.cleaned_data['panel']
            preview_token = signing.dumps({
                'hearing': hearing.pk, 'organization': request.organization['id'],
                'user': request.user.pk, 'parsed': parsed,
                'panel': panel.pk if panel else None,
                'expires_at': form.cleaned_data['expires_at'].isoformat(),
            }, salt=PANEL_INVITATION_IMPORT_TOKEN_SALT, compress=True)
            if embedded:
                request.session[PANEL_INVITATION_IMPORT_SESSION_KEY] = preview_token
                request.session.modified = True
                return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=examiners")
            messages.info(request, 'Examiner list checked. Review the preview before sending invitations.')
        else:
            messages.error(request, 'Some email rows need attention. Review the import preview.')
    elif request.method == 'POST':
        status = 400
        if embedded:
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            if not form.errors:
                messages.error(request, 'Please choose a valid .xlsx spreadsheet file.')
            return redirect(f"{reverse('panel_hearing_manage', args=(hearing.pk,))}?step=examiners")
        messages.error(request, 'Choose a valid examiner spreadsheet to preview.')
    response = render(request, 'service_domains/panel_invitation_import.html', {
        'hearing': hearing, 'form': form, 'plan': plan, 'preview_token': preview_token,
    }, status=status)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    return response


@require_safe
@never_cache
@_panel_owner
def panel_invitation_import_sample(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
        .owned_by(request.user).filter(pk=hearing_id, status=PanelHearing.Status.DRAFT)
        .exclude(workflow=PanelHearing.Workflow.OUTCOME).first())
    if hearing is None:
        raise Http404
    workbook = panel_invitation_sample_workbook()
    response = HttpResponse(workbook.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="panel-examiners.xlsx"'
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_safe
@never_cache
@_panel_owner
def panel_recusal_export(request, hearing_id):
    """Export this owner's reasoned recusals without exposing any score data."""
    hearing = (PanelHearing.objects.for_organization(request.organization)
        .owned_by(request.user).filter(pk=hearing_id).first())
    if hearing is None:
        raise Http404
    conflicts = (PanelConflict.objects.for_organization(request.organization)
        .owned_by(request.user).filter(candidate__hearing=hearing)
        .select_related('candidate', 'invitation').order_by('declared_at', 'pk'))
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(('hearing', 'candidate', 'examiner_email', 'conflict_reason', 'declared_at'))
    for conflict in conflicts:
        # nosemgrep: python.django.security.injection.csv-writer-injection.csv-writer-injection
        writer.writerow(tuple(_csv_cell(value) for value in (
            hearing.name, conflict.candidate.display_name, conflict.invitation.email,
            conflict.reason, conflict.declared_at.isoformat())))
    response = HttpResponse(output.getvalue(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="panel-hearing-{hearing.pk}-recusals.csv"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_safe
@never_cache
@_panel_owner
def panel_results_export(request, hearing_id):
    """Download an owner-only, examiner-anonymous aggregate results workbook."""
    hearing = (PanelHearing.objects.for_organization(request.organization)
        .owned_by(request.user).filter(pk=hearing_id).first())
    if hearing is None or hearing.status == PanelHearing.Status.DRAFT:
        raise Http404
    snapshots = list(PanelResultSnapshot.objects.for_organization(request.organization)
        .owned_by(request.user).filter(hearing=hearing)
        .select_related('candidate', 'criterion').order_by('candidate__order', 'criterion__order', 'pk'))
    adjustments = (PanelResultAdjustment.objects.for_organization(request.organization)
        .owned_by(request.user).filter(snapshot__in=snapshots).order_by('snapshot_id', '-revision'))
    latest = {}
    for adjustment in adjustments:
        latest.setdefault(adjustment.snapshot_id, adjustment)
    book = Workbook()
    sheet = book.active
    sheet.title = 'Panel results'
    sheet.append(['Hearing', 'Date', 'Venue', 'Candidate', 'Reference', 'Criterion',
                  'Aggregate score', 'Final score', 'Raters', 'Minimum met',
                  'Aggregation', 'Trim fraction', 'Panel methodology', 'Criterion weight'])
    def safe_text(value):
        value = str(value or '')
        return "'" + value if value.startswith(('=', '+', '-', '@')) else value

    if snapshots:
        for snapshot in snapshots:
            adjustment = latest.get(snapshot.pk)
            sheet.append([safe_text(hearing.name), hearing.hearing_date, safe_text(hearing.venue),
                safe_text(snapshot.candidate.display_name), safe_text(snapshot.candidate.reference),
                safe_text(snapshot.criterion.name), float(snapshot.score),
                float(adjustment.adjusted_score if adjustment else snapshot.score),
                snapshot.rater_count, not snapshot.below_minimum, snapshot.aggregation,
                float(snapshot.trim_fraction) if snapshot.trim_fraction is not None else None,
                safe_text(snapshot.panel_methodology), float(snapshot.criterion.weight)])
    else:
        # Populate provisional aggregate scores per candidate and criterion
        conflicts_by_candidate = defaultdict(set)
        for inv_id, cand_id in (PanelConflict.objects.filter(candidate__hearing=hearing,
                organization_id=request.organization['id']).values_list('invitation_id', 'candidate_id')):
            conflicts_by_candidate[cand_id].add(inv_id)

        scores_map = defaultdict(list)
        for s in (PanelScore.objects.filter(candidate__hearing=hearing,
                organization_id=request.organization['id']).select_related('invitation')):
            if s.invitation_id not in conflicts_by_candidate[s.candidate_id]:
                scores_map[(s.candidate_id, s.criterion_id)].append(float(s.score))

        for candidate in hearing.candidates.order_by('order', 'pk'):
            for criterion in hearing.criteria.order_by('order', 'pk'):
                c_scores = scores_map.get((candidate.pk, criterion.pk), [])
                avg_score = round(sum(c_scores) / len(c_scores), 2) if c_scores else None
                sheet.append([
                    safe_text(hearing.name),
                    hearing.hearing_date,
                    safe_text(hearing.venue),
                    safe_text(candidate.display_name),
                    safe_text(candidate.reference),
                    safe_text(criterion.name),
                    avg_score,
                    avg_score,
                    len(c_scores),
                    len(c_scores) >= hearing.minimum_examiners,
                    hearing.get_aggregation_display(),
                    float(hearing.trim_fraction) if hearing.trim_fraction is not None else None,
                    'Provisional live aggregation',
                    float(criterion.weight)
                ])
    decisions = (PanelDecision.objects.for_organization(request.organization)
        .owned_by(request.user).filter(hearing=hearing).select_related('candidate', 'decided_by')
        .order_by('candidate__order', 'candidate_id', '-revision'))
    latest_decisions = {}
    for decision in decisions:
        latest_decisions.setdefault(decision.candidate_id, decision)
    written_reviews = {}
    latest_reviews = {}
    for report in (PanelWrittenReview.objects.for_organization(request.organization)
            .owned_by(request.user).filter(hearing=hearing)
            .order_by('candidate__order', 'invitation_id', '-revision')):
        latest_reviews.setdefault((report.candidate_id, report.invitation_id), report)
    for report in latest_reviews.values():
        written_reviews.setdefault(report.candidate_id, []).append(report)
    if latest_decisions:
        decision_sheet = book.create_sheet('Chair outcomes')
        decision_sheet.append(['Candidate', 'Outcome', 'Rationale', 'Conditions or revisions',
                               'Dissent', 'Revision', 'Recorded by', 'Recorded at'])
        for candidate_id, decision in latest_decisions.items():
            decision_sheet.append([safe_text(decision.candidate.display_name),
                decision.get_outcome_display(), safe_text(decision.rationale),
                safe_text(decision.conditions), safe_text(decision.dissent), decision.revision,
                safe_text(decision.decided_by),
                decision.created_at.replace(tzinfo=None) if decision.created_at else None])
        decision_sheet.freeze_panes = 'A2'
        decision_sheet.auto_filter.ref = decision_sheet.dimensions
    if hearing.workflow in (PanelHearing.Workflow.VOTE, PanelHearing.Workflow.HYBRID,
                            PanelHearing.Workflow.REVIEW_VOTE):
        vote_sheet = book.create_sheet('Private vote totals')
        vote_sheet.append(['Candidate', 'Support recommendation', 'Do not support', 'Abstain'])
        for candidate in hearing.candidates.order_by('order', 'pk'):
            tally = {choice: 0 for choice, _label in PanelVote.Choice.choices}
            rows = (PanelVote.objects.for_organization(request.organization)
                .owned_by(request.user).filter(hearing=hearing, candidate=candidate)
                .values_list('choice').annotate(count=Count('pk')))
            for choice, count in rows:
                tally[choice] = count
            vote_sheet.append([safe_text(candidate.display_name), tally[PanelVote.Choice.SUPPORT],
                tally[PanelVote.Choice.OPPOSE], tally[PanelVote.Choice.ABSTAIN]])
        vote_sheet.freeze_panes = 'A2'
        vote_sheet.auto_filter.ref = vote_sheet.dimensions
    if written_reviews:
        report_sheet = book.create_sheet('Written reviews')
        report_sheet.append(['Candidate', 'Report text', 'Revision', 'Correction reason', 'Submitted at'])
        for candidate_id, reports in written_reviews.items():
            for report in reports:
                report_sheet.append([safe_text(report.candidate.display_name),
                    safe_text(report.report), report.revision, safe_text(report.revision_reason),
                    report.created_at.replace(tzinfo=None) if report.created_at else None])
        report_sheet.freeze_panes = 'A2'
        report_sheet.auto_filter.ref = report_sheet.dimensions
    abstentions = (PanelAbstention.objects.for_organization(request.organization)
        .owned_by(request.user).filter(candidate__hearing=hearing)
        .select_related('candidate', 'criterion'))
    if abstentions.exists():
        skipped = book.create_sheet('Abstentions')
        skipped.append(['Candidate', 'Criterion', 'Reason'])
        for abstention in abstentions:
            skipped.append([safe_text(abstention.candidate.display_name),
                            safe_text(abstention.criterion.name),
                            safe_text(abstention.reason or 'Not recorded (legacy response)')])
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    output = io.BytesIO()
    book.save(output)
    event = PanelAuditEvent(hearing=hearing, action=PanelAuditEvent.Action.RESULTS_EXPORTED,
        reason='Owner exported the hearing review workbook with reviewer attribution omitted where shown.', actor_user=request.user,
        organization_id=hearing.organization_id, created_by=request.user)
    event.full_clean()
    event.save(force_insert=True)
    response = HttpResponse(output.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="panel-hearing-{hearing.pk}-results.xlsx"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_safe
@never_cache
@_panel_owner
def panel_hearing_live(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
               .owned_by(request.user).filter(pk=hearing_id).first())
    if hearing is None:
        raise Http404
    if hearing.status == PanelHearing.Status.DRAFT:
        return redirect('panel_hearing_manage', hearing_id=hearing.pk)
    if hearing.status in (PanelHearing.Status.REVIEW, PanelHearing.Status.SIGNED_OFF, PanelHearing.Status.RELEASED):
        return redirect('panel_results_review', hearing_id=hearing.pk)
    context = get_panel_live_state_context(hearing)
    return render(request, 'service_domains/panel_hearing_live.html', context)


@require_safe
@never_cache
@_panel_owner
def panel_hearing_live_state(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
               .owned_by(request.user).filter(pk=hearing_id).first())
    if hearing is None:
        raise Http404
    if hearing.status != PanelHearing.Status.OPEN:
        redirect_url = (reverse('panel_results_review', args=(hearing.pk,))
                        if hearing.status in (PanelHearing.Status.REVIEW, PanelHearing.Status.SIGNED_OFF, PanelHearing.Status.RELEASED)
                        else reverse('panel_hearing_manage', args=(hearing.pk,)))
        return JsonResponse({'status': hearing.status, 'redirect': redirect_url})
    context = get_panel_live_state_context(hearing)
    active_cand = context['active_candidate']
    return JsonResponse({
        'status': hearing.status,
        'active_candidate_id': active_cand.pk if active_cand else None,
        'active_phase': context['active_phase'],
        'phase_duration_seconds': context.get('phase_duration_seconds', 900),
        'phase_seconds_remaining': context['phase_seconds_remaining'],
        'phase_timer_display': context.get('phase_timer_display', '15:00'),
        'timer_paused': context['timer_paused'],
        'display_mode': context['display_mode'],
        'phase_durations_mins': context.get('phase_durations_mins', {}),
        'hero_html': render_to_string('service_domains/_partials/panel_live_hero.html', context, request=request),
        'examiners_html': render_to_string('service_domains/_partials/panel_live_examiners.html', context, request=request),
        'convergence_html': render_to_string('service_domains/_partials/panel_live_convergence.html', context, request=request),
        'docket_html': render_to_string('service_domains/_partials/panel_live_docket.html', context, request=request),
        'display_html': render_to_string('service_domains/_partials/panel_live_display_control.html', context, request=request),
    })


@require_POST
@never_cache
@_panel_owner
def panel_hearing_live_action(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
               .owned_by(request.user).filter(pk=hearing_id).first())
    if hearing is None:
        raise Http404
    if hearing.status != PanelHearing.Status.OPEN:
        return JsonResponse({'error': 'Hearing is not open.'}, status=400)
    action = request.POST.get('action')
    try:
        if action == 'set_candidate':
            cand_id = request.POST.get('candidate_id')
            set_panel_chamber_active_candidate(hearing, cand_id, organization=request.organization, user=request.user)
        elif action == 'set_phase':
            phase = request.POST.get('phase')
            set_panel_chamber_phase(hearing, phase, organization=request.organization, user=request.user)
        elif action == 'adjust_timer':
            delta = int(request.POST.get('delta', 60))
            adjust_panel_chamber_timer(hearing, delta, organization=request.organization, user=request.user)
        elif action == 'toggle_pause':
            toggle_panel_chamber_pause(hearing, organization=request.organization, user=request.user)
        elif action == 'record_decision':
            cand_id = request.POST.get('candidate_id')
            outcome = request.POST.get('outcome')
            rationale = request.POST.get('rationale', 'Recorded during chamber deliberation.')
            conditions = request.POST.get('conditions', '')
            dissent = request.POST.get('dissent', '')
            candidate = PanelCandidate.objects.filter(hearing=hearing, pk=cand_id).first()
            if candidate is None:
                raise ValidationError('Candidate not found.')
            record_panel_decision(hearing, candidate, outcome, rationale, conditions, dissent,
                                  organization=request.organization, user=request.user)
        elif action == 'conclude_candidate':
            cand_id = request.POST.get('candidate_id')
            conclude_panel_chamber_candidate(hearing, cand_id, organization=request.organization, user=request.user)
        elif action == 'nudge_examiner':
            inv_id = request.POST.get('invitation_id')
            inv = PanelInvitation.objects.filter(hearing=hearing, pk=inv_id).first()
            if inv and panel_invitation_resend_available(inv):
                request_panel_invitation_resend(hearing, inv, organization=request.organization, user=request.user)
        elif action == 'set_display_mode':
            mode = request.POST.get('mode')
            toggle_panel_chamber_display_mode(hearing, mode, organization=request.organization, user=request.user)
        elif action == 'set_phase_durations':
            durations = {}
            for phase in ('presentation', 'qa', 'deliberation', 'decision'):
                mins_val = request.POST.get(f'{phase}_minutes')
                if mins_val:
                    try:
                        durations[phase] = int(mins_val) * 60
                    except (ValueError, TypeError):
                        pass
            set_panel_phase_durations(hearing, durations, organization=request.organization, user=request.user)
        elif action == 'close_hearing':
            compile_panel_results(hearing, organization=request.organization, user=request.user)
            broadcast_panel_hearing_event(hearing, 'hearing.closed', {})
            messages.success(request, 'Hearing closed. Review and sign off results.')
            return JsonResponse({'status': 'closed', 'redirect': reverse('panel_results_review', args=(hearing.pk,))})
        else:
            return JsonResponse({'error': f'Unknown action: {action}'}, status=400)
    except (ValidationError, PermissionDenied) as exc:
        msg = '; '.join(getattr(exc, 'messages', [])) or str(exc)
        return JsonResponse({'error': msg}, status=400)

    hearing.refresh_from_db()
    context = get_panel_live_state_context(hearing)
    active_cand = context['active_candidate']
    broadcast_panel_hearing_event(hearing, f'panel.{action}', {
        'action': action,
        'active_candidate_id': active_cand.pk if active_cand else None,
        'active_candidate_name': active_cand.display_name if active_cand else '',
        'active_phase': context['active_phase'],
        'phase_seconds_remaining': context['phase_seconds_remaining'],
        'timer_paused': context['timer_paused'],
        'display_mode': context['display_mode'],
    })
    if action in ('set_candidate', 'conclude_candidate'):
        broadcast_panel_hearing_event(hearing, 'candidate.changed', {
            'active_candidate_id': active_cand.pk if active_cand else None,
            'active_candidate_name': active_cand.display_name if active_cand else '',
        })
    return JsonResponse({
        'status': hearing.status,
        'active_candidate_id': active_cand.pk if active_cand else None,
        'active_phase': context['active_phase'],
        'phase_duration_seconds': context.get('phase_duration_seconds', 900),
        'phase_seconds_remaining': context['phase_seconds_remaining'],
        'phase_timer_display': context.get('phase_timer_display', '15:00'),
        'timer_paused': context['timer_paused'],
        'display_mode': context['display_mode'],
        'phase_durations_mins': context.get('phase_durations_mins', {}),
        'hero_html': render_to_string('service_domains/_partials/panel_live_hero.html', context, request=request),
        'examiners_html': render_to_string('service_domains/_partials/panel_live_examiners.html', context, request=request),
        'convergence_html': render_to_string('service_domains/_partials/panel_live_convergence.html', context, request=request),
        'docket_html': render_to_string('service_domains/_partials/panel_live_docket.html', context, request=request),
        'display_html': render_to_string('service_domains/_partials/panel_live_display_control.html', context, request=request),
    })


@require_POST
@never_cache
@_panel_owner
def panel_hearing_live_display_toggle(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
               .owned_by(request.user).filter(pk=hearing_id).first())
    if hearing is None:
        raise Http404
    link = PanelLiveDisplayLink.objects.filter(hearing=hearing, organization_id=hearing.organization_id, revoked_at__isnull=True).first()
    if link is None:
        link, token = issue_panel_live_display_link(hearing, organization=request.organization, user=request.user)
        return JsonResponse({'active': True, 'url': reverse('panel_live_display', args=(token,))})
    else:
        revoke_panel_live_display_link(hearing, link, organization=request.organization, user=request.user)
        return JsonResponse({'active': False, 'url': None})


@require_http_methods(['GET', 'POST'])
@never_cache
@_panel_owner
def panel_hearing_participants(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
               .owned_by(request.user).filter(pk=hearing_id).first())
    if hearing is None:
        raise Http404
    requires_examiners = hearing.workflow != PanelHearing.Workflow.OUTCOME
    action_error = ''

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'activate_candidate':
            candidate_id = request.POST.get('candidate_id')
            if candidate_id:
                try:
                    set_panel_chamber_active_candidate(hearing, candidate_id,
                        organization=request.organization, user=request.user)
                    messages.success(request, 'Presenter activated on the chamber floor.')
                    return redirect('panel_hearing_live', hearing_id=hearing.pk)
                except (ValidationError, PermissionDenied) as exc:
                    action_error = '; '.join(getattr(exc, 'messages', [])) or str(exc)
        elif action == 'invite' and requires_examiners:
            form = PanelInvitationForm(request.POST,
                panels=hearing.panels.order_by('name', 'pk'))
            if form.is_valid():
                try:
                    invitation = queue_panel_invitation(hearing,
                        form.cleaned_data['email'], form.cleaned_data['expires_at'],
                        organization=request.organization, user=request.user,
                        panel=form.cleaned_data['panel'])
                except (PermissionDenied, ValidationError) as exc:
                    action_error = ('; '.join(getattr(exc, 'messages', [])) or
                                    'This hearing cannot accept an invitation.')
                else:
                    invitation_origin = public_url(request, '/').rstrip('/')
                    def enqueue_invitation():
                        try:
                            send_panel_invitation_email.delay(invitation.pk, invitation_origin)
                        except Exception:
                            mark_panel_invitation_enqueue_failed(invitation.pk)
                    transaction.on_commit(enqueue_invitation)
                    messages.success(request, 'Examiner invitation queued for delivery.')
                    return redirect('panel_hearing_participants', hearing_id=hearing.pk)
            else:
                action_error = 'Please enter a valid email address.'
        elif action == 'resend' and requires_examiners:
            invitation = PanelInvitation.objects.filter(hearing=hearing,
                pk=request.POST.get('invitation_id')).first()
            if invitation is None:
                raise Http404
            try:
                invitation = request_panel_invitation_resend(hearing, invitation,
                    organization=request.organization, user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = ('; '.join(getattr(exc, 'messages', [])) or
                                'This invitation cannot be resent.')
            else:
                invitation_origin = public_url(request, '/').rstrip('/')
                def enqueue_resend():
                    try:
                        send_panel_invitation_email.delay(invitation.pk, invitation_origin)
                    except Exception:
                        mark_panel_invitation_enqueue_failed(invitation.pk)
                transaction.on_commit(enqueue_resend)
                messages.success(request, 'Invitation resend queued for delivery.')
                return redirect('panel_hearing_participants', hearing_id=hearing.pk)
        elif action == 'revoke' and requires_examiners:
            invitation = PanelInvitation.objects.filter(hearing=hearing,
                pk=request.POST.get('invitation_id')).first()
            if invitation is None:
                raise Http404
            try:
                revoke_panel_invitation(hearing, invitation, request.POST.get('reason', ''),
                    organization=request.organization, user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = ('; '.join(getattr(exc, 'messages', [])) or
                                'This invitation cannot be revoked.')
            else:
                messages.success(request, 'Examiner invitation revoked.')
                return redirect('panel_hearing_participants', hearing_id=hearing.pk)

    org = hearing.organization_id
    candidates_qs = list(PanelCandidate.objects.filter(hearing=hearing, organization_id=org).order_by('order', 'pk'))
    criteria = list(PanelCriterion.objects.filter(hearing=hearing, organization_id=org).order_by('order', 'pk'))
    total_criteria = len(criteria)
    active_cand = hearing.active_candidate
    if active_cand is None and candidates_qs:
        active_cand = candidates_qs[0]

    decisions = {d.candidate_id: d for d in PanelDecision.objects.filter(hearing=hearing, organization_id=org).order_by('revision')}

    conflicts_by_candidate = defaultdict(set)
    conflicts_list = list(PanelConflict.objects.filter(candidate__hearing=hearing, organization_id=org).select_related('candidate', 'invitation'))
    for conf in conflicts_list:
        conflicts_by_candidate[conf.candidate_id].add(conf.invitation_id)

    candidate_scores_count = defaultdict(int)
    for row in (PanelScore.objects.filter(candidate__hearing=hearing, organization_id=org)
            .values('candidate_id').annotate(count=Count('pk'))):
        candidate_scores_count[row['candidate_id']] = row['count']

    candidate_rows = []
    for cand in candidates_qs:
        is_active = bool(active_cand and cand.pk == active_cand.pk)
        has_decision = cand.pk in decisions
        candidate_rows.append({
            'candidate': cand,
            'is_active': is_active,
            'has_decision': has_decision,
            'decision': decisions.get(cand.pk),
            'scores_count': candidate_scores_count.get(cand.pk, 0),
        })

    invitations_qs = list(PanelInvitation.objects.filter(hearing=hearing, organization_id=org, revoked_at__isnull=True).select_related('panel').order_by('pk'))
    examiner_rows = []
    for inv in invitations_qs:
        scored_count = 0
        if active_cand:
            scored_count = PanelScore.objects.filter(candidate=active_cand, invitation=inv).count()
        total_evaluations = PanelScore.objects.filter(invitation=inv).count()
        examiner_rows.append({
            'invitation': inv,
            'panel_name': inv.panel.name if inv.panel else 'Default panel',
            'is_conflicted': bool(active_cand and inv.pk in conflicts_by_candidate.get(active_cand.pk, set())),
            'active_candidate_scored': scored_count,
            'total_criteria': total_criteria,
            'total_evaluations': total_evaluations,
            'is_bound': bool(inv.device_digest),
        })

    invitation_form = PanelInvitationForm(panels=hearing.panels.order_by('name', 'pk')) if requires_examiners else None

    context = {
        'hearing': hearing,
        'candidates': candidate_rows,
        'examiners': examiner_rows,
        'conflicts': conflicts_list,
        'requires_examiners': requires_examiners,
        'active_candidate': active_cand,
        'invitation_form': invitation_form,
        'action_error': action_error,
        'active_tab': 'participants',
    }
    response = render(request, 'service_domains/panel_hearing_participants.html', context)
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Referrer-Policy'] = 'same-origin'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
@_panel_owner
def panel_results_review(request, hearing_id):
    hearing = (PanelHearing.objects.for_organization(request.organization)
               .owned_by(request.user).filter(pk=hearing_id).first())
    if hearing is None or hearing.status == PanelHearing.Status.DRAFT:
        raise Http404
    action_error = ''
    adjustment_errors = {}
    one_time_entrant_url = ''
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'decision':
                candidate = (PanelCandidate.objects.for_organization(request.organization)
                    .owned_by(request.user).filter(hearing=hearing,
                        pk=request.POST.get('candidate_id')).first())
                if candidate is None:
                    raise Http404
                form = PanelDecisionForm(request.POST,
                    prefix=f'decision_{candidate.pk}')
                if form.is_valid():
                    values = form.cleaned_data
                    record_panel_decision(hearing, candidate, values['outcome'], values['rationale'],
                        values['conditions'], values['dissent'], organization=request.organization,
                        user=request.user)
                else:
                    action_error = '; '.join(f'{field}: {", ".join(errors)}'
                        for field, errors in form.errors.items())
            elif action == 'sign_off':
                confirm_panel_signoff(hearing, organization=request.organization, user=request.user)
            elif action == 'release':
                release_panel_results(hearing, organization=request.organization, user=request.user)
            elif action == 'adjust':
                snapshot = (PanelResultSnapshot.objects.for_organization(request.organization)
                    .owned_by(request.user).filter(hearing=hearing,
                        pk=request.POST.get('snapshot_id')).first())
                if snapshot is None:
                    raise Http404
                form = PanelResultAdjustmentForm(request.POST, prefix=str(snapshot.pk))
                if form.is_valid():
                    adjust_panel_result(snapshot, form.cleaned_data['adjusted_score'],
                        form.cleaned_data['reason'], organization=request.organization,
                        user=request.user)
                else:
                    adjustment_errors[snapshot.pk] = form
            elif action == 'redact_comment':
                score = (PanelScore.objects.for_organization(request.organization)
                    .owned_by(request.user).filter(candidate__hearing=hearing,
                        criterion__hearing=hearing, pk=request.POST.get('score_id')).first())
                if score is None:
                    raise Http404
                form = PanelCommentRedactionForm(request.POST, prefix=str(score.pk))
                if form.is_valid():
                    redact_panel_comment(hearing, score, form.cleaned_data['reason'],
                        organization=request.organization, user=request.user)
                else:
                    action_error = '; '.join(form.errors.get('reason', []))
            elif action == 'release_comment':
                score = (PanelScore.objects.for_organization(request.organization)
                    .owned_by(request.user).filter(candidate__hearing=hearing,
                        criterion__hearing=hearing, pk=request.POST.get('score_id')).first())
                if score is None:
                    raise Http404
                form = PanelCommentReleaseForm(request.POST, prefix=str(score.pk))
                if form.is_valid():
                    release_panel_comment(hearing, score, form.cleaned_data['reason'],
                        organization=request.organization, user=request.user)
                else:
                    action_error = '; '.join(form.errors.get('reason', []))
            elif action == 'issue_entrant_link':
                candidate = (PanelCandidate.objects.for_organization(request.organization)
                    .owned_by(request.user).filter(hearing=hearing,
                        pk=request.POST.get('candidate_id')).first())
                if candidate is None:
                    raise Http404
                _, raw_token = issue_panel_entrant_result_link(hearing, candidate,
                    organization=request.organization, user=request.user)
                path = reverse('panel_entrant_results', kwargs={'token': raw_token})
                one_time_entrant_url = request.build_absolute_uri(path)
            elif action == 'revoke_entrant_link':
                link = (PanelEntrantResultLink.objects.for_organization(request.organization)
                    .owned_by(request.user).filter(hearing=hearing,
                        pk=request.POST.get('link_id')).first())
                if link is None:
                    raise Http404
                revoke_panel_entrant_result_link(hearing, link, request.POST.get('reason', ''),
                    organization=request.organization, user=request.user)
            else:
                return HttpResponse('Invalid results action.', status=400)
        except Http404:
            raise
        except (PermissionDenied, ValidationError) as exc:
            action_error = '; '.join(getattr(exc, 'messages', [])) or 'This results action cannot be accepted.'
        else:
            if not adjustment_errors and not one_time_entrant_url and not action_error:
                messages.success(request, {
                    'decision': 'Panel decision recorded.',
                    'sign_off': 'Results signed off.',
                    'release': 'Results released.',
                    'adjust': 'Score adjustment recorded.',
                    'redact_comment': 'Comment redacted.',
                    'release_comment': 'Comment released.',
                    'revoke_entrant_link': 'Results link revoked.',
                }.get(action, 'Panel action completed.'))
                return redirect('panel_results_review', hearing_id=hearing.pk)
            if one_time_entrant_url:
                messages.success(request, 'Private results link created. Copy it now; it is shown once.')

    snapshots = list(PanelResultSnapshot.objects.for_organization(request.organization)
        .owned_by(request.user).filter(hearing=hearing)
        .select_related('candidate', 'criterion').order_by(
            'candidate__order', 'criterion__order', 'pk'))
    latest_adjustments = {}
    adjustments = (PanelResultAdjustment.objects.for_organization(request.organization)
        .owned_by(request.user).filter(snapshot__in=snapshots).select_related('adjusted_by')
        .order_by('snapshot_id', '-revision'))
    adjustment_history = {}
    for adjustment in adjustments:
        adjustment_history.setdefault(adjustment.snapshot_id, []).append(adjustment)
        latest_adjustments.setdefault(adjustment.snapshot_id, adjustment)
    adjustment_forms = {
        snapshot.pk: adjustment_errors.get(snapshot.pk,
            PanelResultAdjustmentForm(prefix=str(snapshot.pk)))
        for snapshot in snapshots
    }
    result_rows = [{
        'snapshot': snapshot,
        'latest_adjustment': latest_adjustments.get(snapshot.pk),
        'adjustment_history': adjustment_history.get(snapshot.pk, []),
        'adjustment_form': adjustment_forms[snapshot.pk],
    } for snapshot in snapshots]
    moderation_scores = list(PanelScore.objects.for_organization(request.organization)
        .owned_by(request.user).filter(candidate__hearing=hearing, criterion__hearing=hearing)
        .exclude(comment='').select_related('candidate', 'criterion')
        .order_by('candidate__order', 'criterion__order', 'pk'))
    moderation_comments = [{'score': score,
        'redaction_form': PanelCommentRedactionForm(prefix=str(score.pk)),
        'release_form': PanelCommentReleaseForm(prefix=str(score.pk))} for score in moderation_scores]
    entrant_candidates = list(hearing.candidates.order_by('order', 'pk'))
    latest_decisions = {}
    for decision in (PanelDecision.objects.for_organization(request.organization)
            .owned_by(request.user).filter(hearing=hearing).order_by('candidate_id', '-revision')):
        latest_decisions.setdefault(decision.candidate_id, decision)
    written_reviews = {}
    for report in (PanelWrittenReview.objects.for_organization(request.organization)
            .owned_by(request.user).filter(hearing=hearing)
            .order_by('candidate__order', 'created_at', 'pk')):
        written_reviews.setdefault(report.candidate_id, []).append(report)
    vote_tallies = {}
    for candidate in entrant_candidates:
        tally = {choice: 0 for choice, _label in PanelVote.Choice.choices}
        for choice, count in (PanelVote.objects.for_organization(request.organization)
                .owned_by(request.user).filter(hearing=hearing, candidate=candidate)
                .values_list('choice').annotate(count=Count('pk'))):
            tally[choice] = count
        vote_tallies[candidate.pk] = tally
    decision_rows = [{'candidate': candidate, 'decision': latest_decisions.get(candidate.pk),
        'form': PanelDecisionForm(prefix=f'decision_{candidate.pk}'),
        'votes': vote_tallies[candidate.pk],
        'written_reviews': written_reviews.get(candidate.pk, [])}
        for candidate in entrant_candidates]
    entrant_links = list(PanelEntrantResultLink.objects.for_organization(request.organization)
        .owned_by(request.user).filter(hearing=hearing).select_related('candidate')
        .order_by('issued_at', 'pk'))
    provisional_entries = []
    if hearing.status == PanelHearing.Status.OPEN and not result_rows:
        activity = panel_live_activity(hearing)
        provisional_entries = activity.get('candidates') or activity.get('entries', [])

    criteria_count = len(snapshots) if snapshots else hearing.criteria.count()

    response = render(request, 'service_domains/panel_results_review.html', {
        'hearing': hearing, 'result_rows': result_rows, 'action_error': action_error,
        'provisional_entries': provisional_entries,
        'criteria_count': criteria_count,
        'active_tab': 'results',
        'moderation_comments': moderation_comments,
        'entrant_candidates': entrant_candidates, 'entrant_links': entrant_links,
        'decision_rows': decision_rows,
        'show_decisions': hearing.workflow in (PanelHearing.Workflow.OUTCOME,
            PanelHearing.Workflow.VOTE, PanelHearing.Workflow.HYBRID,
            PanelHearing.Workflow.REVIEW, PanelHearing.Workflow.REVIEW_VOTE,
            PanelHearing.Workflow.SCORED),
        'show_votes': hearing.workflow in (PanelHearing.Workflow.VOTE,
            PanelHearing.Workflow.HYBRID, PanelHearing.Workflow.REVIEW_VOTE),
        'uses_rubric_scoring': hearing.workflow in (
            PanelHearing.Workflow.SCORED, PanelHearing.Workflow.HYBRID),
        'one_time_entrant_url': one_time_entrant_url,
    }, status=400 if action_error or adjustment_errors else 200)
    if request.method == 'POST' and (action_error or adjustment_errors):
        messages.error(request, action_error or 'Please correct the highlighted score adjustment and try again.')
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    return response


@require_safe
@never_cache
@_panel_owner
def panel_oversight_list(request):
    """Organization overseers may inspect compiled, aggregate Panel records."""
    if not can_oversee(request):
        raise Http404
    base_qs = (PanelHearing.objects.for_organization(request.organization)
        .exclude(status__in=(PanelHearing.Status.DRAFT, PanelHearing.Status.OPEN))
        .filter(result_snapshots__isnull=False,
                result_snapshots__organization_id=request.organization['id'])
        .select_related('created_by')
        .annotate(result_count=Count('result_snapshots', distinct=True),
                  candidate_count=Count('candidates', distinct=True)))

    total_panels = base_qs.count()
    total_signed_off = base_qs.filter(status=PanelHearing.Status.SIGNED_OFF).count()
    total_released = base_qs.filter(status=PanelHearing.Status.RELEASED).count()
    total_candidates = base_qs.aggregate(total=Sum('candidate_count'))['total'] or 0
    total_outcomes = base_qs.aggregate(total=Sum('result_count'))['total'] or 0

    stats = {
        'total_panels': total_panels,
        'total_signed_off': total_signed_off,
        'total_released': total_released,
        'total_candidates': total_candidates,
        'total_outcomes': total_outcomes,
    }

    status_counts = {
        'total': total_panels,
        'signed_off': total_signed_off,
        'released': total_released,
    }

    filtered_qs = base_qs
    search_query = (request.GET.get('q') or '').strip()[:100]
    if search_query:
        filtered_qs = filtered_qs.filter(
            Q(name__icontains=search_query) |
            Q(created_by__first_name__icontains=search_query) |
            Q(created_by__last_name__icontains=search_query) |
            Q(created_by__email__icontains=search_query)
        )

    status_filter = request.GET.get('status', '').strip()
    if status_filter in (PanelHearing.Status.SIGNED_OFF, PanelHearing.Status.RELEASED):
        filtered_qs = filtered_qs.filter(status=status_filter)
    else:
        status_filter = ''

    workflow_filter = request.GET.get('workflow', '').strip()
    if workflow_filter in PanelHearing.Workflow.values:
        filtered_qs = filtered_qs.filter(workflow=workflow_filter)
    else:
        workflow_filter = ''

    filtered_qs = filtered_qs.order_by('-created_at', '-pk')
    filtered_count = filtered_qs.count()

    paginator, page_obj = paginate(request, filtered_qs)

    query_params = QueryDict('', mutable=True)
    if search_query:
        query_params['q'] = search_query
    if status_filter:
        query_params['status'] = status_filter
    if workflow_filter:
        query_params['workflow'] = workflow_filter
    query_params['per_page'] = str(paginator.per_page)
    querystring = query_params.urlencode()

    context = {
        'hearings': filtered_qs,
        'page_obj': page_obj,
        'paginator': paginator,
        'stats': stats,
        'status_counts': status_counts,
        'search_query': search_query,
        'status_filter': status_filter,
        'workflow_filter': workflow_filter,
        'workflow_choices': PanelHearing.Workflow.choices,
        'filtered_count': filtered_count,
        'per_page': paginator.per_page,
        'page_sizes': PAGE_SIZES,
        'querystring': querystring,
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        response = render(request, 'service_domains/_partials/panel_oversight_results.html', context)
        response['Vary'] = 'X-Requested-With'
        return response

    response = render(request, 'service_domains/panel_oversight_list.html', context)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_safe
@never_cache
@_panel_owner
def panel_oversight_detail(request, hearing_id):
    """Read-only compiled Panel scores, with no examiner-level records."""
    if not can_oversee(request):
        raise Http404
    hearing = (PanelHearing.objects.for_organization(request.organization)
        .exclude(status__in=(PanelHearing.Status.DRAFT, PanelHearing.Status.OPEN))
        .filter(pk=hearing_id, result_snapshots__isnull=False,
                result_snapshots__organization_id=request.organization['id'])
        .select_related('created_by').first())
    if hearing is None:
        raise Http404
    snapshots = list(PanelResultSnapshot.objects.for_organization(request.organization)
        .filter(hearing=hearing).select_related('candidate', 'criterion')
        .order_by('candidate__order', 'criterion__order', 'pk'))
    adjustments = (PanelResultAdjustment.objects.for_organization(request.organization)
        .filter(snapshot__in=snapshots).select_related('adjusted_by')
        .order_by('snapshot_id', '-revision'))
    latest_adjustments = {}
    for adjustment in adjustments:
        latest_adjustments.setdefault(adjustment.snapshot_id, adjustment)
    result_rows = [{
        'snapshot': snapshot,
        'latest_adjustment': latest_adjustments.get(snapshot.pk),
    } for snapshot in snapshots]
    response = render(request, 'service_domains/panel_oversight_detail.html', {
        'hearing': hearing, 'result_rows': result_rows,
    })
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_safe
@never_cache
def panel_entrant_results(request, token):
    """Show one released candidate's aggregate results through a revocable capability."""
    if getattr(request, 'service', None) != 'panel':
        raise Http404
    link = resolve_panel_entrant_result_link(token)
    if link is None:
        raise Http404
    snapshots = list(PanelResultSnapshot.objects.filter(
        hearing=link.hearing, candidate=link.candidate)
        .select_related('criterion').order_by('criterion__order', 'pk'))
    result_rows = []
    for snapshot in snapshots:
        latest_adjustment = (PanelResultAdjustment.objects.filter(snapshot=snapshot)
            .order_by('-revision', '-pk').first())
        result_rows.append({
            'snapshot': snapshot,
            'effective_score': (latest_adjustment.adjusted_score
                if latest_adjustment else snapshot.score),
            'released_comments': list(PanelScore.objects.filter(candidate=link.candidate,
                criterion=snapshot.criterion, comment_released_to_entrant=True)
                .exclude(comment='').filter(comment_redaction__isnull=True)
                .order_by('pk').values_list('comment', flat=True)),
        })
    decision = (PanelDecision.objects.filter(hearing=link.hearing, candidate=link.candidate)
        .order_by('-revision').first())
    response = render(request, 'service_domains/panel_entrant_results.html', {
        'hearing': link.hearing, 'candidate': link.candidate,
        'result_rows': result_rows,
        'decision': decision,
    })
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    return response


def _panel_attempt_action(request):
    action = request.POST.get('action')
    if action == 'conflict':
        return PanelInvitationAttempt.Action.CONFLICT
    if action == 'score':
        return PanelInvitationAttempt.Action.SCORE
    if action == 'abstain':
        return PanelInvitationAttempt.Action.ABSTAIN
    if action == 'vote':
        return PanelInvitationAttempt.Action.VOTE
    if action == 'review':
        return PanelInvitationAttempt.Action.REVIEW
    return PanelInvitationAttempt.Action.INVALID


@require_http_methods(['GET', 'POST'])
@never_cache
def panel_examiner(request, token):
    """Bearer-link examiner workspace; no LiveGrade owner account is required."""
    if getattr(request, 'service', None) != 'panel':
        raise Http404
    invitation = resolve_panel_invitation(token)
    if invitation is None:
        known_invitation = None
        if token and len(token) <= 256:
            known_invitation = (PanelInvitation.objects
                .filter(token_digest=PanelInvitation.digest_token(token)).first())
        if known_invitation is None:
            unknown_allowed = allow_unknown_bearer_request(request, service='panel-examiner')
            if unknown_allowed is None:
                return HttpResponse('The examiner service is temporarily unavailable.', status=503)
            if not unknown_allowed:
                response = HttpResponse('Too many requests for examiner links.', status=429)
                response['Retry-After'] = str(settings.PUBLIC_BEARER_UNKNOWN_RATE_WINDOW)
                return response
        else:
            allowed = allow_panel_examiner_request(known_invitation)
            if allowed is None:
                outcome = PanelInvitationAttempt.Outcome.UNAVAILABLE
                reason = PanelInvitationAttempt.Reason.THROTTLE_UNAVAILABLE
            elif not allowed:
                outcome = PanelInvitationAttempt.Outcome.RATE_LIMITED
                reason = PanelInvitationAttempt.Reason.RATE_LIMIT
            else:
                outcome = PanelInvitationAttempt.Outcome.REJECTED
                reason = PanelInvitationAttempt.Reason.INVITATION_UNUSABLE
            if request.method == 'POST':
                record_panel_invitation_attempt(known_invitation,
                    _panel_attempt_action(request), outcome, reason)
        raise Http404
    is_state_request = request.method == 'GET' and request.GET.get('state') == '1'
    if is_state_request:
        allowed = allow_panel_examiner_state_request(invitation)
    else:
        allowed = allow_panel_examiner_request(invitation)
    if allowed is None:
        if request.method == 'POST':
            record_panel_invitation_attempt(invitation, _panel_attempt_action(request),
                PanelInvitationAttempt.Outcome.UNAVAILABLE,
                PanelInvitationAttempt.Reason.THROTTLE_UNAVAILABLE)
        return HttpResponse('The examiner service is temporarily unavailable.', status=503)
    if not allowed:
        if request.method == 'POST':
            record_panel_invitation_attempt(invitation, _panel_attempt_action(request),
                PanelInvitationAttempt.Outcome.RATE_LIMITED,
                PanelInvitationAttempt.Reason.RATE_LIMIT)
        response = HttpResponse('Too many requests for this examiner link.', status=429)
        response['Retry-After'] = str(settings.PANEL_EXAMINER_RATE_WINDOW)
        return response
    if invitation.hearing.status not in (PanelHearing.Status.DRAFT, PanelHearing.Status.OPEN):
        if request.method == 'POST':
            record_panel_invitation_attempt(invitation, _panel_attempt_action(request),
                PanelInvitationAttempt.Outcome.REJECTED,
                PanelInvitationAttempt.Reason.HEARING_CLOSED)
        raise Http404
    cookie_name = f'{PANEL_EXAMINER_COOKIE_PREFIX}{invitation.pk}'
    device_cookie = request.COOKIES.get(cookie_name, '')
    if request.method == 'GET' and request.GET.get('state') == '1':
        if (not invitation.device_digest or not valid_panel_device_cookie(invitation, device_cookie)
                or not hmac.compare_digest(hashlib.sha256(device_cookie.encode('utf-8')).hexdigest(),
                                           invitation.device_digest)):
            return HttpResponse('Confirm this invitation in the browser that accepted it.', status=403)
        hearing = invitation.hearing
        return JsonResponse({
            'status': hearing.status,
            'active_candidate_id': hearing.active_candidate_id,
            'active_candidate_name': (hearing.active_candidate.display_name
                if hearing.active_candidate_id else ''),
        }, headers={'Cache-Control': 'private, no-store', 'Referrer-Policy': 'same-origin'})
    if request.method == 'POST' and request.POST.get('action') == 'decline':
        try:
            decline_panel_invitation(invitation)
        except ValidationError as exc:
            response = render(request, 'service_domains/panel_examiner_bind.html', {
                'hearing': invitation.hearing,
                'masked_email': _masked_invitation_email(invitation.email),
                'action_error': '; '.join(exc.messages),
            }, status=400)
        else:
            response = render(request, 'service_domains/panel_examiner_bind.html', {
                'hearing': invitation.hearing,
                'masked_email': _masked_invitation_email(invitation.email),
                'declined': True,
            })
        return _panel_examiner_headers(response)
    if request.method == 'POST' and request.POST.get('action') == 'bind':
        device_cookie, device_allowed, newly_bound = bind_panel_invitation_device(
            invitation, device_cookie)
        if not device_allowed:
            return _panel_device_mismatch(invitation)
        record_panel_invitation_attempt(invitation, PanelInvitationAttempt.Action.OPEN,
            PanelInvitationAttempt.Outcome.ACCEPTED,
            PanelInvitationAttempt.Reason.SUBMITTED)
        messages.success(request, 'This browser is linked to your examiner invitation.')
        response = redirect('panel_examiner', token=token)
        if newly_bound:
            _set_panel_examiner_cookie(response, request, cookie_name,
                device_cookie, invitation)
        return response
    elif not invitation.device_digest and request.method == 'GET':
        if not valid_panel_device_cookie(invitation, device_cookie):
            device_cookie = issue_panel_device_cookie(invitation)
            set_pending_cookie = True
        else:
            set_pending_cookie = False
        response = render(request, 'service_domains/panel_examiner_bind.html', {
            'hearing': invitation.hearing,
            'masked_email': _masked_invitation_email(invitation.email),
        })
        _panel_examiner_headers(response)
        if set_pending_cookie:
            _set_panel_examiner_cookie(response, request, cookie_name,
                device_cookie, invitation)
        return response
    elif invitation.device_digest:
        device_cookie, device_allowed, _ = bind_panel_invitation_device(
            invitation, device_cookie)
        if not device_allowed:
            return _panel_device_mismatch(invitation)
    else:
        return HttpResponse('Confirm this invitation in the browser that opened it before scoring.',
            status=403)

    hearing = invitation.hearing
    candidates = list(PanelCandidate.objects.filter(hearing=hearing).order_by('order', 'pk'))
    criteria = list(invitation.panel.criteria.filter(hearing=hearing).order_by('order', 'pk'))
    form_error = ''
    submitted_review_form = None
    submitted_review_candidate_id = None

    if request.method == 'POST':
        action = request.POST.get('action')
        attempt_action = _panel_attempt_action(request)
        if attempt_action == PanelInvitationAttempt.Action.INVALID:
            record_panel_invitation_attempt(invitation, attempt_action,
                PanelInvitationAttempt.Outcome.REJECTED,
                PanelInvitationAttempt.Reason.INVALID_ACTION)
            return HttpResponse('Invalid examiner action.', status=400)
        try:
            candidate = next((item for item in candidates
                              if str(item.pk) == request.POST.get('candidate_id')), None)
            if candidate is None:
                raise Http404
            if action == 'conflict':
                declare_panel_conflict(token, candidate, request.POST.get('reason', ''))
            elif action == 'vote':
                vote_form = PanelVoteForm(request.POST)
                if not vote_form.is_valid():
                    raise ValidationError('Choose a vote option and check the form.')
                submit_panel_vote(token, candidate, vote_form.cleaned_data['choice'])
                record_panel_invitation_attempt(invitation, PanelInvitationAttempt.Action.VOTE,
                    PanelInvitationAttempt.Outcome.ACCEPTED, PanelInvitationAttempt.Reason.SUBMITTED)
            elif action == 'review':
                report_form = PanelWrittenReviewForm(request.POST)
                previous_report = PanelWrittenReview.objects.filter(
                    invitation=invitation, candidate=candidate).exists()
                report_form.fields['revision_reason'].required = previous_report
                if not report_form.is_valid():
                    submitted_review_form = report_form
                    submitted_review_candidate_id = candidate.pk
                    errors = '; '.join(f'{field}: {", ".join(items)}'
                        for field, items in report_form.errors.items())
                    raise ValidationError(errors or 'Complete the written review before submitting it.')
                submit_panel_written_review(token, candidate, report_form.cleaned_data['report'],
                    report_form.cleaned_data['revision_reason'])
                record_panel_invitation_attempt(invitation, PanelInvitationAttempt.Action.REVIEW,
                    PanelInvitationAttempt.Outcome.ACCEPTED, PanelInvitationAttempt.Reason.SUBMITTED)
            else:
                criterion = next((item for item in criteria
                                  if str(item.pk) == request.POST.get('criterion_id')), None)
                if criterion is None:
                    raise Http404
                if action == 'abstain':
                    abstain_panel_score(token, candidate, criterion, reason=request.POST.get('abstention_reason', ''))
                else:
                    submit_panel_score(token, candidate, criterion,
                        request.POST.get('score', ''), request.POST.get('comment', ''))
        except Http404:
            record_panel_invitation_attempt(invitation, attempt_action,
                PanelInvitationAttempt.Outcome.REJECTED,
                PanelInvitationAttempt.Reason.INVALID_TARGET)
            raise
        except PermissionDenied as exc:
            record_panel_invitation_attempt(invitation, attempt_action,
                PanelInvitationAttempt.Outcome.REJECTED,
                PanelInvitationAttempt.Reason.NOT_ALLOWED)
            form_error = '; '.join(getattr(exc, 'messages', [])) or 'This action cannot be accepted.'
        except ValidationError as exc:
            record_panel_invitation_attempt(invitation, attempt_action,
                PanelInvitationAttempt.Outcome.REJECTED,
                PanelInvitationAttempt.Reason.INVALID_INPUT)
            form_error = '; '.join(getattr(exc, 'messages', [])) or 'This action cannot be accepted.'
        except IntegrityError as exc:
            record_panel_invitation_attempt(invitation, attempt_action,
                PanelInvitationAttempt.Outcome.REJECTED,
                PanelInvitationAttempt.Reason.DUPLICATE)
            form_error = '; '.join(getattr(exc, 'messages', [])) or 'This action cannot be accepted.'
        else:
            messages.success(request, {
                'conflict': 'Conflict declaration recorded.',
                'vote': 'Private vote recorded.',
                'review': 'Written review submitted.',
                'abstain': 'Abstention recorded.',
                'score': 'Score submitted.',
            }.get(action, 'Your response was recorded.'))
            broadcast_panel_hearing_event(invitation.hearing, f'{action}.submitted', {
                'examiner_id': invitation.pk,
                'action': action,
            })
            return redirect('panel_examiner', token=token)

    conflicts = set(PanelConflict.objects.filter(invitation=invitation)
                    .values_list('candidate_id', flat=True))
    scores = {(score.candidate_id, score.criterion_id): score
              for score in PanelScore.objects.filter(invitation=invitation)}
    abstentions = set(PanelAbstention.objects.filter(invitation=invitation)
                      .values_list('candidate_id', 'criterion_id'))
    own_votes = {vote.candidate_id: vote for vote in PanelVote.objects.filter(invitation=invitation)}
    own_reviews = {}
    for review in PanelWrittenReview.objects.filter(invitation=invitation).order_by('-revision'):
        own_reviews.setdefault(review.candidate_id, review)
    candidate_rows = [{
        'candidate': candidate,
        'conflicted': candidate.pk in conflicts,
        'active': hearing.status == PanelHearing.Status.OPEN and hearing.active_candidate_id == candidate.pk,
        'can_declare_conflict': candidate.pk not in conflicts
            and not any(cid == candidate.pk for cid, _ in scores)
            and not any(cid == candidate.pk for cid, _ in abstentions)
            and candidate.pk not in own_votes and candidate.pk not in own_reviews,
        'vote': own_votes.get(candidate.pk),
        'written_review': own_reviews.get(candidate.pk),
        'criteria': [{'criterion': criterion,
                     'levels': list(criterion.levels.all()),
                     'score': scores.get((candidate.pk, criterion.pk)),
                     'abstained': (candidate.pk, criterion.pk) in abstentions}
                    for criterion in criteria],
    } for candidate in candidates]
    if request.method == 'POST' and form_error:
        messages.error(request, form_error)
    for row in candidate_rows:
        row['vote_form'] = PanelVoteForm()
        row['review_form'] = (submitted_review_form
            if row['candidate'].pk == submitted_review_candidate_id else PanelWrittenReviewForm())
        row['review_form'].fields['revision_reason'].required = bool(row['written_review'])
    response = render(request, 'service_domains/panel_examiner.html', {
        'hearing': hearing, 'invitation': invitation, 'token': token, 'candidate_rows': candidate_rows,
        'form_error': form_error,
    }, status=400 if form_error else 200)
    response['Referrer-Policy'] = 'same-origin'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    response['Cache-Control'] = 'private, no-store'
    return response


def _panel_examiner_headers(response):
    response['Referrer-Policy'] = 'same-origin'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    response['Cache-Control'] = 'private, no-store'
    return response


def _panel_live_display_link_for_request(request, token):
    """Resolve and throttle a public link that returns aggregate progress only."""
    if getattr(request, 'service', None) != 'panel':
        raise Http404
    link = resolve_panel_live_display_link(token)
    if link is None:
        allowed = allow_unknown_bearer_request(request, service='panel-live-display')
        if allowed is None:
            return HttpResponse('The live display is temporarily unavailable.', status=503)
        if not allowed:
            response = HttpResponse('Too many requests for live display links.', status=429)
            response['Retry-After'] = str(settings.PUBLIC_BEARER_UNKNOWN_RATE_WINDOW)
            return response
        raise Http404
    allowed = allow_public_event_request(request, service='panel-live-display',
        event_id=link.pk, limit=3000, window=60)
    if allowed is None:
        return HttpResponse('The live display is temporarily unavailable.', status=503)
    if not allowed:
        response = HttpResponse('The live display is receiving too many requests.', status=429)
        response['Retry-After'] = '60'
        return response
    return link


def _panel_live_display_headers(response):
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    return response


@require_safe
@never_cache
def panel_live_display(request, token):
    """Large-screen public view with no examiner-level records."""
    link = _panel_live_display_link_for_request(request, token)
    if isinstance(link, HttpResponse):
        return link
    activity = panel_live_activity(link.hearing)
    response = render(request, 'service_domains/panel_live_display.html', {
        'hearing': link.hearing,
        'activity': activity,
        'updates_url': reverse('panel_live_display_updates', kwargs={'token': token}),
    })
    return _panel_live_display_headers(response)


@require_safe
@never_cache
def panel_live_display_updates(request, token):
    """Polling endpoint for aggregate progress; never returns individual responses."""
    link = _panel_live_display_link_for_request(request, token)
    if isinstance(link, HttpResponse):
        return link
    response = JsonResponse(panel_live_activity(link.hearing))
    return _panel_live_display_headers(response)


def _panel_device_mismatch(invitation):
    record_panel_invitation_attempt(invitation, PanelInvitationAttempt.Action.OPEN,
        PanelInvitationAttempt.Outcome.REJECTED,
        PanelInvitationAttempt.Reason.DEVICE_MISMATCH)
    return _panel_examiner_headers(HttpResponse(
        'This invitation is already linked to another browser. Ask the organiser for a new invitation.',
        status=403))


def _masked_invitation_email(email):
    local, separator, domain = email.partition('@')
    if not separator or not local:
        return 'the invited email address'
    return f'{local[0]}***@{domain}'


def _set_panel_examiner_cookie(response, request, name, value, invitation):
    max_age = max(1, int((invitation.expires_at - timezone.now()).total_seconds()))
    response.set_cookie(name, value, max_age=max_age, httponly=True,
        secure=request.is_secure(), samesite='Strict', path='/panel/examiner/')


def _owned_training_series(request, series_id):
    try:
        return get_owned_series(request.organization, request.user, series_id)
    except TrainingSeriesUnavailable:
        raise Http404


@require_safe
@never_cache
@_team_owner
def training_series_trends(request, series_id):
    series = _owned_training_series(request, series_id)
    try:
        trend_data = training_series_competency_trends(series,
            organization=request.organization, user=request.user)
    except PermissionDenied:
        raise Http404
    from_session = None
    if request.GET.get('session', '').isdigit():
        from_session = (TrainingSeriesSession.objects.for_organization(request.organization)
                        .owned_by(request.user).select_related('assessment_session')
                        .filter(pk=int(request.GET['session']), series=series).first())
    return render(request, 'service_domains/team_series_trends.html', {
        'from_session': from_session, 'series': series, 'trends': trend_data['learners'],
        'team_trends': trend_data['teams'],
        'session_count': series.series_sessions.count(),
        'feedback_summaries': training_series_feedback_summary(
            series, organization=request.organization, user=request.user),
    })


def _csv_cell(value):
    """Prevent spreadsheet cells from being interpreted as formulas."""
    text = str(value)
    if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
        return "'" + text
    return text


@require_safe
@never_cache
@_team_owner
def training_series_export(request, series_id):
    """Download a scoped CSV of latest competency outcomes, excluding evidence."""
    series = _owned_training_series(request, series_id)
    try:
        rows = training_series_competency_export(series,
            organization=request.organization, user=request.user)
    except PermissionDenied:
        raise Http404
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(('series', 'demo_sequence', 'demo_session', 'learner_reference',
                     'learner_name', 'competency', 'score', 'passing_score',
                     'passed', 'revision', 'assessed_at'))
    for row in rows:
        # nosemgrep: python.django.security.injection.csv-writer-injection.csv-writer-injection
        writer.writerow(tuple(_csv_cell(row[key]) for key in (
            'series', 'sequence', 'session', 'learner_reference', 'learner_name',
            'competency', 'score', 'passing_score')) + (
                'passed' if row['passed'] else 'not passed',
                row['revision'], row['assessed_at'].isoformat()))
    response = HttpResponse(output.getvalue(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="team-series-{series.pk}-outcomes.csv"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
def training_anonymous_feedback(request, feedback_public_id):
    """Capability-link feedback for a closed Team demo; no account identity is read or stored."""
    if getattr(request, 'service', None) != 'team':
        raise Http404
    allowed = allow_public_event_request(request, service='team-feedback',
        event_id=feedback_public_id)
    if allowed is not True:
        response = HttpResponse(
            'Feedback is temporarily unavailable. Please try again shortly.',
            status=429 if allowed is False else 503)
        response['Cache-Control'] = 'no-store'
        response['Retry-After'] = str(settings.PUBLIC_BEARER_UNKNOWN_RATE_WINDOW)
        return response
    series_link = (TrainingSeriesSession.objects.select_related(
        'series', 'assessment_session').filter(feedback_public_id=feedback_public_id).first())
    if (series_link is None or not series_link.series.active
            or series_link.series.feedback_minimum_responses is None
            or series_link.assessment_session.status not in (
                AssessmentSession.Status.LIVE, AssessmentSession.Status.CLOSED
            )):
        raise Http404

    cookie_name = f'{TEAM_FEEDBACK_COOKIE_PREFIX}{series_link.feedback_public_id.hex}'
    token = request.COOKIES.get(cookie_name, '')
    token_valid = False
    device_digest = ''
    is_live = (series_link.assessment_session.status == AssessmentSession.Status.LIVE)
    if token:
        try:
            device_digest = TrainingFeedback.digest_device_token(token, series_link)
            token_valid = True
        except ValidationError:
            token = ''
    already_submitted = (token_valid and TrainingFeedback.objects.filter(
        organization_id=series_link.organization_id, series_session=series_link,
        device_digest=device_digest).exists())
    form = TrainingFeedbackForm(request.POST or None)
    error = ''
    submitted = False
    if request.method == 'POST':
        if is_live:
            error = 'Feedback is accepted after the demo session closes.'
        elif not token_valid:
            error = 'The anonymous response token expired. Reload this page and submit again.'
        elif already_submitted:
            error = 'This browser has already submitted feedback for this demo.'
        elif form.is_valid():
            try:
                submit_training_feedback(series_link.feedback_public_id, token,
                    int(form.cleaned_data['score']))
            except ValidationError as exc:
                error = '; '.join(exc.messages)
            else:
                submitted = True

    response = render(request, 'service_domains/team_feedback.html', {
        'series_link': series_link, 'form': form, 'error': error,
        'submitted': submitted, 'already_submitted': already_submitted,
        'is_live': is_live,
    }, status=400 if error or (request.method == 'POST' and not submitted and not form.is_valid()) else 200)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    if not token_valid:
        fresh_token = TrainingFeedback.issue_device_token(series_link)
        response.set_cookie(cookie_name, fresh_token,
            max_age=TrainingFeedback.FEEDBACK_TOKEN_MAX_AGE, httponly=True,
            secure=request.is_secure(), samesite='Lax',
            path=f'/team/feedback/{series_link.feedback_public_id}/')
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
def competition_audience_vote(request, competition_id, round_id, track_id):
    """Public ranked-choice ballot scoped to one active competition round and track."""
    if getattr(request, 'service', None) != 'competition':
        raise Http404
    competition = Competition.objects.filter(public_id=competition_id).first()
    if competition is None or competition.status != Competition.Status.ACTIVE:
        raise Http404
    round_obj = CompetitionRound.objects.filter(pk=round_id, competition=competition).first()
    track = CompetitionTrack.objects.filter(pk=track_id, competition=competition).first()
    latest_round_id = (CompetitionRound.objects.filter(competition=competition)
        .order_by('-order').values_list('pk', flat=True).first())
    if (round_obj is None or track is None or round_obj.pk != latest_round_id):
        raise Http404

    voting_locked = bool(round_obj.audience_locked_at is not None)
    if not voting_locked:
        try:
            from .operations import get_or_create_competition_live_state
            live_state = get_or_create_competition_live_state(competition)
            if not live_state.audience_voting_open:
                voting_locked = True
        except Exception:
            pass

    entries = list(CompetitionRoundEntry.objects.filter(round=round_obj, track=track)
        .select_related('entry').order_by('entry__order', 'entry_id'))
    if not entries:
        raise Http404
    if competition.effective_audience_weight == 0 and len(entries) < 2:
        raise Http404

    cookie_name = (f'{COMPETITION_AUDIENCE_COOKIE_PREFIX}{competition.public_id.hex}_'
                   f'{round_obj.pk}_{track.pk}')
    token = request.COOKIES.get(cookie_name, '')
    token_valid = False
    device_digest = ''
    if token:
        try:
            device_digest = CompetitionAudienceBallot.digest_device_token(
                token, competition, round_obj, track)
            token_valid = True
        except ValidationError:
            token = ''
    already_submitted = (token_valid and CompetitionAudienceBallot.objects.filter(
        organization_id=competition.organization_id, competition=competition,
        round=round_obj, track=track, device_digest=device_digest).exists())
    form = CompetitionAudienceBallotForm(
        request.POST if request.method == 'POST' else None,
        round_obj=round_obj, track=track,
        require_complete=competition.effective_audience_weight > 0)
    error = ''
    submitted = False
    if request.method == 'POST':
        if voting_locked:
            error = 'Audience voting is currently paused or closed for this round.'
        elif not token_valid:
            error = 'The audience ballot token expired. Reload this page and submit again.'
        elif already_submitted:
            error = 'This browser has already voted in this round and track.'
        elif form.is_valid():
            try:
                submit_competition_audience_ballot(
                    competition.public_id, round_obj.pk, track.pk, token,
                    form.cleaned_data['ranking'])
            except ValidationError as exc:
                error = '; '.join(exc.messages)
            else:
                submitted = True

    response = render(request, 'service_domains/competition_audience_vote.html', {
        'competition': competition, 'round': round_obj, 'track': track,
        'form': form, 'error': error, 'submitted': submitted,
        'already_submitted': already_submitted,
        'voting_locked': voting_locked,
        'voting_locked_message': ('Audience voting for this round has ended.'
                                  if round_obj.audience_locked_at
                                  else 'Audience voting is currently paused by the organizer. Please stay tuned.'),
    }, status=400 if error or (
        request.method == 'POST' and not submitted and not already_submitted and not form.is_valid()) else 200)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    if not token_valid:
        fresh_token = CompetitionAudienceBallot.issue_device_token(competition, round_obj, track)
        response.set_cookie(cookie_name, fresh_token,
            max_age=CompetitionAudienceBallot.DEVICE_TOKEN_MAX_AGE, httponly=True,
            secure=request.is_secure(), samesite='Lax',
            path=f'/competition/audience/{competition.public_id}/{round_obj.pk}/{track.pk}/vote/')
    return response


@require_safe
@never_cache
@_team_owner
def training_series_list(request):
    context = dashboard_data(request.organization, request.user)
    latest_series = context['recent_series'][0] if context['recent_series'] else None
    onboarding = service_onboarding('team',
        resume_url=(reverse('training_series_manage', args=(latest_series.pk,))
                    if latest_series else ''),
        resume_name=(latest_series.name if latest_series else ''))
    onboarding['create_anchor'] = reverse('training_series_create')
    if latest_series is None:
        onboarding['steps'] = (
            ('Create a series', 'Name the series and add its purpose.'),
            ('Add your team', 'Import a roster or add team members.'),
            ('Define competencies and run demos', 'Set pass marks, then record each demo.'),
        )
    context['onboarding'] = onboarding
    return render(request, 'service_domains/team_series.html', context)


@require_safe
@never_cache
@_team_owner
def training_series_table(request):
    context = series_list_data(request, request.organization, request.user)
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        response = render(request, 'service_domains/_partials/team_series_results.html', context)
        response['Vary'] = 'X-Requested-With'
        return response
    return render(request, 'service_domains/team_series_table.html', context)


_HEARING_CREATE_STEPS = unsaved_steps([
    ('details', 'Hearing info'), ('candidates', 'Candidates'),
    ('scoring', 'Criteria and weights'), ('examiners', 'Panel examiners'),
    ('ready', 'Launch or schedule'),
], hint='Save the hearing info to unlock this step')


def _hearing_steps(step_completion, requires_examiners, active):
    """Stepper steps for the hearing workspace; scoring and examiners only
    apply to workflows that need examiners."""
    labels = [('details', 'Hearing info'), ('candidates', 'Candidates')]
    if requires_examiners:
        labels += [('scoring', 'Criteria and weights'), ('examiners', 'Panel examiners')]
    labels.append(('ready', 'Review and launch'))
    return build_steps([{'key': key, 'label': label, 'done': step_completion[key]}
                        for key, label in labels], active)


def _new_training_series_context(request, details_form):
    series = TrainingSeries(organization_id=organization_id(request.organization),
        created_by=request.user)
    return {
        'series': series,
        'participants': [],
        'competencies': [],
        'sessions': [],
        'participant_form': TrainingParticipantForm(auto_id='participant_%s'),
        'competency_form': TrainingCompetencyForm(auto_id='competency_%s'),
        'launch_form': CreateTrainingSessionForm(
            organization=request.organization, user=request.user, auto_id='launch_%s'),
        'privacy_form': TrainingFeedbackPrivacyForm(),
        'has_team_templates': (SessionTemplate.objects.for_organization(request.organization)
            .for_service('team').owned_by(request.user).exists()),
        'roster_import_form': TrainingRosterImportForm(),
        'details_form': details_form,
        'roster_import_pending': False,
        'roster_import_plan': None,
        'setup_steps': [
            {'key': 'details', 'label': 'Series setup', 'href': '#series-details', 'complete': False},
            {'key': 'roster', 'label': 'Roster', 'href': '#team-roster', 'complete': False},
            {'key': 'competencies', 'label': 'Competencies', 'href': '#competency-criteria', 'complete': False},
            {'key': 'demos', 'label': 'Session evaluation', 'href': '#series-demos', 'complete': False},
            {'key': 'review', 'label': 'Review', 'href': '#series-review', 'complete': False},
        ],
        'stepper_steps': unsaved_steps([
            ('details', 'Series setup'), ('roster', 'Roster'), ('competencies', 'Competencies'),
            ('demos', 'Session evaluation'), ('review', 'Review'),
        ], hint='Save the series setup to unlock this step'),
        'active_step': 'details',
        'team_starters': starters_for('team'),
        'is_new_series': True,
    }


@require_http_methods(['GET', 'POST'])
@never_cache
@_team_owner
def training_series_create(request):
    details_form = TrainingSeriesDetailsForm(
        request.POST if request.method == 'POST' else None, auto_id='series_details_%s')
    if request.method == 'POST' and details_form.is_valid():
        series = details_form.save(commit=False)
        series.organization_id = organization_id(request.organization)
        series.created_by = request.user
        try:
            series.full_clean()
        except ValidationError as exc:
            details_form.add_error(None, '; '.join(exc.messages))
        else:
            series.save(force_insert=True)
            messages.success(request, 'Training series created.')
            if request.POST.get('advance') == '1':
                return redirect(f"{reverse('training_series_manage', args=(series.pk,))}?step=roster")
            return redirect('training_series_manage', series_id=series.pk)
    return render(request, 'service_domains/team_series_manage.html',
        _new_training_series_context(request, details_form))


def _delete_export_response(content, token):
    response = HttpResponse(content, content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename="team-series-deletion-export.zip"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-Delete-Export-Token'] = token
    return response


@require_safe
@never_cache
@_team_owner
def training_series_delete_export(request, series_id):
    try:
        content, token, _ = build_deletion_export(
            request.organization, request.user, [str(series_id)])
    except (InvalidSeriesSelection, TrainingSeriesUnavailable):
        raise Http404
    return _delete_export_response(content, token)


@_team_owner
@never_cache
@require_POST
def training_series_bulk_delete_export(request):
    try:
        ids = parse_series_ids(request.POST.getlist('series_ids'))
        content, token, _ = build_deletion_export(
            request.organization, request.user, [str(item) for item in ids])
    except InvalidSeriesSelection:
        return HttpResponse('Select one or more valid series to export.', status=400)
    except TrainingSeriesUnavailable:
        raise Http404
    return _delete_export_response(content, token)


def _series_delete_redirect(request):
    query = deletion_redirect_query(request.POST)
    url = reverse('training_series_table')
    return redirect(f'{url}?{query}' if query else url)


def _reject_series_delete(request, message):
    messages.error(request, message)
    return _series_delete_redirect(request)


def _delete_selected_series(request, ids):
    try:
        selected = selected_series_names(request.organization, request.user, ids)
    except TrainingSeriesUnavailable:
        return _reject_series_delete(request,
            'One or more selected series are no longer available to delete.')
    if not verify_deletion_export_token(
            request.POST.get('delete_export_token'), request.organization, request.user, ids):
        return _reject_series_delete(request,
            'Export the selected series and confirm that you saved the archive before deleting.')
    try:
        count = delete_series_selection(request.organization, request.user, ids)
    except TrainingSeriesUnavailable:
        return _reject_series_delete(request,
            'One or more selected series are no longer available to delete.')
    if count == 1:
        messages.success(request, f'"{selected[0].name}" deleted.')
    else:
        messages.success(request, f'{count} series deleted.')
    return _series_delete_redirect(request)


@_team_owner
@never_cache
@require_POST
def training_series_delete(request, series_id):
    try:
        get_owned_series(request.organization, request.user, series_id, active_only=False)
    except TrainingSeriesUnavailable:
        raise Http404
    return _delete_selected_series(request, [series_id])


@_team_owner
@never_cache
@require_POST
def training_series_bulk_delete(request):
    try:
        ids = parse_series_ids(request.POST.getlist('series_ids'))
    except InvalidSeriesSelection:
        return _reject_series_delete(request, 'Select one or more valid series to delete.')
    return _delete_selected_series(request, ids)


@require_http_methods(['GET', 'POST'])
@never_cache
@_team_owner
def training_series_manage(request, series_id):
    series = _owned_training_series(request, series_id)
    manage_url = reverse('training_series_manage', kwargs={'series_id': series.pk})
    from_param = request.GET.get('from') or request.POST.get('from', '')
    session_id_param = request.GET.get('session_id') or request.POST.get('session_id', '')
    if not session_id_param.isdigit():
        session_id_param = ''
    active_session_link = series.series_sessions.exclude(assessment_session__status=AssessmentSession.Status.CLOSED).order_by('-sequence').first()
    target_session_id = session_id_param or (active_session_link.pk if active_session_link else None)
    if (from_param == 'live' or active_session_link) and target_session_id:
        back_url = reverse('training_session_live', kwargs={'series_id': series.pk, 'session_id': target_session_id})
        back_label = 'Back to Live Workshop'
    elif from_param == 'trends':
        back_url = reverse('training_series_trends', kwargs={'series_id': series.pk})
        back_label = 'Back to Competency Trends'
    else:
        back_url = reverse('training_series_table')
        back_label = 'Training series'
    workspace_link = None
    if from_param == 'live' and str(target_session_id).isdigit():
        workspace_link = series.series_sessions.select_related('assessment_session').filter(
            pk=int(target_session_id)).first()
    from_query_parts = []
    if from_param:
        from_query_parts.append(f'from={quote(from_param)}')
    if target_session_id:
        from_query_parts.append(f'session_id={target_session_id}')
    from_query = ('&' + '&'.join(from_query_parts)) if from_query_parts else ''
    participant_form = competency_form = launch_form = privacy_form = None
    roster_import_form = TrainingRosterImportForm()
    roster_import_pending = False
    roster_import_plan = None
    roster_token = request.session.get(TEAM_ROSTER_IMPORT_SESSION_KEY)
    roster_payload = None
    if roster_token:
        try:
            roster_payload = cache.get(_team_roster_import_cache_key(roster_token))
        except Exception:
            roster_payload = None
        if (isinstance(roster_payload, dict) and roster_payload.get('series_id') == series.pk
                and roster_payload.get('organization_id') == series.organization_id
                and roster_payload.get('owner_id') == request.user.pk):
            roster_import_pending = True
            roster_import_plan = plan_training_roster_import(
                series, roster_payload['parsed'],
                organization=request.organization, user=request.user)
        else:
            request.session.pop(TEAM_ROSTER_IMPORT_SESSION_KEY, None)
    details_form = None
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'series_details':
            details_form = TrainingSeriesDetailsForm(
                request.POST, instance=series, auto_id='series_details_%s')
            if details_form.is_valid():
                details_form.save()
                messages.success(request, 'Series details saved.')
                if request.POST.get('advance') == '1':
                    return redirect(f'{manage_url}?step=roster{from_query}')
                return redirect(f'{manage_url}?step=details&saved=1{from_query}')
        elif action == 'feedback_privacy':
            privacy_form = TrainingFeedbackPrivacyForm(request.POST)
            if privacy_form.is_valid():
                series.feedback_minimum_responses = privacy_form.cleaned_data['minimum_responses']
                series.save(update_fields=('feedback_minimum_responses', 'updated_at'))
                messages.success(request, 'Feedback privacy setting saved.')
                if request.POST.get('setup_step'):
                    return redirect(f"{manage_url}?step={request.POST['setup_step']}")
                return redirect('training_series_manage', series_id=series.pk)
        elif action == 'participant':
            participant_form = TrainingParticipantForm(request.POST, auto_id='participant_%s')
            if participant_form.is_valid():
                participant = participant_form.save(commit=False)
                participant.series = series
                participant.organization_id = series.organization_id
                participant.created_by = request.user
                try:
                    participant.full_clean()
                except ValidationError as exc:
                    participant_form.add_error(None, '; '.join(exc.messages))
                else:
                    participant.save(force_insert=True)
                    messages.success(request, 'Team member added.')
                    if request.POST.get('setup_step'):
                        return redirect(f"{manage_url}?step={request.POST['setup_step']}")
                    return redirect('training_series_manage', series_id=series.pk)
        elif action == 'remove_participant':
            participant = get_object_or_404(
                TrainingParticipant.objects.for_organization(request.organization).owned_by(request.user),
                series=series, pk=request.POST.get('participant_id'))
            participant.delete()
            messages.success(request, 'Team member removed.')
            return redirect(f'{manage_url}?step=roster')
        elif action == 'competency':
            competency_form = TrainingCompetencyForm(request.POST, auto_id='competency_%s')
            if competency_form.is_valid():
                competency = competency_form.save(commit=False)
                competency.series = series
                competency.organization_id = series.organization_id
                competency.created_by = request.user
                try:
                    competency.full_clean()
                except ValidationError as exc:
                    competency_form.add_error(None, '; '.join(exc.messages))
                else:
                    competency.save(force_insert=True)
                    messages.success(request, 'Competency criterion added.')
                    if request.POST.get('setup_step'):
                        return redirect(f"{manage_url}?step={request.POST['setup_step']}")
                    return redirect('training_series_manage', series_id=series.pk)
        elif action == 'remove_competency':
            competency = get_object_or_404(
                TrainingCompetency.objects.for_organization(request.organization).owned_by(request.user),
                series=series, pk=request.POST.get('competency_id'))
            competency.delete()
            messages.success(request, 'Competency criterion removed.')
            return redirect(f'{manage_url}?step=competencies')
        elif action == 'create_session':
            launch_form = CreateTrainingSessionForm(request.POST,
                organization=request.organization, user=request.user, auto_id='launch_%s')
            if launch_form.is_valid():
                try:
                    session = create_next_training_session(
                        series, launch_form.cleaned_data['template'],
                        launch_form.cleaned_data['name'],
                        organization=request.organization, user=request.user,
                        identify_by=launch_form.cleaned_data.get('identify_by') or None,
                        scheduled_for=launch_form.cleaned_data.get('scheduled_for'))
                except PermissionDenied:
                    raise Http404
                except ValidationError as exc:
                    launch_form.add_error(None, '; '.join(exc.messages))
                else:
                    messages.success(request,
                        f"Demo session ‘{session.name}’ was created with this series roster and rubric.")
                    return redirect('assessment_session_detail', pk=session.pk)
        else:
            raise Http404

    if participant_form is None:
        participant_form = TrainingParticipantForm(auto_id='participant_%s')
    if competency_form is None:
        competency_form = TrainingCompetencyForm(auto_id='competency_%s')
    if launch_form is None:
        launch_form = CreateTrainingSessionForm(
            organization=request.organization, user=request.user, auto_id='launch_%s')
    if privacy_form is None:
        privacy_form = TrainingFeedbackPrivacyForm(initial={
            'minimum_responses': series.feedback_minimum_responses})
    if details_form is None:
        details_form = TrainingSeriesDetailsForm(
            instance=series, auto_id='series_details_%s')
    participants = TrainingParticipant.objects.for_organization(request.organization).owned_by(
        request.user).filter(series=series)
    competencies = TrainingCompetency.objects.for_organization(request.organization).owned_by(
        request.user).filter(series=series)
    sessions = (series.series_sessions.select_related('assessment_session')
                .order_by('sequence'))
    competency_plan, competency_token = embedded_import.load_preview(
        request, 'training-competency', series, plan_competencies)
    competency_import_errors = embedded_import.take_errors(request, 'training-competency', series)
    setup_steps = [
        {'key': 'details', 'label': 'Series setup', 'href': '#series-details', 'complete': True},
        {'key': 'roster', 'label': 'Roster', 'href': '#team-roster', 'complete': participants.exists()},
        {'key': 'competencies', 'label': 'Competencies', 'href': '#competency-criteria', 'complete': competencies.exists()},
        {'key': 'demos', 'label': 'Session evaluation', 'href': '#series-demos', 'complete': sessions.exists()},
        {'key': 'review', 'label': 'Review', 'href': '#series-review',
         'complete': sessions.exclude(
             assessment_session__status=AssessmentSession.Status.DRAFT).exists()},
    ]
    valid_steps = {step['key'] for step in setup_steps}
    requested_step = request.POST.get('setup_step') or request.GET.get('step')
    if requested_step in valid_steps:
        active_step = requested_step
    elif request.method == 'POST':
        active_step = {
            'series_details': 'details', 'participant': 'roster', 'remove_participant': 'roster',
            'competency': 'competencies', 'remove_competency': 'competencies',
            'create_session': 'demos', 'feedback_privacy': 'demos',
        }.get(request.POST.get('action'), 'details')
    elif roster_import_plan is not None:
        active_step = 'roster'
    elif competency_plan is not None or competency_import_errors:
        active_step = 'competencies'
    else:
        active_step = 'details'
    has_team_templates = (SessionTemplate.objects.for_organization(request.organization)
        .for_service('team').owned_by(request.user).exists())
    live_link = next((link for link in reversed(list(sessions))
        if link.assessment_session.status != AssessmentSession.Status.CLOSED), None)
    return render(request, 'service_domains/team_series_manage.html', {
        'series': series, 'participants': participants, 'competencies': competencies,
        'live_link': live_link,
        'competency_import_form': SpreadsheetImportForm(auto_id='competency_import_%s'),
        'competency_import_plan': competency_plan,
        'competency_import_token': competency_token,
        'competency_import_errors': competency_import_errors,
        'back_url': back_url, 'back_label': back_label, 'from_param': from_param,
        'target_session_id': target_session_id, 'workspace_link': workspace_link,
        'sessions': sessions, 'participant_form': participant_form,
        'competency_form': competency_form, 'launch_form': launch_form,
        'privacy_form': privacy_form, 'has_team_templates': has_team_templates,
        'roster_import_form': roster_import_form, 'details_form': details_form,
        'roster_import_pending': roster_import_pending,
        'roster_import_plan': roster_import_plan,
        'setup_steps': setup_steps,
        'stepper_steps': steps_from_setup(setup_steps, active_step),
        'active_step': active_step,
        'team_starters': starters_for('team'),
    })


def _team_roster_import_cache_key(token):
    return f'livegrade:team:roster-import:{token}'


@require_POST
@never_cache
@_team_owner
def training_competency_import(request, series_id):
    """Preview, apply or cancel a competency spreadsheet; returns to the Competencies step."""
    series = _owned_training_series(request, series_id)
    manage_url = reverse('training_series_manage', args=(series.pk,))
    return embedded_import.handle_post(
        request, kind='training-competency', owner=series, form_class=SpreadsheetImportForm,
        parse=parse_competencies, plan=plan_competencies, apply=apply_competencies,
        success='Competencies imported.',
        back=lambda: redirect(f'{manage_url}?step=competencies'))


@require_safe
@never_cache
@_team_owner
def training_competency_import_sample(request, series_id):
    _owned_training_series(request, series_id)
    response = HttpResponse(training_competency_sample().getvalue(), content_type=(
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))
    response['Content-Disposition'] = 'attachment; filename="competencies-sample.xlsx"'
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
@_team_owner
def training_series_roster_import(request, series_id):
    if request.method == 'GET':
        series = _owned_training_series(request, series_id)
        return redirect(f"{reverse('training_series_manage', args=(series.pk,))}?step=roster")
    if request.POST.get('action') in ('apply', 'cancel'):
        return training_series_roster_preview(request, series_id)
    series = _owned_training_series(request, series_id)
    manage_url = reverse('training_series_manage', kwargs={'series_id': series.pk})
    embedded = request.GET.get('embedded') == '1' or 'setup_step' in request.POST
    target_manage_url = f'{manage_url}?step=roster' if embedded else manage_url
    form = TrainingRosterImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, '; '.join(
            message for errors in form.errors.values() for message in errors))
        return redirect(target_manage_url)
    parsed = parse_training_roster(form.cleaned_data['file'])
    if parsed['fatal']:
        messages.error(request, parsed['fatal'])
        return redirect(target_manage_url)
    token = secrets.token_urlsafe(32)
    payload = {'series_id': series.pk, 'organization_id': series.organization_id,
               'owner_id': request.user.pk, 'parsed': parsed}
    try:
        cache.set(_team_roster_import_cache_key(token), payload, timeout=TEAM_ROSTER_IMPORT_TTL)
    except Exception:
        messages.error(request, 'The roster preview is temporarily unavailable. Please try again.')
        return redirect(target_manage_url)
    old_token = request.session.get(TEAM_ROSTER_IMPORT_SESSION_KEY)
    if old_token:
        cache.delete(_team_roster_import_cache_key(old_token))
    request.session[TEAM_ROSTER_IMPORT_SESSION_KEY] = token
    request.session.modified = True
    if embedded:
        return redirect(target_manage_url)
    return redirect('training_series_roster_preview', series_id=series.pk)


@require_http_methods(['GET', 'POST'])
@never_cache
@_team_owner
def training_series_roster_preview(request, series_id):
    series = _owned_training_series(request, series_id)
    manage_url = reverse('training_series_manage', kwargs={'series_id': series.pk})
    embedded = request.GET.get('embedded') == '1' or 'setup_step' in request.POST
    target_manage_url = f'{manage_url}?step=roster' if embedded else manage_url
    token = request.session.get(TEAM_ROSTER_IMPORT_SESSION_KEY)
    try:
        payload = cache.get(_team_roster_import_cache_key(token)) if token else None
    except Exception:
        payload = None
    if (not isinstance(payload, dict) or payload.get('series_id') != series.pk
            or payload.get('organization_id') != series.organization_id
            or payload.get('owner_id') != request.user.pk):
        if token:
            try:
                cache.delete(_team_roster_import_cache_key(token))
            except Exception:
                pass
        request.session.pop(TEAM_ROSTER_IMPORT_SESSION_KEY, None)
        messages.info(request, 'Choose a roster file to preview before importing.')
        return redirect(target_manage_url)
    try:
        plan = plan_training_roster_import(series, payload['parsed'],
            organization=request.organization, user=request.user)
    except PermissionDenied:
        raise Http404
    error = ''
    if request.method == 'POST':
        if request.POST.get('action') == 'cancel':
            try:
                cache.delete(_team_roster_import_cache_key(token))
            except Exception:
                pass
            request.session.pop(TEAM_ROSTER_IMPORT_SESSION_KEY, None)
            return redirect(target_manage_url)
        if request.POST.get('action') != 'apply':
            raise Http404
        try:
            result = apply_training_roster_import(series, payload['parsed'],
                organization=request.organization, user=request.user)
        except PermissionDenied:
            raise Http404
        except ValidationError as exc:
            error = '; '.join(exc.messages)
            plan = plan_training_roster_import(series, payload['parsed'],
                organization=request.organization, user=request.user)
        else:
            try:
                cache.delete(_team_roster_import_cache_key(token))
            except Exception:
                pass
            request.session.pop(TEAM_ROSTER_IMPORT_SESSION_KEY, None)
            messages.success(request,
                f"Imported {result['created']} learner(s) across {result['teams']} team(s); "
                f"{result['skipped']} row(s) were skipped.")
            return redirect(target_manage_url)
    return render(request, 'service_domains/team_roster_import_preview.html', {
        'series': series, 'plan': plan, 'error': error,
    }, status=400 if error else 200)


@require_safe
@never_cache
@_team_owner
def training_series_roster_sample(request, series_id):
    _owned_training_series(request, series_id)
    response = HttpResponse(sample_training_roster(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="team-training-roster-sample.xlsx"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
@_team_owner
def training_session_competencies(request, pk):
    session = (AssessmentSession.objects.for_organization(request.organization)
               .for_service('team').owned_by(request.user).filter(pk=pk).first())
    if session is None:
        raise Http404
    series_link = (TrainingSeriesSession.objects.for_organization(request.organization)
                   .owned_by(request.user).filter(assessment_session=session).first())
    if series_link is None:
        raise Http404

    form = None
    certificate_error = ''
    learner_report_url = ''
    if request.method == 'POST' and request.POST.get('action') in ('release_learner', 'revoke_learner'):
        from .team_reports import release_learner_report, revoke_learner_report
        try:
            if request.POST['action'] == 'release_learner':
                token = release_learner_report(series_link, request.POST.get('student_id'),
                    organization=request.organization, user=request.user)
                learner_report_url = public_url(request, reverse('training_learner_report', args=(token,)))
            else:
                revoke_learner_report(series_link, request.POST.get('student_id'),
                    organization=request.organization, user=request.user)
                messages.success(request, 'Private result links revoked for this learner.')
        except (ValidationError, PermissionDenied) as exc:
            certificate_error = '; '.join(getattr(exc, 'messages', [])) or 'This learner is unavailable.'
    if request.method == 'POST' and request.POST.get('action') == 'issue_certificates':
        try:
            issued = issue_training_certificates(session,
                organization=request.organization, user=request.user)
        except PermissionDenied:
            raise Http404
        except ValidationError as exc:
            certificate_error = '; '.join(exc.messages)
        else:
            messages.success(request,
                f'{len(issued)} current passing certificate(s) issued or already available.')
            return redirect('training_session_competencies', pk=session.pk)
    elif request.method == 'POST' and request.POST.get('action') not in ('release_learner', 'revoke_learner'):
        form = TrainingCompetencyResultForm(request.POST,
            auto_id=f"comp_{request.POST.get('criterion_id')}_student_{request.POST.get('student_id')}_%s")
        criterion = series_link.competency_criteria.filter(
            pk=request.POST.get('criterion_id')).first()
        student = session.students.filter(pk=request.POST.get('student_id')).first()
        if criterion is None or student is None:
            raise Http404
        if form.is_valid():
            try:
                record_training_competency_result(
                    session, criterion, student, form.cleaned_data['score'],
                    form.cleaned_data['evidence'], organization=request.organization,
                    user=request.user,
                    correction_reason=form.cleaned_data['correction_reason'])
            except PermissionDenied:
                raise Http404
            except ValidationError as exc:
                form.add_error(None, '; '.join(exc.messages))
            else:
                messages.success(request, 'Competency decision saved with an auditable timestamp.')
                return redirect('training_session_competencies', pk=session.pk)

    criteria = list(series_link.competency_criteria.all())
    students = list(session.students.all())
    certificates = {certificate.result_id: certificate for certificate in
        TrainingCertificate.objects.for_organization(request.organization).owned_by(request.user)
        .filter(result__criterion__series_session=series_link)}
    current_results = {}
    for result in (TrainingCompetencyResult.objects.for_organization(request.organization)
                   .owned_by(request.user).filter(criterion__series_session=series_link)
                   .select_related('criterion', 'student', 'assessed_by')
                   .order_by('criterion_id', 'student_id', '-revision')):
        current_results.setdefault((result.criterion_id, result.student_id), result)
    rows = []
    for criterion in criteria:
        for student in students:
            result = current_results.get((criterion.pk, student.pk))
            rows.append({
                'criterion': criterion,
                'student': student,
                'result': result,
                'certificate': certificates.get(result.pk) if result else None,
                'form': (form if form is not None
                         and str(criterion.pk) == request.POST.get('criterion_id')
                         and str(student.pk) == request.POST.get('student_id')
                         else TrainingCompetencyResultForm(
                             auto_id=f'comp_{criterion.pk}_student_{student.pk}_%s')),
            })
    response = render(request, 'service_domains/team_competencies.html', {
        'session': session, 'series_link': series_link, 'series': series_link.series, 'rows': rows,
        'criteria': criteria, 'certificate_error': certificate_error,
        'learner_report_url': learner_report_url, 'learners': students,
    }, status=400 if form is not None and not form.is_valid() else 200)
    response['Cache-Control'] = 'private, no-store'
    return response


@require_safe
@never_cache
def training_certificate_verify(request, public_id):
    """Verify a shared Team certificate without exposing assessor evidence or contact data."""
    if getattr(request, 'service', None) != 'team':
        raise Http404
    certificate = (TrainingCertificate.objects.select_related('result', 'result__criterion',
        'result__student').filter(public_id=public_id).first())
    if certificate is None:
        raise Http404
    latest_id = (TrainingCompetencyResult.objects.filter(
        criterion=certificate.result.criterion, student=certificate.result.student)
        .order_by('-revision', '-pk').values_list('pk', flat=True).first())
    response = render(request, 'service_domains/team_certificate.html', {
        'certificate': certificate,
        'current': latest_id == certificate.result_id,
    })
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    return response


def _get_showcase_events_queryset(organization, user, current_time=None):
    if current_time is None:
        current_time = timezone.now()
    valid_or_submitted_assignment = (
        Q(invited_judge_assignments__active=True,
          invited_judge_assignments__conflict_declaration__isnull=True)
        & (Q(invited_judge_assignments__invitation__revoked_at__isnull=True,
             invited_judge_assignments__invitation__expires_at__gt=current_time)
           | Q(invited_judge_assignments__evaluation__isnull=False))
    )
    return (ShowcaseEvent.objects.for_organization(organization)
            .owned_by(user)
            .annotate(
                exhibit_count=Count('exhibits', distinct=True),
                rating_count=Count('ratings', distinct=True),
                average_rating=Avg('ratings__stars'),
                judge_assignment_count=Count('invited_judge_assignments',
                    filter=valid_or_submitted_assignment, distinct=True),
                judge_submitted_count=Count('invited_judge_assignments__evaluation',
                    filter=Q(invited_judge_assignments__active=True,
                        invited_judge_assignments__conflict_declaration__isnull=True),
                    distinct=True))
            .order_by('-created_at', '-pk'))


@require_http_methods(['GET', 'POST'])
@never_cache
@_showcase_owner
def showcase_event_list(request):
    """Showcase dashboard overview: metrics, active events, stage distribution, and recent events."""
    if request.method == 'POST':
        response = showcase_event_create(request)
        if response.status_code == 400:
            response.status_code = 200
        return response

    events_qs = _get_showcase_events_queryset(request.organization, request.user)
    all_events = list(events_qs)
    total_events = len(all_events)

    status_counts = {
        'total': total_events,
        'draft': sum(e.status == ShowcaseEvent.Status.DRAFT for e in all_events),
        'open': sum(e.status == ShowcaseEvent.Status.OPEN for e in all_events),
        'closed': sum(e.status == ShowcaseEvent.Status.CLOSED for e in all_events),
        'published': sum(e.status == ShowcaseEvent.Status.PUBLISHED for e in all_events),
    }

    stage_colors = {
        'draft': '#d28a08',
        'open': 'var(--success)',
        'closed': 'var(--text-muted)',
        'published': 'var(--accent)',
    }
    stage_items = [
        ('draft', 'Draft', status_counts['draft']),
        ('open', 'Open for ratings', status_counts['open']),
        ('closed', 'Closed', status_counts['closed']),
        ('published', 'Results published', status_counts['published']),
    ]
    stage_distribution = []
    pie_segments = []
    cumulative = 0
    for key, label, count in stage_items:
        seg_start = cumulative * 360 / total_events if total_events else 0
        cumulative += count
        seg_end = cumulative * 360 / total_events if total_events else 0
        percentage = round(count * 100 / total_events, 1) if total_events else 0
        stage_distribution.append({
            'key': key,
            'label': label,
            'count': count,
            'percentage': percentage,
        })
        if count:
            pie_segments.append(f'{stage_colors[key]} {seg_start:.3f}deg {seg_end:.3f}deg')
    stage_pie_background = f"conic-gradient({', '.join(pie_segments)})" if pie_segments else ''

    live_events = [e for e in all_events if e.status == ShowcaseEvent.Status.OPEN]
    recent_events = all_events[:5]

    exhibit_events = sorted(all_events, key=lambda e: (-e.exhibit_count, -e.created_at.timestamp()))[:5]
    largest_exhibits = max((e.exhibit_count for e in exhibit_events), default=0)
    for e in exhibit_events:
        e.exhibit_bar_percent = round(e.exhibit_count * 100 / largest_exhibits, 1) if largest_exhibits else 0

    return render(request, 'service_domains/showcase_events.html', {
        'events': all_events,
        'total_events': total_events,
        'open_events': status_counts['open'],
        'status_counts': status_counts,
        'total_exhibits': sum(e.exhibit_count for e in all_events),
        'total_ratings': sum(e.rating_count for e in all_events),
        'judge_submitted': sum(e.judge_submitted_count for e in all_events),
        'judge_assigned': sum(e.judge_assignment_count for e in all_events),
        'live_events': live_events,
        'recent_events': recent_events,
        'stage_distribution': stage_distribution,
        'stage_pie_background': stage_pie_background,
        'exhibit_events': exhibit_events,
        'largest_exhibits': largest_exhibits,
    })


@require_safe
@never_cache
@_showcase_owner
def showcase_event_table(request):
    """Full searchable and paginated table of owned showcase events."""
    base_qs = _get_showcase_events_queryset(request.organization, request.user)
    base_events = list(base_qs)

    status_counts = {
        'total': len(base_events),
        'draft': sum(e.status == ShowcaseEvent.Status.DRAFT for e in base_events),
        'open': sum(e.status == ShowcaseEvent.Status.OPEN for e in base_events),
        'closed': sum(e.status == ShowcaseEvent.Status.CLOSED for e in base_events),
        'published': sum(e.status == ShowcaseEvent.Status.PUBLISHED for e in base_events),
    }

    filtered_qs = base_qs
    search_query = (request.GET.get('q') or '').strip()[:100]
    if search_query:
        filtered_qs = filtered_qs.filter(name__icontains=search_query)

    status_filter = request.GET.get('status', '').strip()
    if status_filter in ShowcaseEvent.Status.values:
        filtered_qs = filtered_qs.filter(status=status_filter)
    else:
        status_filter = ''

    filtered_count = filtered_qs.count()
    paginator, page_obj = paginate(request, filtered_qs)

    query_params = QueryDict('', mutable=True)
    if search_query:
        query_params['q'] = search_query
    if status_filter:
        query_params['status'] = status_filter
    query_params['per_page'] = str(paginator.per_page)
    querystring = query_params.urlencode()

    context = {
        'events': page_obj.object_list,
        'status_counts': status_counts,
        'search_query': search_query,
        'status_filter': status_filter,
        'filtered_count': filtered_count,
        'paginator': paginator,
        'page_obj': page_obj,
        'per_page': paginator.per_page,
        'page_sizes': PAGE_SIZES,
        'querystring': querystring,
    }

    template = 'service_domains/showcase_event_table.html'
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        template = 'service_domains/_partials/showcase_event_results.html'
    response = render(request, template, context)
    patch_vary_headers(response, ['X-Requested-With'])
    return response


@require_POST
@never_cache
@_showcase_owner
def showcase_event_delete(request, event_id):
    """Delete a single owned showcase event."""
    event = (ShowcaseEvent.objects.for_organization(request.organization)
             .owned_by(request.user).filter(pk=event_id).first())
    if event is None:
        raise Http404
    name = event.name
    delete_showcase_events([event])
    messages.success(request, f'"{name}" deleted.')
    return redirect('showcase_event_table')


@require_POST
@never_cache
@_showcase_owner
def showcase_event_bulk_delete(request):
    """Delete an explicitly selected set of owned showcase events."""
    raw_ids = request.POST.getlist('event_ids')
    if (not raw_ids or len(raw_ids) > max(PAGE_SIZES)
            or any(not re.fullmatch(r'[1-9][0-9]*', value)
                   or len(value) > 19
                   or (len(value) == 19 and value > '9223372036854775807')
                   for value in raw_ids)):
        messages.error(request, 'Select one or more valid events to delete.')
        return _showcase_bulk_delete_redirect(request)

    event_ids = {int(value) for value in raw_ids}
    owned_events = (ShowcaseEvent.objects.for_organization(request.organization)
                    .owned_by(request.user))
    with transaction.atomic():
        selected = list(owned_events.select_for_update().filter(pk__in=event_ids))
        if len(selected) != len(event_ids):
            messages.error(request, 'One or more selected events are no longer available to delete.')
            return _showcase_bulk_delete_redirect(request)
        delete_showcase_events(selected)
        count = len(selected)

    messages.success(request, f'{count} event{"s" if count != 1 else ""} deleted.')
    return _showcase_bulk_delete_redirect(request)


def _showcase_bulk_delete_redirect(request):
    """Return to the filtered event list preserving parameters."""
    params = QueryDict('', mutable=True)
    query = (request.POST.get('q') or '').strip()[:100]
    status_filter = request.POST.get('status', '').strip()
    if query:
        params['q'] = query
    if status_filter in ShowcaseEvent.Status.values:
        params['status'] = status_filter
    try:
        per_page = int(request.POST.get('per_page', PAGE_SIZE))
    except (TypeError, ValueError):
        per_page = PAGE_SIZE
    if per_page not in PAGE_SIZES:
        per_page = PAGE_SIZE
    params['per_page'] = str(per_page)
    url = reverse('showcase_event_table')
    qs = params.urlencode()
    return redirect(f'{url}?{qs}' if qs else url)


@require_http_methods(['GET', 'POST'])
@never_cache
@_showcase_owner
def showcase_event_create(request):
    form = ShowcaseEventForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        event = form.save(commit=False)
        event.organization_id = request.organization['id']
        event.created_by = request.user
        try:
            event.full_clean()
        except ValidationError as exc:
            form.add_error(None, '; '.join(exc.messages))
        else:
            event.save(force_insert=True)
            messages.success(request,
                'Event details saved. Continue setup by adding exhibits, judging details, and consent checks before opening it.')
            return redirect(f"{reverse('showcase_event_manage', args=(event.pk,))}?step=exhibits")
    return render(request, 'service_domains/showcase_event_create.html', {
        'form': form,
        'stepper_steps': _SHOWCASE_CREATE_STEPS,
    }, status=400 if request.method == 'POST' and form.errors else 200)


def _load_showcase_import_preview(request, event):
    """Load an active signed exhibit import preview from session if valid."""
    raw_token = request.session.get(SHOWCASE_IMPORT_SESSION_KEY, '')
    if not raw_token or event.status != ShowcaseEvent.Status.DRAFT or len(raw_token) > 150_000:
        return None, ''
    try:
        preview = signing.loads(raw_token, salt=SHOWCASE_IMPORT_TOKEN_SALT,
                                max_age=SHOWCASE_IMPORT_TOKEN_MAX_AGE)
    except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError):
        request.session.pop(SHOWCASE_IMPORT_SESSION_KEY, None)
        return None, ''
    if (not isinstance(preview, dict) or preview.get('event') != event.pk
            or preview.get('organization') != request.organization['id']
            or preview.get('user') != request.user.pk
            or not isinstance(preview.get('parsed'), dict)):
        request.session.pop(SHOWCASE_IMPORT_SESSION_KEY, None)
        return None, ''
    plan = plan_import(event, preview['parsed'])
    return plan, (raw_token if plan['ok'] else '')


def _store_showcase_import_errors(request, event, form):
    """Keep actionable import errors for the next, step-preserving workspace GET."""
    errors = [str(error) for field_errors in form.errors.values() for error in field_errors]
    if not errors:
        errors = ['The exhibit spreadsheet could not be processed. Review it and try again.']
    request.session[SHOWCASE_IMPORT_ERRORS_SESSION_KEY] = {
        'event': event.pk,
        'organization': request.organization['id'],
        'user': request.user.pk,
        'errors': errors,
    }


def _take_showcase_import_errors(request, event):
    """Consume one-time import feedback only in the matching owner workspace."""
    payload = request.session.pop(SHOWCASE_IMPORT_ERRORS_SESSION_KEY, None)
    if not isinstance(payload, dict):
        return []
    if (payload.get('event') != event.pk
            or payload.get('organization') != request.organization['id']
            or payload.get('user') != request.user.pk):
        return []
    errors = payload.get('errors')
    return [item for item in errors if isinstance(item, str)] if isinstance(errors, list) else []


def _showcase_manage_step_redirect(event, step=None, from_param=None):
    """Return to an event's active setup step after a setup action."""
    manage_url = reverse('showcase_event_manage', kwargs={'event_id': event.pk})
    valid_steps = {'details', 'exhibits', 'criteria', 'judges-consent', 'open'}
    params = []
    if step in valid_steps:
        params.append(f'step={step}')
    if from_param:
        params.append(f'from={from_param}')
    if params:
        manage_url = f'{manage_url}?{"&".join(params)}'
    return redirect(manage_url)


_SHOWCASE_CREATE_STEPS = unsaved_steps([
    ('basics', 'Event details', 'Create a private draft'),
    ('exhibits', 'Add exhibits', 'Import your exhibit list'),
    ('criteria', 'Set judging criteria', 'Build the scorecard rubric'),
    ('judges', 'Invite and assign judges', 'Choose which exhibits each judge reviews'),
    ('consent', 'Check consent', 'Only when participants are minors'),
    ('open', 'Review and open', 'Start the event when ready'),
], hint='Save the event details to unlock this step')


def _criterion_initial(criterion):
    return {'name': criterion.name, 'description': criterion.description,
            'weight': criterion.weight, 'scale': criterion.scale,
            'max_points': criterion.max_points,
            'checklist_items_text': '\n'.join(criterion.checklist_items)}


def _showcase_readiness(event):
    """Setup checklist and readiness for a draft event's live console."""
    exhibit_count = event.exhibits.count()
    judge_coverage = showcase_judge_coverage(
        event, minimum_judges=event.minimum_judges_per_exhibit)
    active_judge_assignments = ShowcaseJudgeExhibitAssignment.objects.filter(
        event=event, active=True, invitation__revoked_at__isnull=True,
        invitation__expires_at__gt=timezone.now()).exclude(
            conflict_declaration__isnull=False).exists()
    setup_steps, event_ready = _showcase_setup_state(
        event, exhibit_count, judge_coverage,
        event.minimum_judges_per_exhibit > 0 or active_judge_assignments)
    return {'setup_steps': setup_steps, 'event_ready': event_ready,
            'exhibit_count': exhibit_count}


def _pitch_timer_payload(context):
    """Server-owned pitch clock fields shared by every live JSON response."""
    return {key: context[key] for key in (
        'pitch_seconds_remaining', 'timer_paused', 'pitch_state', 'pitch_duration',
        'pitch_ends_at', 'server_now')}


def _showcase_setup_state(event, exhibit_count, judge_coverage, scoring_required):
    rubric_total = showcase_rubric_total(event)
    rubric_complete = rubric_total == 100
    criteria_complete = rubric_complete or (not scoring_required and rubric_total == 0)
    coverage_complete = judge_coverage['uncovered_exhibit_count'] == 0
    consent_complete = not event.includes_minors or event.consent_confirmed_at is not None
    setup_steps = [
        {'key': 'details', 'label': 'Event details', 'description': 'Name, dates, and event settings',
         'complete': True},
        {'key': 'exhibits', 'label': 'Exhibits', 'description': 'Add at least one exhibit',
         'complete': exhibit_count > 0},
        {'key': 'criteria', 'label': 'Rubric',
         'description': (f'Weights total {rubric_total}%; they must reach 100%'
                         if 0 < rubric_total < 100 else
                         'Build the rubric judges will use' if scoring_required
                         else 'Judging is turned off for this event'),
         'complete': rubric_complete, 'optional': not scoring_required},
        {'key': 'judges-consent', 'label': 'Judge assignments and consent',
         'description': ('Confirm participant consent' if not consent_complete else
                         'Complete judge coverage and consent checks'),
         'complete': exhibit_count > 0 and coverage_complete and consent_complete,
         'optional': event.minimum_judges_per_exhibit == 0 and not event.includes_minors},
        {'key': 'open', 'label': 'Open Showcase',
         'description': 'Start accepting visitor ratings',
         'complete': event.status != ShowcaseEvent.Status.DRAFT},
    ]
    event_ready = bool(exhibit_count and criteria_complete and coverage_complete and consent_complete)
    return setup_steps, event_ready


def _showcase_event_can_be_deleted(event):
    return (event.status == ShowcaseEvent.Status.DRAFT
        and not ShowcaseEventAudit.objects.filter(event=event).exists()
        and not ShowcaseConsentAudit.objects.filter(event=event).exists()
        and not ShowcaseCertificate.objects.filter(event=event).exists()
        and not ShowcaseRating.objects.filter(event=event).exists()
        and not ShowcaseJudgeEvaluation.objects.filter(assignment__event=event).exists()
        and not ShowcaseJudgeConflict.objects.filter(assignment__event=event).exists())


@require_http_methods(['GET', 'POST'])
@never_cache
@_showcase_owner
def showcase_event_manage(request, event_id):
    event = _owned_showcase_event(request, event_id)
    from_param = request.GET.get('from') or request.POST.get('from') or ''
    referer = request.META.get('HTTP_REFERER', '')
    if from_param == 'live' or (not from_param and 'live-control' in referer):
        back_url = reverse('showcase_live_control', kwargs={'event_id': event.pk})
        back_label = 'Back to live control'
    elif from_param == 'table' or (not from_param and 'table' in referer):
        back_url = reverse('showcase_event_table')
        back_label = 'All events'
    elif from_param == 'monitor' or (not from_param and 'monitor' in referer):
        back_url = reverse('showcase_live_monitor', kwargs={'event_id': event.pk})
        back_label = 'Back to live monitor'
    elif event.status in (ShowcaseEvent.Status.OPEN, ShowcaseEvent.Status.CLOSED):
        back_url = reverse('showcase_live_control', kwargs={'event_id': event.pk})
        back_label = 'Back to live control'
    else:
        back_url = reverse('showcase_event_list')
        back_label = 'All showcases'

    if request.method == 'POST' and request.POST.get('action') == 'delete_event':
        with transaction.atomic():
            event = (ShowcaseEvent.objects.for_organization(request.organization)
                .owned_by(request.user).select_for_update().filter(pk=event.pk).first())
            if event is None:
                raise Http404
            has_history = (
                ShowcaseEventAudit.objects.filter(event=event).exists()
                or ShowcaseConsentAudit.objects.filter(event=event).exists()
                or ShowcaseCertificate.objects.filter(event=event).exists()
                or ShowcaseRating.objects.filter(event=event).exists()
                or ShowcaseJudgeEvaluation.objects.filter(assignment__event=event).exists()
                or ShowcaseJudgeConflict.objects.filter(assignment__event=event).exists()
            )
            if request.POST.get('confirm_delete') != 'yes':
                messages.error(request, 'Confirm deletion by checking the box before continuing.')
            elif event.status != ShowcaseEvent.Status.DRAFT:
                messages.error(request, 'Only draft events can be deleted.')
            elif has_history:
                messages.error(request,
                    'This draft has recorded activity or an audit record, so it cannot be deleted. Keep it for its event history.')
            else:
                event_name = event.name
                event.delete()
                messages.success(request, f'“{event_name}” was deleted.')
                return redirect('showcase_event_list')
        return _showcase_manage_step_redirect(event,
            request.POST.get('setup_step') or 'details', from_param=from_param)
    if request.method == 'POST' and request.POST.get('action') == 'add_exhibit':
        if event.status != ShowcaseEvent.Status.DRAFT:
            raise Http404
        exhibit_name = request.POST.get('name', '').strip()
        category_name = request.POST.get('category', '').strip()
        description = request.POST.get('description', '').strip()
        if not exhibit_name:
            messages.error(request, 'Enter an exhibit name.')
        else:
            category = None
            if category_name:
                category, _ = ShowcaseCategory.objects.get_or_create(
                    event=event, name=category_name,
                    defaults={'organization_id': event.organization_id, 'created_by': request.user}
                )
            order = event.exhibits.count() + 1
            ShowcaseExhibit.objects.create(
                event=event, name=exhibit_name, category=category,
                description=description, order=order,
                organization_id=event.organization_id, created_by=request.user
            )
            messages.success(request, f'Exhibit “{exhibit_name}” added.')
        return _showcase_manage_step_redirect(event, 'exhibits', from_param=from_param)
    if request.method == 'POST' and request.POST.get('action') == 'remove_exhibit':
        if event.status != ShowcaseEvent.Status.DRAFT:
            raise Http404
        exhibit = event.exhibits.filter(pk=request.POST.get('exhibit_id')).first()
        if exhibit is None:
            raise Http404
        exhibit.delete()
        messages.success(request, 'Exhibit removed.')
        return _showcase_manage_step_redirect(event,
            request.POST.get('setup_step') or 'exhibits', from_param=from_param)
    if request.method == 'POST' and request.POST.get('action') == 'remove_criterion':
        if event.status != ShowcaseEvent.Status.DRAFT:
            raise Http404
        criterion = event.judge_criteria.filter(pk=request.POST.get('criterion_id')).first()
        if criterion is None:
            raise Http404
        criterion.delete()
        messages.success(request, 'Judging criterion removed.')
        return _showcase_manage_step_redirect(event,
            request.POST.get('setup_step') or 'criteria', from_param=from_param)
    event_form = None
    invite_form = None
    criterion_form = None
    editing_criterion = None
    consent_form = None
    manual_judge_url = ''
    judge_invitation_notice = ''
    if request.method == 'POST' and request.POST.get('action') == 'confirm_consent':
        consent_form = ShowcaseMinorConsentForm(request.POST, auto_id='consent_%s')
        if event.status != ShowcaseEvent.Status.DRAFT or not event.includes_minors:
            raise Http404
        if consent_form.is_valid():
            try:
                confirm_showcase_minor_consent(event,
                    consent_form.cleaned_data['reason'],
                    organization=request.organization, user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                consent_form.add_error(None, '; '.join(getattr(exc, 'messages', []))
                    or 'Consent confirmation could not be recorded.')
            else:
                messages.success(request, 'Minor participant consent confirmed.')
                return _showcase_manage_step_redirect(event,
                    request.POST.get('setup_step') or 'judges-consent', from_param=from_param)
    elif request.method == 'POST' and request.POST.get('action') == 'edit':
        if event.status not in (ShowcaseEvent.Status.DRAFT, ShowcaseEvent.Status.OPEN,
                ShowcaseEvent.Status.CLOSED):
            raise Http404
        event_form = ShowcaseEventForm(request.POST, instance=event)
        if event_form.is_valid():
            updated = event_form.save(commit=False)
            if not updated.includes_minors:
                updated.consent_confirmed_at = None
            updated.save(update_fields=('name', 'starts_at', 'ends_at',
                'includes_minors', 'minimum_judges_per_exhibit', 'branding',
                'consent_confirmed_at', 'updated_at'))
            if request.POST.get('advance') == '1':
                messages.success(request, 'Event details updated.')
                return _showcase_manage_step_redirect(event, 'exhibits', from_param=from_param)
            return_step = request.POST.get('return_step')
            if return_step in {'exhibits', 'criteria', 'judges-consent', 'open'}:
                messages.success(request, 'Event details updated.')
                return _showcase_manage_step_redirect(event, return_step, from_param=from_param)
            leave_to = request.POST.get('leave_to', '')
            if leave_to and url_has_allowed_host_and_scheme(
                    leave_to, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                messages.success(request, 'Event details updated.')
                return redirect(leave_to)
            redirect_url = f"{reverse('showcase_event_manage', kwargs={'event_id': event.pk})}?step=details&saved=1"
            if from_param:
                redirect_url += f"&from={from_param}"
            return redirect(redirect_url)
    elif request.method == 'POST' and request.POST.get('action') == 'save_criterion':
        if event.status != ShowcaseEvent.Status.DRAFT:
            raise Http404
        criterion_form = ShowcaseJudgeCriterionForm(request.POST, auto_id='criterion_%s')
        criterion_id = request.POST.get('criterion_id', '')
        editing_criterion = (event.judge_criteria.filter(pk=criterion_id).first()
                             if criterion_id.isdigit() else None)
        if criterion_id and editing_criterion is None:
            raise Http404
        if criterion_form.is_valid():
            try:
                save_showcase_judge_criterion(event, criterion_form.criterion_data(),
                    criterion_id=editing_criterion.pk if editing_criterion else None,
                    organization=request.organization, user=request.user)
            except PermissionDenied:
                raise Http404
            except ValidationError as exc:
                criterion_form.add_error(None, '; '.join(exc.messages))
            else:
                messages.success(request, 'Judging criterion {}.'.format(
                    'updated' if editing_criterion else 'added'))
                return _showcase_manage_step_redirect(event, 'criteria', from_param=from_param)
    elif request.method == 'POST' and request.POST.get('action') == 'invite_judge':
        invite_form = ShowcaseJudgeInvitationForm(request.POST, event=event)
        if event.status not in (ShowcaseEvent.Status.DRAFT, ShowcaseEvent.Status.OPEN,
                ShowcaseEvent.Status.CLOSED):
            raise Http404
        if invite_form.is_valid():
            try:
                with transaction.atomic():
                    invitation, raw_token = issue_showcase_judge_invitation(event,
                        invite_form.cleaned_data['email'], invite_form.cleaned_data['expires_at'],
                        organization=request.organization, user=request.user)
                    now = timezone.now()
                    ShowcaseJudgeExhibitAssignment.objects.filter(
                        event=event, active=True
                    ).filter(
                        Q(invitation__revoked_at__isnull=False) | Q(invitation__expires_at__lte=now)
                    ).update(active=False)
                    assignments = {}
                    for row in ShowcaseJudgeExhibitAssignment.objects.filter(
                            event=event, active=True,
                            invitation__revoked_at__isnull=True,
                            invitation__expires_at__gt=now).order_by('invitation_id', 'exhibit_id'):
                        assignments.setdefault(row.invitation_id, []).append(row.exhibit_id)
                    assignments[invitation.pk] = [exhibit.pk
                        for exhibit in invite_form.cleaned_data['exhibits']]
                    replace_showcase_invited_judge_assignments(event, assignments,
                        organization=request.organization, user=request.user)
            except PermissionDenied:
                raise Http404
            except ValidationError as exc:
                invite_form.add_error(None, '; '.join(exc.messages))
            else:
                judge_url = request.build_absolute_uri(reverse(
                    'showcase_judge_workspace', kwargs={'token': raw_token}))
                _sender_name, sender_address = parseaddr(settings.DEFAULT_FROM_EMAIL)
                showcase_from_email = formataddr((
                    'LiveGrade Showcase', sender_address or settings.DEFAULT_FROM_EMAIL))
                assigned_exhibits = list(invite_form.cleaned_data['exhibits'])
                email_context = {
                    'event': event,
                    'invitation_url': judge_url,
                    'assignment_count': len(assigned_exhibits),
                    'expires_at': timezone.localtime(invitation.expires_at),
                }
                try:
                    sent = send_mail(
                        subject=f'Judge invitation: {event.name}',
                        message=render_to_string(
                            'emails/showcase_judge_invitation.txt', email_context),
                        from_email=showcase_from_email,
                        recipient_list=[invitation.email],
                        fail_silently=False,
                        html_message=render_to_string(
                            'emails/showcase_judge_invitation.html', email_context),
                    )
                except Exception:
                    sent = 0
                if sent:
                    messages.success(request, 'Judge invitation emailed. The private link is not shown again.')
                    return _showcase_manage_step_redirect(event,
                        request.POST.get('setup_step') or 'judges-consent', from_param=from_param)
                judge_invitation_notice = 'Email delivery failed. Copy this private link now; it will not be shown again.'
                manual_judge_url = judge_url
    elif request.method == 'POST' and request.POST.get('action') == 'revoke_judge':
        try:
            revoke_showcase_judge_invitation(event, request.POST.get('invitation_id'),
                organization=request.organization, user=request.user)
        except PermissionDenied:
            raise Http404
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
        else:
            messages.success(request, 'Judge invitation revoked.')
        return _showcase_manage_step_redirect(event,
            request.POST.get('setup_step') or 'judges-consent', from_param=from_param)
    if event_form is None:
        event_form = ShowcaseEventForm(instance=event)

    exhibits = event.exhibits.select_related('category').all()
    exhibit_count = exhibits.count()
    active_judge_assignments = ShowcaseJudgeExhibitAssignment.objects.filter(
        event=event, active=True, invitation__revoked_at__isnull=True,
        invitation__expires_at__gt=timezone.now()).exclude(
            conflict_declaration__isnull=False).exists()
    judge_coverage = showcase_judge_coverage(
        event, minimum_judges=event.minimum_judges_per_exhibit)
    scoring_required = event.minimum_judges_per_exhibit > 0 or active_judge_assignments
    setup_steps, event_ready = _showcase_setup_state(
        event, exhibit_count, judge_coverage, scoring_required)
    event_can_be_deleted = _showcase_event_can_be_deleted(event)
    transition_choices = _showcase_transition_choices(event)
    if event.status in (ShowcaseEvent.Status.DRAFT, ShowcaseEvent.Status.OPEN,
            ShowcaseEvent.Status.CLOSED) and invite_form is None:
        invite_form = ShowcaseJudgeInvitationForm(event=event)
    criteria = list(event.judge_criteria.all())
    if criterion_form is None:
        edit_id = request.GET.get('edit_criterion', '')
        editing_criterion = next(
            (item for item in criteria if edit_id.isdigit() and item.pk == int(edit_id)), None
        ) if event.status == ShowcaseEvent.Status.DRAFT else None
        criterion_form = ShowcaseJudgeCriterionForm(auto_id='criterion_%s', initial=(
            _criterion_initial(editing_criterion) if editing_criterion else None))
    criteria_total = sum((item.weight for item in criteria), Decimal('0'))
    judge_invitations = list(ShowcaseJudgeInvitation.objects.for_organization(
        request.organization).owned_by(request.user).filter(event=event)
        .order_by('-created_at', 'pk'))
    invitation_ids = [invitation.pk for invitation in judge_invitations]
    assignments_by_invitation = {invitation_id: [] for invitation_id in invitation_ids}
    assignments = (ShowcaseJudgeExhibitAssignment.objects.filter(
        event=event, invitation_id__in=invitation_ids, active=True)
        .select_related('exhibit').order_by('exhibit__order', 'pk'))
    for assignment in assignments:
        assignments_by_invitation[assignment.invitation_id].append(assignment)
    submitted_by_invitation = dict(ShowcaseJudgeEvaluation.objects.filter(
        assignment__invitation_id__in=invitation_ids, assignment__active=True)
        .values('assignment__invitation_id').annotate(submitted_count=Count('pk'))
        .values_list('assignment__invitation_id', 'submitted_count'))
    for invitation in judge_invitations:
        invitation.assigned_exhibits = assignments_by_invitation[invitation.pk]
        invitation.submitted_count = submitted_by_invitation.get(invitation.pk, 0)
    judge_progress = showcase_judge_progress(event,
        organization=request.organization, user=request.user)
    judge_conflicts = (ShowcaseJudgeConflict.objects.for_organization(request.organization)
        .owned_by(request.user).filter(assignment__event=event)
        .select_related('assignment__invitation', 'assignment__exhibit')
        .order_by('declared_at', 'pk'))
    if (event.status == ShowcaseEvent.Status.DRAFT and event.includes_minors
            and event.consent_confirmed_at is None and consent_form is None):
        consent_form = ShowcaseMinorConsentForm(auto_id='consent_%s')
    exhibit_import_plan, exhibit_import_token = _load_showcase_import_preview(request, event)
    exhibit_import_errors = _take_showcase_import_errors(request, event)
    rubric_import_plan, rubric_import_token = _load_showcase_rubric_preview(request, event)
    rubric_import_errors = _take_showcase_rubric_import_errors(request, event)
    valid_steps = {step['key'] for step in setup_steps}
    requested_step = request.POST.get('setup_step') or request.GET.get('step')
    if requested_step in valid_steps:
        active_step = requested_step
    elif request.method == 'POST':
        action = request.POST.get('action')
        active_step = {
            'edit': 'details',
            'save_criterion': 'criteria',
            'invite_judge': 'judges-consent',
            'revoke_judge': 'judges-consent',
            'confirm_consent': 'judges-consent',
        }.get(action, 'details')
    elif exhibit_import_plan is not None:
        active_step = 'exhibits'
    elif (rubric_import_plan is not None or rubric_import_errors
            or request.GET.get('edit_criterion')):
        active_step = 'criteria'
    else:
        active_step = 'details'
    response = render(request, 'service_domains/showcase_event_manage.html', {
        'event': event, 'exhibits': exhibits, 'event_form': event_form,
        'back_url': back_url,
        'back_label': back_label,
        'from_param': from_param,
        'invite_form': invite_form,
        'criteria': criteria,
        'criteria_total': criteria_total,
        'criteria_remaining': Decimal('100') - criteria_total,
        'criterion_form': criterion_form,
        'editing_criterion': editing_criterion,
        'judge_invitations': judge_invitations,
        'judge_invitation_notice': judge_invitation_notice,
        'manual_judge_url': manual_judge_url,
        'judge_progress': judge_progress,
        'judge_conflicts': judge_conflicts,
        'consent_form': consent_form,
        'consent_audit_events': ShowcaseConsentAudit.objects.for_organization(
            request.organization).owned_by(request.user).filter(event=event),
        'judge_coverage': judge_coverage,
        'setup_steps': setup_steps,
        'stepper_steps': steps_from_setup(setup_steps, active_step),
        'active_step': active_step,
        'exhibit_import_form': ShowcaseImportForm(),
        'exhibit_import_errors': exhibit_import_errors,
        'exhibit_import_plan': exhibit_import_plan,
        'exhibit_import_token': exhibit_import_token,
        'rubric_import_form': ShowcaseRubricImportForm(),
        'rubric_import_errors': rubric_import_errors,
        'rubric_import_plan': rubric_import_plan,
        'rubric_import_token': rubric_import_token,
        'event_ready': event_ready,
        'event_can_be_deleted': event_can_be_deleted,
        'transition_choices': transition_choices,
        'transition_form': ShowcaseEventTransitionForm(choices=transition_choices,
            auto_id='transition_%s'),
    }, status=400 if ((consent_form is not None and consent_form.errors)
        or (invite_form is not None and invite_form.errors)
        or (criterion_form is not None and criterion_form.is_bound
            and criterion_form.errors)) else 200)
    if manual_judge_url:
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'same-origin'
    return response


@require_http_methods(['GET'])
@never_cache
@_showcase_owner
def showcase_live_monitor(request, event_id):
    """Redirect to the live command console."""
    event = _owned_showcase_event(request, event_id)
    if event.status not in (ShowcaseEvent.Status.OPEN, ShowcaseEvent.Status.CLOSED):
        raise Http404
    response = redirect('showcase_live_control', event_id=event.pk)
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET'])
@never_cache
@_showcase_owner
def showcase_live_control(request, event_id):
    """Render the organizer's live command console for an open/closed showcase event."""
    event = _owned_showcase_event(request, event_id)
    if event.status not in (ShowcaseEvent.Status.DRAFT, ShowcaseEvent.Status.OPEN,
                            ShowcaseEvent.Status.CLOSED):
        raise Http404
    context = get_showcase_live_context(event)
    if event.status == ShowcaseEvent.Status.DRAFT:
        context.update(_showcase_readiness(event))
    response = render(request, 'service_domains/showcase_live_control.html', context)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    return response


@require_http_methods(['GET'])
@never_cache
@_showcase_owner
def showcase_live_state(request, event_id):
    """Real-time JSON state endpoint returning telemetry metrics and HTML fragments."""
    event = _owned_showcase_event(request, event_id)
    if event.status not in (ShowcaseEvent.Status.OPEN, ShowcaseEvent.Status.CLOSED):
        return JsonResponse({'status': event.status, 'redirect': reverse('showcase_event_manage', args=(event.pk,))})
    context = get_showcase_live_context(event)
    return JsonResponse({
        'ok': True,
        'status': event.status,
        'visitor_voting_paused': context['visitor_voting_paused'],
        'judge_scoring_paused': context['judge_scoring_paused'],
        'total_ratings': context['total_ratings'],
        'avg_stars_all': context['avg_stars_all'],
        'total_assigned_scorecards': context['total_assigned_scorecards'],
        'total_submitted_scorecards': context['total_submitted_scorecards'],
        'attention_count': context['attention_count'],
        **_pitch_timer_payload(context),
        'hero_html': render_to_string('service_domains/_partials/showcase_live_hero.html', context, request=request),
        'stage_html': render_to_string('service_domains/_partials/showcase_live_stage.html', context, request=request),
        'table_html': render_to_string('service_domains/_partials/showcase_live_table.html', context, request=request),
        'projector_html': render_to_string('service_domains/_partials/showcase_live_projector_control.html', context, request=request),
    })


@require_POST
@never_cache
@_showcase_owner
def showcase_live_action(request, event_id):
    """Execute atomic live floor and stage management actions."""
    event = _owned_showcase_event(request, event_id)
    action = request.POST.get('action')
    opening = (action == 'open_event' and event.status == ShowcaseEvent.Status.DRAFT)
    if not opening and event.status not in (ShowcaseEvent.Status.OPEN, ShowcaseEvent.Status.CLOSED):
        return JsonResponse({'error': 'Event is not active.'}, status=400)
    try:
        if action == 'toggle_visitor_voting':
            toggle_showcase_floor_visitor_voting(event, organization=request.organization, user=request.user)
        elif action == 'toggle_judge_scoring':
            toggle_showcase_floor_judge_scoring(event, organization=request.organization, user=request.user)
        elif action == 'assign_roving_judge':
            exhibit_id = request.POST.get('exhibit_id')
            judge_inv_id = request.POST.get('judge_invitation_id')
            assign_showcase_roving_judge(event, exhibit_id, judge_inv_id, organization=request.organization, user=request.user)
        elif action == 'stage_set_exhibit':
            exhibit_id = request.POST.get('exhibit_id')
            stage_set_showcase_exhibit(event, exhibit_id, organization=request.organization, user=request.user)
        elif action == 'stage_adjust_timer':
            delta = int(request.POST.get('delta', 30))
            stage_adjust_showcase_timer(event, delta, organization=request.organization, user=request.user)
        elif action == 'stage_toggle_pause':
            stage_toggle_showcase_pause(event, organization=request.organization, user=request.user)
        elif action == 'stage_clear':
            stage_clear_showcase_exhibit(event, organization=request.organization, user=request.user)
        elif action == 'set_projector_mode':
            mode = request.POST.get('mode')
            set_showcase_projector_mode(event, mode, organization=request.organization, user=request.user)
        elif action == 'open_event':
            transition_showcase_event(
                event, ShowcaseEvent.Status.OPEN,
                organization=request.organization, user=request.user,
                reason='Organizer opened the showcase from the live console.')
            broadcast_showcase_event(event, 'showcase.opened', {})
            messages.success(request, 'The showcase is open. Visitors can now rate exhibits.')
            return JsonResponse({'status': 'open', 'redirect': reverse('showcase_live_control', args=(event.pk,))})
        elif action == 'close_ratings':
            transition_showcase_event(
                event, ShowcaseEvent.Status.CLOSED,
                organization=request.organization, user=request.user,
                reason='Organizer closed public visitor rating window from live command console.'
            )
            broadcast_showcase_event(event, 'showcase.closed', {})
            messages.success(request, 'Visitor rating window closed. Finalize judge scorecards and review results.')
            return JsonResponse({'status': 'closed', 'redirect': reverse('showcase_event_results', args=(event.pk,))})
        else:
            return JsonResponse({'error': f'Unknown action: {action}'}, status=400)
    except (ValidationError, PermissionDenied) as exc:
        msg = '; '.join(getattr(exc, 'messages', [])) or str(exc)
        return JsonResponse({'error': msg}, status=400)

    event.refresh_from_db()
    context = get_showcase_live_context(event)
    broadcast_showcase_event(event, f'showcase.{action}', {'action': action})
    return JsonResponse({
        'ok': True,
        'status': event.status,
        'visitor_voting_paused': context['visitor_voting_paused'],
        'judge_scoring_paused': context['judge_scoring_paused'],
        'total_ratings': context['total_ratings'],
        'avg_stars_all': context['avg_stars_all'],
        'total_assigned_scorecards': context['total_assigned_scorecards'],
        'total_submitted_scorecards': context['total_submitted_scorecards'],
        'attention_count': context['attention_count'],
        **_pitch_timer_payload(context),
        'hero_html': render_to_string('service_domains/_partials/showcase_live_hero.html', context, request=request),
        'stage_html': render_to_string('service_domains/_partials/showcase_live_stage.html', context, request=request),
        'table_html': render_to_string('service_domains/_partials/showcase_live_table.html', context, request=request),
        'projector_html': render_to_string('service_domains/_partials/showcase_live_projector_control.html', context, request=request),
    })


@require_http_methods(['GET'])
@never_cache
@_showcase_owner
def showcase_live_activity_view(request, event_id):
    """Return an owner-scoped refresh projection with judging channels separate."""
    event = _owned_showcase_event(request, event_id)
    if event.status not in (ShowcaseEvent.Status.OPEN, ShowcaseEvent.Status.CLOSED):
        raise Http404
    response = JsonResponse(showcase_live_activity(event))
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
@_showcase_owner
def showcase_event_results(request, event_id):
    """Render the organizer's dedicated Results and Awards review screen."""
    event = _owned_showcase_event(request, event_id)
    if event.status == ShowcaseEvent.Status.DRAFT:
        raise Http404

    if request.method == 'POST' and request.POST.get('action') == 'publish_results':
        try:
            transition_showcase_event(
                event, ShowcaseEvent.Status.PUBLISHED,
                organization=request.organization, user=request.user,
                reason='Organizer published official showcase results.'
            )
            broadcast_showcase_event(event, 'showcase.published', {})
            messages.success(request, 'Results and awards published successfully.')
            return redirect('showcase_event_results', event_id=event.pk)
        except ValidationError as exc:
            messages.error(request, '; '.join(getattr(exc, 'messages', [str(exc)])))

    if request.method == 'POST' and request.POST.get('action') == 'revoke_results':
        try:
            transition_showcase_event(
                event, ShowcaseEvent.Status.CLOSED,
                organization=request.organization, user=request.user,
                reason='Organizer returned showcase results to review.')
            broadcast_showcase_event(event, 'showcase.closed', {})
            messages.success(request, 'Results returned to review.')
            return redirect('showcase_event_results', event_id=event.pk)
        except ValidationError as exc:
            messages.error(request, '; '.join(getattr(exc, 'messages', [str(exc)])))

    awards = showcase_awards(event, allow_preview=True)
    certificates = {
        (row.exhibit_id, row.award_type): row
        for row in ShowcaseCertificate.objects.filter(event=event)
    }
    categories = []
    for award in awards['categories']:
        winner = award['winner']
        certificate = (certificates.get((winner.pk, ShowcaseCertificate.AwardType.CATEGORY))
                       if winner else None)
        categories.append({
            'category': award['category'],
            'winner': winner.name if winner else None,
            'certificate_url': (reverse('showcase_certificate_verify',
                args=(certificate.public_id,)) if certificate else None),
            'tied': [exhibit.name for exhibit in award['tied']],
            'tied_details': [
                {'exhibit': item['exhibit'].name, 'mean': str(item['mean']),
                 'judge_count': item['judge_count']}
                for item in award['tied_details']
            ],
            'mean': str(award['mean']) if award['mean'] is not None else None,
            'judge_count': award['judge_count'],
        })
    favorite = awards['audience_favorite']
    favorite_certificate = (certificates.get((favorite.pk, ShowcaseCertificate.AwardType.AUDIENCE))
                            if favorite else None)
    results = {
        'categories': categories,
        'audience_favorite': favorite.name if favorite else None,
        'audience_favorite_certificate_url': (reverse('showcase_certificate_verify',
            args=(favorite_certificate.public_id,)) if favorite_certificate else None),
        'audience_favorite_tied': [exhibit.name for exhibit in awards['audience_favorite_tied']],
        'audience_favorite_tied_details': [
            {'exhibit': item['exhibit'].name, 'mean': str(item['mean']),
             'rating_count': item['rating_count']}
            for item in awards['audience_favorite_tied_details']
        ],
        'audience_favorite_mean': (
            str(awards['audience_favorite_mean'])
            if awards['audience_favorite_mean'] is not None else None),
        'audience_favorite_rating_count': awards['audience_favorite_rating_count'],
    }
    live = get_showcase_live_context(event)
    winners = {name for name in [item['winner'] for item in categories] + [results['audience_favorite']] if name}
    ranked = sorted(live['rows'], key=lambda row: (
        -(row['judge_mean'] if row['judge_mean'] is not None else -1),
        -(row['audience_mean'] if row['audience_mean'] is not None else -1),
        row['exhibit'].order, row['exhibit'].pk))
    standings = [{'rank': position, 'row': row, 'is_winner': row['exhibit'].name in winners}
                 for position, row in enumerate(ranked, start=1)]
    decided = sum(1 for item in categories if item['winner']) + (1 if results['audience_favorite'] else 0)
    context = {
        'event': event,
        'results': results,
        'standings': standings,
        'exhibit_count': len(live['rows']),
        'total_ratings': live['total_ratings'],
        'avg_stars_all': live['avg_stars_all'],
        'submitted_scorecards': live['total_submitted_scorecards'],
        'assigned_scorecards': live['total_assigned_scorecards'],
        'scorecard_percent': (round(100 * live['total_submitted_scorecards'] / live['total_assigned_scorecards'])
                              if live['total_assigned_scorecards'] else 0),
        'awards_decided': decided,
        'awards_total': len(categories) + 1,
        'public_results_url': reverse('showcase_public_results', args=(event.public_id,)),
        'is_published': event.status == ShowcaseEvent.Status.PUBLISHED and event.public_results,
    }
    template = ('service_domains/_partials/showcase_results_body.html'
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                else 'service_domains/showcase_event_results.html')
    response = render(request, template, context)
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET'])
@never_cache
def showcase_projector_screen(request, public_id):
    """Fullscreen venue presentation display supporting QR, ticker, stage pitch, and awards modes."""
    event = ShowcaseEvent.objects.filter(public_id=public_id).first()
    if event is None:
        raise Http404

    context = get_showcase_live_context(event)
    context['venue_qr_url'] = reverse('showcase_visit_qr', args=(event.public_id,))
    context['stage_qr_url'] = (reverse('showcase_exhibit_qr', args=(context['active_exhibit'].public_id,))
                               if context.get('active_exhibit') else '')

    response = render(request, 'service_domains/showcase_projector.html', context)
    response['Cache-Control'] = 'private, no-store'
    return response


def _showcase_transition_choices(event):
    choices = {
        ShowcaseEvent.Status.DRAFT: [(ShowcaseEvent.Status.OPEN, 'Open rating window')],
        ShowcaseEvent.Status.OPEN: [(ShowcaseEvent.Status.CLOSED, 'Close rating window')],
        ShowcaseEvent.Status.CLOSED: [(ShowcaseEvent.Status.PUBLISHED, 'Publish results')],
        ShowcaseEvent.Status.PUBLISHED: [(ShowcaseEvent.Status.CLOSED, 'Revoke public results')],
    }
    return choices.get(event.status, [])


@require_http_methods(['GET', 'POST'])
@never_cache
def showcase_judge_workspace(request, token):
    """Private-link scorecards for the exhibits assigned to one invited judge."""
    if getattr(request, 'service', None) != 'showcase':
        raise Http404
    invitation = resolve_showcase_judge_invitation(token)
    if invitation is None:
        allowed = allow_unknown_bearer_request(request, service='showcase-judge')
        if allowed is None:
            return HttpResponse('The judge service is temporarily unavailable.', status=503)
        if not allowed:
            response = HttpResponse('Too many requests for judge links.', status=429)
            response['Retry-After'] = str(settings.PUBLIC_BEARER_UNKNOWN_RATE_WINDOW)
            return response
        raise Http404
    allowed = allow_showcase_judge_request(invitation)
    if allowed is None:
        return HttpResponse('The judge service is temporarily unavailable.', status=503)
    if not allowed:
        response = HttpResponse('Too many requests for this judge link.', status=429)
        response['Retry-After'] = str(settings.PANEL_EXAMINER_RATE_WINDOW)
        return response

    assignments = list(ShowcaseJudgeExhibitAssignment.objects.filter(
        event=invitation.event, invitation=invitation, active=True)
        .select_related('exhibit', 'exhibit__category').order_by('exhibit__order', 'pk'))
    criteria = list(ShowcaseJudgeCriterion.objects.filter(event=invitation.event)
        .order_by('order', 'pk'))
    evaluations = {item.assignment_id: item for item in ShowcaseJudgeEvaluation.objects.filter(
        assignment__in=assignments)}
    conflicts = {item.assignment_id: item for item in ShowcaseJudgeConflict.objects.filter(
        assignment__in=assignments)}
    error = ''
    bound_assignment = None
    form = None
    conflict_form = None
    if request.method == 'POST':
        public_id = request.POST.get('exhibit_id', '')
        bound_assignment = next((item for item in assignments
            if str(item.exhibit.public_id) == public_id), None)
        if bound_assignment is None:
            raise Http404
        if request.POST.get('action') == 'declare_conflict':
            if bound_assignment.pk in evaluations:
                error = 'A conflict must be declared before submitting this exhibit scorecard.'
            elif bound_assignment.pk in conflicts:
                error = 'A conflict has already been declared for this exhibit.'
            else:
                conflict_form = ShowcaseJudgeConflictForm(request.POST,
                    auto_id=f'conflict_{bound_assignment.pk}_%s')
                if conflict_form.is_valid():
                    try:
                        declare_showcase_judge_conflict(token,
                            bound_assignment.exhibit.public_id,
                            conflict_form.cleaned_data['reason'])
                    except PermissionDenied:
                        raise Http404
                    except ValidationError as exc:
                        conflict_form.add_error(None, '; '.join(exc.messages))
                    else:
                        broadcast_showcase_event(invitation.event, 'showcase.conflict', {})
                        return redirect('showcase_judge_workspace', token=token)
        elif bound_assignment.pk in evaluations:
            error = 'This exhibit scorecard was already submitted and is immutable.'
        elif bound_assignment.pk in conflicts:
            error = 'This exhibit is marked as a conflict and cannot be scored.'
        elif (invitation.event.workflow_snapshot or {}).get('floor_state', {}).get('judge_scoring_paused', False):
            error = 'Judge scoring is temporarily locked by the organizer.'
        elif invitation.event.status not in (ShowcaseEvent.Status.OPEN, ShowcaseEvent.Status.CLOSED):
            error = 'Scoring becomes available when the Showcase event opens.'
        elif not criteria:
            error = 'The organizer has not configured judge criteria for this event.'
        elif (invitation.event.status == ShowcaseEvent.Status.OPEN
                and not showcase_exhibit_on_stage(invitation.event, bound_assignment.exhibit_id)):
            error = SHOWCASE_NOT_ON_STAGE
        else:
            form = ShowcaseJudgeScorecardForm(request.POST, criteria=criteria,
                prefix=f'exhibit_{bound_assignment.pk}')
            if form.is_valid():
                try:
                    submit_showcase_judge_evaluation(token, bound_assignment.exhibit.public_id,
                        form.score_payload())
                except PermissionDenied:
                    raise Http404
                except ValidationError as exc:
                    form.add_error(None, '; '.join(exc.messages))
                else:
                    broadcast_showcase_event(invitation.event, 'showcase.scorecard', {})
                    return redirect('showcase_judge_workspace', token=token)

    cards = []
    for assignment in assignments:
        evaluation = evaluations.get(assignment.pk)
        score_form = None
        declaration_form = None
        if (evaluation is None and criteria and invitation.event.status in
                (ShowcaseEvent.Status.DRAFT, ShowcaseEvent.Status.OPEN,
                 ShowcaseEvent.Status.CLOSED)):
            score_form = (form if assignment == bound_assignment and form is not None
                else ShowcaseJudgeScorecardForm(criteria=criteria,
                    prefix=f'exhibit_{assignment.pk}'))
        if evaluation is None and assignment.pk not in conflicts:
            declaration_form = (conflict_form if assignment == bound_assignment
                and conflict_form is not None else ShowcaseJudgeConflictForm(
                    auto_id=f'conflict_{assignment.pk}_%s'))
        on_stage = showcase_exhibit_on_stage(invitation.event, assignment.exhibit_id)
        cards.append({'assignment': assignment, 'evaluation': evaluation,
            'form': score_form, 'conflict': conflicts.get(assignment.pk),
            'conflict_form': declaration_form, 'on_stage': on_stage,
            'stage_locked': invitation.event.status == ShowcaseEvent.Status.OPEN and not on_stage})
    response = render(request, 'service_domains/showcase_judge_workspace.html', {
        'invitation': invitation, 'cards': cards, 'criteria': criteria,
        'error': error, 'form': form,
        'waiting_for_open': invitation.event.status == ShowcaseEvent.Status.DRAFT,
        'scoring_available': invitation.event.status in
            (ShowcaseEvent.Status.OPEN, ShowcaseEvent.Status.CLOSED) and bool(criteria),
        'submitted_count': sum(1 for card in cards if card['evaluation'] is not None),
        'conflict_count': sum(1 for card in cards if card['conflict'] is not None),
        'remaining_count': sum(1 for card in cards
            if card['evaluation'] is None and card['conflict'] is None),
    }, status=400 if error or (form is not None and not form.is_valid())
        or (conflict_form is not None and not conflict_form.is_valid()) else 200)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    return response


@require_http_methods(['GET'])
@never_cache
def showcase_judge_state(request, token):
    """Which of this judge's exhibits are on stage right now (no scores, no identities)."""
    if getattr(request, 'service', None) != 'showcase':
        raise Http404
    invitation = resolve_showcase_judge_invitation(token)
    if invitation is None:
        raise Http404
    allowed = allow_showcase_judge_request(invitation)
    if allowed is None:
        return JsonResponse({'error': 'unavailable'}, status=503)
    if not allowed:
        return JsonResponse({'error': 'rate limited'}, status=429,
                            headers={'Retry-After': str(settings.PANEL_EXAMINER_RATE_WINDOW)})
    event = invitation.event
    stage = (event.workflow_snapshot or {}).get('stage_state') or {}
    on_stage = [str(exhibit_id) for exhibit_id in ShowcaseJudgeExhibitAssignment.objects.filter(
        event=event, invitation=invitation, active=True,
        exhibit_id=stage.get('active_exhibit_id') or 0).values_list('exhibit__public_id', flat=True)]
    response = JsonResponse({'status': event.status, 'on_stage': on_stage,
        'scoring_paused': bool((event.workflow_snapshot or {}).get('floor_state', {}).get(
            'judge_scoring_paused', False))})
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['POST'])
@never_cache
@_showcase_owner
def showcase_event_transition(request, event_id):
    """Move an event to its next lifecycle state (open, close, publish or revoke)."""
    event = _owned_showcase_event(request, event_id)
    form = ShowcaseEventTransitionForm(request.POST,
        choices=_showcase_transition_choices(event), auto_id='transition_%s')
    if form.is_valid():
        try:
            transition_showcase_event(event, form.cleaned_data['target_status'],
                organization=request.organization, user=request.user,
                reason=form.cleaned_data['reason'])
        except ValidationError as exc:
            form.add_error(None, '; '.join(exc.messages))
        else:
            event.refresh_from_db()
            broadcast_showcase_event(event, f'showcase.{event.status}', {})
            if event.status in (ShowcaseEvent.Status.OPEN, ShowcaseEvent.Status.CLOSED):
                return redirect('showcase_live_control', event_id=event.pk)
            return redirect('showcase_event_results', event_id=event.pk)
    messages.error(request, ' '.join(
        str(error) for errors in form.errors.values() for error in errors))
    if event.status in (ShowcaseEvent.Status.DRAFT, ShowcaseEvent.Status.OPEN,
                        ShowcaseEvent.Status.CLOSED):
        return redirect('showcase_live_control', event_id=event.pk)
    return redirect('showcase_event_results', event_id=event.pk)


def _showcase_import_payload(request, event, **extra):
    return {'event': event.pk, 'organization': request.organization['id'],
            'user': request.user.pk, **extra}


def _owned_rubric_payload(request, event, payload):
    """True when a signed or stored rubric payload belongs to this owner and event."""
    return (isinstance(payload, dict) and payload.get('event') == event.pk
            and payload.get('organization') == request.organization['id']
            and payload.get('user') == request.user.pk)


def _load_showcase_rubric_preview(request, event):
    """Load an active signed rubric import preview from the session if valid."""
    raw_token = request.session.get(SHOWCASE_RUBRIC_IMPORT_SESSION_KEY, '')
    if not raw_token or event.status != ShowcaseEvent.Status.DRAFT or len(raw_token) > 150_000:
        return None, ''
    try:
        preview = signing.loads(raw_token, salt=SHOWCASE_RUBRIC_IMPORT_TOKEN_SALT,
                                max_age=SHOWCASE_IMPORT_TOKEN_MAX_AGE)
    except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError):
        request.session.pop(SHOWCASE_RUBRIC_IMPORT_SESSION_KEY, None)
        return None, ''
    if not _owned_rubric_payload(request, event, preview) or not isinstance(preview.get('parsed'), dict):
        request.session.pop(SHOWCASE_RUBRIC_IMPORT_SESSION_KEY, None)
        return None, ''
    plan = plan_showcase_rubric(event, preview['parsed'])
    return plan, (raw_token if plan['ok'] else '')


def _take_showcase_rubric_import_errors(request, event):
    """Consume one-time rubric import feedback for the matching owner workspace."""
    payload = request.session.pop(SHOWCASE_RUBRIC_IMPORT_ERRORS_SESSION_KEY, None)
    if not _owned_rubric_payload(request, event, payload):
        return []
    errors = payload.get('errors')
    return [item for item in errors if isinstance(item, str)] if isinstance(errors, list) else []


def _store_showcase_rubric_import_errors(request, event, errors):
    request.session[SHOWCASE_RUBRIC_IMPORT_ERRORS_SESSION_KEY] = _showcase_import_payload(
        request, event, errors=errors or ['The rubric spreadsheet could not be processed. Try again.'])


@require_POST
@never_cache
@_showcase_owner
def showcase_rubric_import(request, event_id):
    """Preview, apply or cancel a rubric spreadsheet; always returns to the Rubric step."""
    event = _owned_showcase_event(request, event_id)
    from_param = 'live' if request.POST.get('from') == 'live' else None

    def back():
        return _showcase_manage_step_redirect(event, 'criteria', from_param=from_param)

    action = request.POST.get('action')
    request.session.pop(SHOWCASE_RUBRIC_IMPORT_ERRORS_SESSION_KEY, None)
    if action == 'cancel':
        request.session.pop(SHOWCASE_RUBRIC_IMPORT_SESSION_KEY, None)
        return back()
    if action == 'apply':
        form = ShowcaseImportApplyForm(request.POST)
        errors = [str(error) for field_errors in form.errors.values() for error in field_errors]
        preview = None
        if form.is_valid() and len(form.cleaned_data['preview_token']) <= 150_000:
            try:
                preview = signing.loads(form.cleaned_data['preview_token'],
                    salt=SHOWCASE_RUBRIC_IMPORT_TOKEN_SALT, max_age=SHOWCASE_IMPORT_TOKEN_MAX_AGE)
            except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError):
                errors = ['This preview expired or is invalid. Upload the spreadsheet again.']
        if preview is not None:
            if not _owned_rubric_payload(request, event, preview) or not isinstance(preview.get('parsed'), dict):
                raise Http404
            try:
                apply_rubric_import(event, preview['parsed'],
                    organization=request.organization, user=request.user)
            except PermissionDenied:
                raise Http404
            except ValidationError as exc:
                errors = exc.messages
            else:
                request.session.pop(SHOWCASE_RUBRIC_IMPORT_SESSION_KEY, None)
                messages.success(request, 'Judge rubric imported.')
                return back()
        _store_showcase_rubric_import_errors(request, event, errors)
        return back()
    request.session.pop(SHOWCASE_RUBRIC_IMPORT_SESSION_KEY, None)
    form = ShowcaseRubricImportForm(request.POST, request.FILES)
    if not form.is_valid():
        _store_showcase_rubric_import_errors(request, event,
            [str(error) for field_errors in form.errors.values() for error in field_errors])
        return back()
    parsed = parse_showcase_rubric(form.cleaned_data['file'])
    request.session[SHOWCASE_RUBRIC_IMPORT_SESSION_KEY] = signing.dumps(
        _showcase_import_payload(request, event, parsed=parsed),
        salt=SHOWCASE_RUBRIC_IMPORT_TOKEN_SALT, compress=True)
    return back()


@require_safe
@never_cache
@_showcase_owner
def showcase_rubric_import_sample(request, event_id):
    _owned_showcase_event(request, event_id)
    response = HttpResponse(showcase_rubric_sample().getvalue(), content_type=(
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))
    response['Content-Disposition'] = 'attachment; filename="showcase-rubric-sample.xlsx"'
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
@_showcase_owner
def showcase_exhibit_import(request, event_id):
    event = _owned_showcase_event(request, event_id)
    embedded = request.GET.get('embedded') == '1'
    if request.method == 'POST' and request.POST.get('action') == 'cancel':
        request.session.pop(SHOWCASE_IMPORT_SESSION_KEY, None)
        request.session.pop(SHOWCASE_IMPORT_ERRORS_SESSION_KEY, None)
        if embedded:
            return _showcase_manage_step_redirect(event,
                request.POST.get('setup_step') or 'exhibits')
        return _showcase_manage_step_redirect(event, request.POST.get('setup_step'))
    if request.method == 'POST':
        request.session.pop(SHOWCASE_IMPORT_ERRORS_SESSION_KEY, None)
        if request.POST.get('action') == 'preview':
            request.session.pop(SHOWCASE_IMPORT_SESSION_KEY, None)
    plan = None
    parsed = None
    preview_token = ''
    status = 200
    if request.method == 'POST' and request.POST.get('action') == 'apply':
        form = ShowcaseImportApplyForm(request.POST)
        if form.is_valid():
            token = form.cleaned_data['preview_token']
            if len(token) > 150_000:
                raise Http404
            try:
                preview = signing.loads(token, salt=SHOWCASE_IMPORT_TOKEN_SALT,
                                        max_age=SHOWCASE_IMPORT_TOKEN_MAX_AGE)
            except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError):
                form.add_error(None, 'This preview expired or is invalid. Upload the spreadsheet again.')
                status = 400
            else:
                if (not isinstance(preview, dict)
                        or preview.get('event') != event.pk
                        or preview.get('organization') != request.organization['id']
                        or preview.get('user') != request.user.pk
                        or not isinstance(preview.get('parsed'), dict)):
                    raise Http404
                parsed = preview['parsed']
                plan = plan_import(event, parsed)
                preview_token = token if plan['ok'] else ''
                try:
                    apply_import(event, parsed,
                        organization=request.organization, user=request.user)
                except PermissionDenied:
                    raise Http404
                except ValidationError as exc:
                    form.add_error(None, '; '.join(exc.messages))
                    status = 400
                else:
                    request.session.pop(SHOWCASE_IMPORT_SESSION_KEY, None)
                    messages.success(request, 'Exhibits imported.')
                    if embedded:
                        return _showcase_manage_step_redirect(event,
                            request.POST.get('setup_step') or 'exhibits')
                    return _showcase_manage_step_redirect(event, request.POST.get('setup_step'))
        else:
            status = 400
        apply_errors = [str(error) for field_errors in form.errors.values() for error in field_errors]
        if embedded:
            _store_showcase_import_errors(request, event, form)
            return _showcase_manage_step_redirect(event,
                request.POST.get('setup_step') or 'exhibits')
        return render(request, 'service_domains/showcase_import.html', {
            'event': event, 'form': ShowcaseImportForm(), 'plan': plan, 'parsed': parsed,
            'preview_token': preview_token, 'apply_errors': apply_errors,
            'apply_attempt': True,
        }, status=status)

    form = ShowcaseImportForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        parsed = parse_exhibits(form.cleaned_data['file'])
        plan = plan_import(event, parsed)
        if plan['ok'] or embedded:
            preview_token = signing.dumps({
                'event': event.pk,
                'organization': request.organization['id'],
                'user': request.user.pk,
                'parsed': parsed,
            }, salt=SHOWCASE_IMPORT_TOKEN_SALT, compress=True)
            if embedded:
                request.session[SHOWCASE_IMPORT_SESSION_KEY] = preview_token
                request.session.modified = True
                return _showcase_manage_step_redirect(event,
                    request.POST.get('setup_step') or 'exhibits')
    elif request.method == 'POST':
        status = 400
        if embedded:
            _store_showcase_import_errors(request, event, form)
            return _showcase_manage_step_redirect(event,
                request.POST.get('setup_step') or 'exhibits')
    return render(request, 'service_domains/showcase_import.html', {
        'event': event, 'form': form, 'plan': plan, 'parsed': parsed,
        'preview_token': preview_token, 'apply_errors': [],
    }, status=status)


@require_safe
@never_cache
@_showcase_owner
def showcase_exhibit_import_sample(request, event_id):
    _owned_showcase_event(request, event_id)
    workbook = sample_workbook()
    response = HttpResponse(workbook.getvalue(), content_type=(
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))
    response['Content-Disposition'] = 'attachment; filename="showcase-exhibits-sample.xlsx"'
    response['Cache-Control'] = 'private, no-store'
    return response


@require_safe
@never_cache
@_showcase_owner
def showcase_event_labels(request, event_id):
    """Render one print-ready QR label per exhibit in the owner's event."""
    event = _owned_showcase_event(request, event_id)
    labels = []
    exhibits = event.exhibits.select_related('category').order_by(
        'category__order', 'order', 'pk')
    for exhibit in exhibits:
        labels.append({
            'exhibit': exhibit,
            'qr_url': reverse('showcase_exhibit_qr', args=(exhibit.public_id,)),
            'rating_url': request.build_absolute_uri(reverse(
                'showcase_rate_exhibit', args=(exhibit.public_id,))),
        })
    response = render(request, 'service_domains/showcase_event_labels.html', {
        'event': event, 'labels': labels,
    })
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


def _device_rating_digest(request, event):
    """Digest of this browser's rating token for the event, or None without a valid one."""
    token = request.COOKIES.get(_cookie_name(event), '')
    return ShowcaseRating.digest_device_key(token, event) if _valid_device_token(token, event) else None


def _rated_exhibit_ids(request, event):
    """Exhibits this device has already rated (never shown to anyone else)."""
    digest = _device_rating_digest(request, event)
    if digest is None:
        return set()
    return set(ShowcaseRating.objects.filter(event=event, device_digest=digest)
               .values_list('exhibit_id', flat=True))


def _staged_exhibit(event):
    """The exhibit the organizer has put on stage, if any."""
    exhibit_id = ((event.workflow_snapshot or {}).get('stage_state') or {}).get('active_exhibit_id')
    return (event.exhibits.select_related('category').filter(pk=exhibit_id).first()
            if exhibit_id else None)


def _rating_unavailable_message(event, exhibit=None):
    """Why visitors cannot rate right now, or '' when they can.

    Ratings follow what is live: the event must be open and, when a specific
    exhibit is asked about, that exhibit must be the one on stage. The event's
    planned dates play no part.
    """
    if event.status == ShowcaseEvent.Status.DRAFT:
        return 'This showcase has not opened for ratings yet. Please check back soon.'
    if event.status != ShowcaseEvent.Status.OPEN:
        return 'Ratings for this showcase are closed. Thank you for taking part.'
    if event.includes_minors and not event.consent_confirmed_at:
        return 'Ratings are not open yet. The organizer is finishing setup.'
    if exhibit is not None and not showcase_exhibit_on_stage(event, exhibit.pk):
        return (f'{exhibit.name} is not on stage right now. You can rate an exhibit while it is '
                'presenting.')
    return ''


def _cookie_name(event):
    return f'{DEVICE_COOKIE_PREFIX}{event.public_id.hex}'


def _valid_device_token(token, event):
    if not token:
        return False
    try:
        ShowcaseRating.digest_device_key(token, event)
    except ValidationError:
        return False
    return True


def _render_rating(request, exhibit, form, *, status=200, submitted=False, message='', unavailable='',
                   on_stage=None, already_rated=False):
    return render(request, 'service_domains/showcase_rate.html', {
        'event': exhibit.event,
        'exhibit': exhibit,
        'category': exhibit.category,
        'form': form,
        'submitted': submitted,
        'message': message,
        'unavailable': unavailable,
        'on_stage': on_stage,
        'already_rated': already_rated,
    }, status=status)


@require_safe
@never_cache
def showcase_rating_state(request, exhibit_id):
    """Can this device rate this exhibit right now? Answers only for the asking device."""
    exhibit = (ShowcaseExhibit.objects.select_related('event')
               .filter(public_id=exhibit_id).first())
    if exhibit is None:
        raise Http404
    event = exhibit.event
    if exhibit.pk in _rated_exhibit_ids(request, event):
        state = {'can_rate': False, 'reason': 'rated',
                 'message': 'You have already rated this exhibit. Thank you.'}
    elif (event.workflow_snapshot or {}).get('floor_state', {}).get('visitor_voting_paused', False):
        state = {'can_rate': False, 'reason': 'paused',
                 'message': 'Visitor ratings are temporarily paused by the organizer.'}
    else:
        message = _rating_unavailable_message(event, exhibit)
        state = {'can_rate': not message, 'reason': 'unavailable' if message else '', 'message': message}
    response = JsonResponse(state)
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
def rate_showcase_exhibit(request, exhibit_id):
    """Anonymous public rating flow; stores no visitor identity or raw token."""
    exhibit = (ShowcaseExhibit.objects.select_related('event', 'category')
               .filter(public_id=exhibit_id).first())
    if exhibit is None:
        raise Http404
    event = exhibit.event
    snapshot = event.workflow_snapshot or {}
    if snapshot.get('floor_state', {}).get('visitor_voting_paused', False):
        response = _render_rating(request, exhibit, ShowcaseRatingForm(),
            message='Visitor ratings are temporarily paused by the organizer. Please check back shortly.')
        response['Cache-Control'] = 'private, no-store'
        return response
    unavailable = _rating_unavailable_message(event, exhibit)
    if unavailable:
        response = _render_rating(request, exhibit, ShowcaseRatingForm(), unavailable=unavailable,
                                  status=200 if request.method == 'GET' else 403,
                                  on_stage=_staged_exhibit(event),
                                  already_rated=exhibit.pk in _rated_exhibit_ids(request, event))
        response['Cache-Control'] = 'private, no-store'
        return response
    allowed = allow_public_event_request(request, service='showcase-rating',
        event_id=event.public_id, limit=settings.SHOWCASE_PUBLIC_RATE_LIMIT,
        window=settings.SHOWCASE_PUBLIC_RATE_WINDOW)
    if allowed is None:
        return HttpResponse('Visitor ratings are temporarily unavailable.', status=503,
            headers={'Cache-Control': 'private, no-store', 'Retry-After': '10'})
    if not allowed:
        return HttpResponse('Too many rating requests from this network. Please try again shortly.',
            status=429, headers={'Cache-Control': 'private, no-store',
                'Retry-After': str(settings.SHOWCASE_PUBLIC_RATE_WINDOW)})

    cookie_name = _cookie_name(event)
    existing_token = request.COOKIES.get(cookie_name, '')
    token_valid = _valid_device_token(existing_token, event)
    issued_token = None
    if request.method == 'GET':
        if not token_valid:
            issued_token = ShowcaseRating.issue_device_token(event)
        response = _render_rating(request, exhibit, ShowcaseRatingForm(),
                                  already_rated=exhibit.pk in _rated_exhibit_ids(request, event))
        if issued_token:
            response.set_cookie(cookie_name, issued_token, max_age=SHOWCASE_DEVICE_TOKEN_MAX_AGE,
                httponly=True, secure=request.is_secure(), samesite='Lax', path=DEVICE_COOKIE_PATH)
        response['Cache-Control'] = 'private, no-store'
        return response

    if not token_valid:
        form = ShowcaseRatingForm(request.POST)
        issued_token = ShowcaseRating.issue_device_token(event)
        response = _render_rating(request, exhibit, form, status=400,
            message='Your device link expired. Review your rating and submit it again.')
        response.set_cookie(cookie_name, issued_token, max_age=SHOWCASE_DEVICE_TOKEN_MAX_AGE,
            httponly=True, secure=request.is_secure(), samesite='Lax', path=DEVICE_COOKIE_PATH)
        response['Cache-Control'] = 'private, no-store'
        return response

    form = ShowcaseRatingForm(request.POST)
    if not form.is_valid():
        response = _render_rating(request, exhibit, form, status=400)
        response['Cache-Control'] = 'private, no-store'
        return response
    try:
        submit_showcase_rating(event, exhibit, existing_token,
            int(form.cleaned_data['stars']))
    except ValidationError as exc:
        form.add_error(None, exc.messages[0])
        response = _render_rating(request, exhibit, form, status=400)
        response['Cache-Control'] = 'private, no-store'
        return response
    broadcast_showcase_event(event, 'showcase.rating', {})
    response = _render_rating(request, exhibit, form, submitted=True)
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET'])
@never_cache
def showcase_exhibit_label(request, exhibit_id):
    """Return a print-ready label and QR for the public exhibit rating link."""
    exhibit = (ShowcaseExhibit.objects.select_related('event', 'category')
               .filter(public_id=exhibit_id).first())
    if exhibit is None:
        raise Http404
    rating_url = request.build_absolute_uri(reverse(
        'showcase_rate_exhibit', args=(exhibit.public_id,)))
    qr_url = reverse('showcase_exhibit_qr', args=(exhibit.public_id,))
    response = render(request, 'service_domains/showcase_label.html', {
        'event': exhibit.event, 'exhibit': exhibit, 'category': exhibit.category,
        'rating_url': rating_url, 'qr_url': qr_url,
    })
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET'])
@never_cache
def showcase_exhibit_qr(request, exhibit_id):
    exhibit = ShowcaseExhibit.objects.filter(public_id=exhibit_id).only('public_id').first()
    if exhibit is None:
        raise Http404
    target = request.build_absolute_uri(reverse(
        'showcase_rate_exhibit', args=(exhibit.public_id,)))
    response = HttpResponse(qr_png(target), content_type='image/png')
    response['Cache-Control'] = 'private, no-store'
    return response


@require_safe
@never_cache
def showcase_visit(request, public_id):
    """Visitor landing page behind the event QR: pick a booth, then rate it."""
    event = ShowcaseEvent.objects.filter(public_id=public_id).first()
    if event is None:
        raise Http404
    unavailable = _rating_unavailable_message(event)
    staged = None
    if not unavailable:
        staged = _staged_exhibit(event)
        if (event.workflow_snapshot or {}).get('floor_state', {}).get('visitor_voting_paused', False):
            unavailable = 'Visitor ratings are temporarily paused by the organizer. Please check back shortly.'
    groups = {}
    if event.status != ShowcaseEvent.Status.DRAFT:
        for exhibit in event.exhibits.select_related('category').order_by(
                'category__order', 'category__name', 'order', 'pk'):
            groups.setdefault(exhibit.category.name if exhibit.category_id else 'Exhibits', []).append(exhibit)
    rated_ids = _rated_exhibit_ids(request, event)
    response = render(request, 'service_domains/showcase_visit.html', {
        'event': event, 'groups': groups, 'unavailable': unavailable, 'staged': staged,
        'rated_ids': rated_ids, 'staged_rated': bool(staged and staged.pk in rated_ids),
    })
    response['Cache-Control'] = 'private, no-store'
    return response


@require_safe
@never_cache
def showcase_visit_qr(request, public_id):
    if not ShowcaseEvent.objects.filter(public_id=public_id).exists():
        raise Http404
    target = request.build_absolute_uri(reverse('showcase_visit', args=(public_id,)))
    response = HttpResponse(qr_png(target), content_type='image/png')
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET'])
def showcase_public_results(request, public_id):
    """Publish only released aggregate awards; cache the public projection briefly."""
    event = ShowcaseEvent.objects.filter(
        public_id=public_id,
        status=ShowcaseEvent.Status.PUBLISHED,
        public_results=True,
    ).first()
    if event is None:
        raise Http404

    cache_key = f'livegrade:showcase:public-results:v1:{event.public_id.hex}'
    try:
        results = cache.get(cache_key)
    except Exception:
        # Public results remain available from the database during a shared
        # cache outage; the publication gate above still applies.
        logger.warning('Showcase public-results cache read failed; recomputing event_id=%s.',
            event.pk)
        results = None
    if results is None:
        awards = showcase_awards(event)
        certificates = {
            (row.exhibit_id, row.award_type): row
            for row in ShowcaseCertificate.objects.filter(event=event)
        }
        categories = []
        for award in awards['categories']:
            winner = award['winner']
            certificate = (certificates.get((winner.pk, ShowcaseCertificate.AwardType.CATEGORY))
                           if winner else None)
            categories.append({
                'category': award['category'],
                'winner': winner.name if winner else None,
                'certificate_url': (reverse('showcase_certificate_verify',
                    args=(certificate.public_id,)) if certificate else None),
                'tied': [exhibit.name for exhibit in award['tied']],
                'tied_details': [
                    {'exhibit': item['exhibit'].name, 'mean': str(item['mean']),
                     'judge_count': item['judge_count']}
                    for item in award['tied_details']
                ],
                'mean': str(award['mean']) if award['mean'] is not None else None,
                'judge_count': award['judge_count'],
            })
        favorite = awards['audience_favorite']
        favorite_certificate = (certificates.get((favorite.pk, ShowcaseCertificate.AwardType.AUDIENCE))
                                if favorite else None)
        results = {
            'categories': categories,
            'audience_favorite': favorite.name if favorite else None,
            'audience_favorite_certificate_url': (reverse('showcase_certificate_verify',
                args=(favorite_certificate.public_id,)) if favorite_certificate else None),
            'audience_favorite_tied': [exhibit.name for exhibit in awards['audience_favorite_tied']],
            'audience_favorite_tied_details': [
                {'exhibit': item['exhibit'].name, 'mean': str(item['mean']),
                 'rating_count': item['rating_count']}
                for item in awards['audience_favorite_tied_details']
            ],
            'audience_favorite_mean': (
                str(awards['audience_favorite_mean'])
                if awards['audience_favorite_mean'] is not None else None),
            'audience_favorite_rating_count': awards['audience_favorite_rating_count'],
        }
        try:
            cache.set(cache_key, results, SHOWCASE_RESULTS_CACHE_SECONDS)
        except Exception:
            logger.warning('Showcase public-results cache write failed; event_id=%s.',
                event.pk)

    response = render(request, 'service_domains/showcase_results.html', {
        'event': event,
        'results': results,
    })
    # Keep the short server-side aggregate cache above, but never let a browser
    # or intermediary serve a stale public page after the owner revokes results.
    response['Cache-Control'] = 'no-store'
    return response


@require_http_methods(['GET'])
def showcase_certificate_verify(request, public_id):
    certificate = (ShowcaseCertificate.objects.select_related('event', 'exhibit')
        .filter(public_id=public_id).first())
    if certificate is None:
        raise Http404
    response = render(request, 'service_domains/showcase_certificate.html', {
        'certificate': certificate,
    })
    response['Cache-Control'] = 'public, max-age=300'
    return response


def _owned_training_series_session(request, series_id, session_id):
    """Retrieve verified series and session within tenant and ownership boundary."""
    series = _owned_training_series(request, series_id)
    session = (TrainingSeriesSession.objects.for_organization(request.organization)
               .owned_by(request.user).filter(pk=session_id, series=series).select_related('assessment_session').first())
    if session is None:
        raise Http404
    return series, session


@require_http_methods(['GET'])
@never_cache
@_team_owner
def training_session_live(request, series_id, session_id):
    """Render the workshop live command and observation console for facilitators."""
    series, session = _owned_training_series_session(request, series_id, session_id)
    context = get_training_session_live_state(series, session)
    feedback_url = request.build_absolute_uri(
        reverse('training_anonymous_feedback', kwargs={'feedback_public_id': session.feedback_public_id})
    )
    qr_img_url = reverse('training_session_feedback_qr', kwargs={
        'series_id': series.pk, 'session_id': session.pk
    }) + '?format=png'
    context['feedback_url'] = feedback_url
    context['qr_img_url'] = qr_img_url
    assessment_session = session.assessment_session
    context['session_status'] = assessment_session.status
    context['can_go_live'] = (assessment_session.status == AssessmentSession.Status.DRAFT
                              and assessment_session.can_go_live())
    context['participant_count'] = TrainingParticipant.objects.filter(
        series=series, organization_id=series.organization_id).count()
    response = render(request, 'service_domains/training_session_live.html', context)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    return response


@require_http_methods(['GET'])
@never_cache
@_team_owner
def training_session_projector(request, series_id, session_id):
    """Render public-safe auditorium/screen projection for workshop drills."""
    series, session = _owned_training_series_session(request, series_id, session_id)
    context = get_training_session_live_state(series, session)
    feedback_url = request.build_absolute_uri(
        reverse('training_anonymous_feedback', kwargs={'feedback_public_id': session.feedback_public_id})
    )
    qr_img_url = reverse('training_session_feedback_qr', kwargs={
        'series_id': series.pk, 'session_id': session.pk
    }) + '?format=png'
    context['feedback_url'] = feedback_url
    context['qr_img_url'] = qr_img_url
    response = render(request, 'service_domains/training_session_projector.html', context)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    return response


def _training_live_json(request, series, session):
    """The one live-state payload: server clock, counters and the console fragments."""
    context = get_training_session_live_state(series, session)
    context['feedback_url'] = request.build_absolute_uri(
        reverse('training_anonymous_feedback', kwargs={'feedback_public_id': session.feedback_public_id}))
    context['qr_img_url'] = reverse('training_session_feedback_qr', kwargs={
        'series_id': series.pk, 'session_id': session.pk}) + '?format=png'
    context['session_status'] = session.assessment_session.status
    fragments = {
        'hero_html': 'training_live_hero', 'competencies_html': 'training_live_competencies',
        'pods_html': 'training_live_pods',
    }
    return JsonResponse({
        'ok': True,
        'session_status': context['session_status'],
        'active_drill_phase': context['drill_phase'],
        'drill_timer': context['drill_timer'],
        'drill_seconds_remaining': context['drill_seconds_remaining'],
        'timer_paused': context['timer_paused'],
        **{key: render_to_string(f'service_domains/_partials/{name}.html', context, request=request)
           for key, name in fragments.items()},
    })


@require_http_methods(['GET'])
@never_cache
@_team_owner
def training_session_live_state(request, series_id, session_id):
    """Real-time JSON state endpoint returning drill timer, feedback meter, and HTML fragments."""
    series, session = _owned_training_series_session(request, series_id, session_id)
    return _training_live_json(request, series, session)


@require_POST
@never_cache
@_team_owner
def training_session_live_action(request, series_id, session_id):
    """Execute atomic live workshop operations: drill phase, timer, competency scoring, and pod visits."""
    series, session = _owned_training_series_session(request, series_id, session_id)
    action = request.POST.get('action')
    try:
        if action != 'start_session' and session.assessment_session.status != AssessmentSession.Status.LIVE:
            raise ValidationError('This session is not live.')
        if action == 'set_drill_phase':
            phase = request.POST.get('phase')
            set_training_drill_phase(session, phase, organization=request.organization, user=request.user)
        elif action == 'adjust_drill_timer':
            delta = int(request.POST.get('delta', 300))
            adjust_training_drill_timer(session, delta, organization=request.organization, user=request.user)
        elif action == 'toggle_drill_pause':
            toggle_training_drill_pause(session, organization=request.organization, user=request.user)
        elif action == 'record_competency_score':
            criterion_id = int(request.POST.get('criterion_id'))
            student_id = int(request.POST.get('student_id'))
            score = Decimal(request.POST.get('score'))
            evidence = request.POST.get('evidence', '').strip() or 'Direct observation during drill phase.'
            criterion = TrainingSessionCompetency.objects.filter(pk=criterion_id, series_session=session).first()
            student = session.assessment_session.students.filter(pk=student_id).first()
            if criterion and student:
                record_training_competency_result(
                    session.assessment_session, criterion, student, score, evidence,
                    organization=request.organization, user=request.user
                )
        elif action == 'mark_pod_visited':
            pod_name = request.POST.get('pod_name', '').strip()
            if pod_name:
                mark_training_pod_visited(session, pod_name, organization=request.organization, user=request.user)
        elif action == 'save_observation_note':
            key = request.POST.get('key', '').strip()
            note = request.POST.get('note', '').strip()
            if key:
                save_training_observation_note(session, key, note, organization=request.organization, user=request.user)
        elif action == 'start_session':
            assessment_session = session.assessment_session
            if assessment_session.status != AssessmentSession.Status.DRAFT:
                raise ValidationError('This session has already started.')
            if not assessment_session.can_go_live():
                raise ValidationError('Add rubric categories that total 100% and at least one group before starting.')
            assessment_session.status = AssessmentSession.Status.LIVE
            assessment_session.save(update_fields=('status',))
            messages.success(request, 'The session is live.')
            broadcast_training_session_event(session.pk, 'training.start_session')
            redirect_url = reverse('training_session_live', args=(series.pk, session.pk))
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'status': 'live', 'redirect': redirect_url})
            return redirect(redirect_url)
        elif action == 'conclude_session':
            with transaction.atomic():
                assessment_session = session.assessment_session
                assessment_session.status = 'closed'
                assessment_session.save(update_fields=('status',))
            messages.success(request, 'Workshop session concluded and results preserved.')
            broadcast_training_session_event(session.pk, 'training.conclude_session')
            redirect_url = reverse('training_session_competencies', args=(assessment_session.pk,))
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'status': 'closed', 'redirect': redirect_url})
            return redirect(redirect_url)
        else:
            return JsonResponse({'error': f'Unknown action: {action}'}, status=400)
    except (ValidationError, PermissionDenied) as exc:
        msg = '; '.join(getattr(exc, 'messages', [])) or str(exc)
        if request.headers.get('x-requested-with') != 'XMLHttpRequest':
            messages.error(request, msg)
            return redirect('training_session_live', series_id=series.pk, session_id=session.pk)
        return JsonResponse({'error': msg}, status=400)

    session.refresh_from_db()
    broadcast_training_session_event(session.pk, f'training.{action}')
    return _training_live_json(request, series, session)


@require_http_methods(['GET'])
@never_cache
@_team_owner
def training_session_feedback_qr(request, series_id, session_id):
    """The attendee feedback link as a PNG QR code."""
    series, session = _owned_training_series_session(request, series_id, session_id)
    feedback_url = request.build_absolute_uri(
        reverse('training_anonymous_feedback', kwargs={'feedback_public_id': session.feedback_public_id})
    )
    response = HttpResponse(qr_png(feedback_url), content_type='image/png')
    response['Cache-Control'] = 'private, no-store'
    return response
