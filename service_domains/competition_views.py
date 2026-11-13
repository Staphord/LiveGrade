"""Owner-scoped Competition organizer setup; results remain behind policy gates."""

from decimal import Decimal
from functools import wraps
import re
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.access import lecturer_required
from assessments.pagination import PAGE_SIZE, PAGE_SIZES, paginate
from assessments.qr import qr_png

from .competition_forms import (CompetitionEntryForm, CompetitionFirstRoundForm,
    CompetitionAdvanceRoundForm, CompetitionAudienceJoinForm, CompetitionFinalTieForm, CompetitionForm,
    CompetitionCriterionForm, CompetitionJudgeInvitationForm,
    CompetitionTrackForm, competition_head_judge_requirements)
from .competition_import import (apply_competition_entry_import, parse_competition_entries,
    plan_entries, sample_competition_entries)
from .models import (Competition, CompetitionAudienceBallot, CompetitionCriterion, CompetitionEntry, CompetitionJudgeInvitation,
    CompetitionJudgeScore, CompetitionRound, CompetitionRoundEntry, CompetitionTrack)
from .operations import (create_initial_competition_round,
    compile_competition_judge_scores, delete_competitions, issue_competition_judge_invitation,
    create_next_competition_round, revoke_competition_judge_invitation,
    start_competition, complete_competition, reveal_competition_leaderboard)
from .operations import set_competition_public_leaderboard
from .operations import (competition_final_tie_requirements,
    resolve_competition_final_ties, competition_audience_join_code,
    competition_audience_ranked_choice, finalize_competition_weighted_scores,
    get_competition_live_state_context, set_competition_stage_entry,
    adjust_competition_pitch_timer, toggle_competition_pitch_pause,
    toggle_competition_audience_voting, set_competition_projector_mode)
from .models import CompetitionLeaderboardTieDecision
from .throttling import allow_public_event_request
from .onboarding import service_onboarding
from . import embedded_import
from .competition_criteria_import import (
    apply_criteria, parse_criteria, plan_criteria, sample_workbook as criteria_sample_workbook)
from .forms import SpreadsheetImportForm
from portal.stepper import steps_from_setup, unsaved_steps

_COMPETITION_CREATE_STEPS = unsaved_steps([
    ('details', 'Competition overview'), ('tracks', 'Tracks and categories'),
    ('entries', 'Entries'), ('criteria', 'Scoring criteria'),
    ('launch', 'Review'),
], hint='Save competition details to unlock this step')


COMPETITION_LEADERBOARD_CACHE_SECONDS = 300
COMPETITION_SETUP_STEPS = ('details', 'tracks', 'entries', 'criteria', 'launch')


def _competition_owner(view):
    authenticated_view = lecturer_required(view)

    @wraps(view)
    def scoped(request, *args, **kwargs):
        if getattr(request, 'service', None) != 'competition':
            raise Http404
        return authenticated_view(request, *args, **kwargs)

    return scoped


def _owned_competition(request, competition_id):
    competition = (Competition.objects.for_organization(request.organization)
                   .owned_by(request.user).filter(pk=competition_id).first())
    if competition is None:
        raise Http404
    return competition


@require_safe
@never_cache
@_competition_owner
def competition_list(request):
    competitions = list(Competition.objects.for_organization(request.organization)
                        .owned_by(request.user)
                        .annotate(track_count=Count('tracks', distinct=True),
                                  round_count=Count('rounds', distinct=True))
                        .order_by('-created_at', '-pk'))
    counts = {
        'total': len(competitions),
        'active': sum(c.status == Competition.Status.ACTIVE for c in competitions),
        'draft': sum(c.status == Competition.Status.DRAFT for c in competitions),
        'complete': sum(c.status == Competition.Status.COMPLETE for c in competitions),
        'revealed': sum(c.status == Competition.Status.REVEALED for c in competitions),
        'total_tracks': sum(c.track_count for c in competitions),
        'total_rounds': sum(c.round_count for c in competitions),
    }
    attention_competitions = [
        c for c in competitions
        if c.status in (Competition.Status.ACTIVE, Competition.Status.COMPLETE)
    ]

    stage_distribution = []
    stage_colors = {
        'draft': '#d28a08',
        'active': 'var(--success)',
        'complete': 'var(--accent)',
        'revealed': 'var(--text-muted)',
    }
    pie_segments = []
    cumulative = 0
    stage_definitions = (
        ('draft', 'Draft', counts['draft']),
        ('active', 'In progress', counts['active']),
        ('complete', 'Complete', counts['complete']),
        ('revealed', 'Leaderboard revealed', counts['revealed']),
    )
    for key, label, count in stage_definitions:
        start_deg = cumulative * 360 / counts['total'] if counts['total'] else 0
        cumulative += count
        end_deg = cumulative * 360 / counts['total'] if counts['total'] else 0
        stage_distribution.append({
            'key': key,
            'label': label,
            'count': count,
            'percentage': round(count * 100 / counts['total'], 1) if counts['total'] else 0,
        })
        if count:
            pie_segments.append(f'{stage_colors[key]} {start_deg:.3f}deg {end_deg:.3f}deg')
    stage_pie_background = (
        f"conic-gradient({', '.join(pie_segments)})" if pie_segments else ''
    )

    largest_competitions = sorted(
        competitions,
        key=lambda c: (-c.round_count, -c.created_at.timestamp() if c.created_at else 0, c.pk)
    )[:5]
    largest = max((c.round_count for c in largest_competitions), default=0)
    for comp in largest_competitions:
        comp.round_bar_percent = round(comp.round_count * 100 / largest, 1) if largest else 0

    recent_competitions = competitions[:5]

    return render(request, 'service_domains/competition_list.html', {
        'dashboard_stats': counts,
        'attention_competitions': attention_competitions,
        'stage_distribution': stage_distribution,
        'stage_pie_background': stage_pie_background,
        'largest_competitions': largest_competitions,
        'largest': largest,
        'recent_competitions': recent_competitions,
    })


@require_safe
@never_cache
@_competition_owner
def competition_table(request):
    visible = (Competition.objects.for_organization(request.organization)
               .owned_by(request.user))
    counts = visible.aggregate(
        total=Count('pk'),
        draft=Count('pk', filter=Q(status=Competition.Status.DRAFT)),
        active=Count('pk', filter=Q(status=Competition.Status.ACTIVE)),
        complete=Count('pk', filter=Q(status=Competition.Status.COMPLETE)),
        revealed=Count('pk', filter=Q(status=Competition.Status.REVEALED)),
    )
    search_query = request.GET.get('q', '').strip()[:100]
    status_filter = request.GET.get('status', '')
    if status_filter not in Competition.Status.values:
        status_filter = ''
    filtered = visible
    if search_query:
        filtered = filtered.filter(name__icontains=search_query)
    if status_filter:
        filtered = filtered.filter(status=status_filter)

    queryset = (filtered
        .annotate(track_count=Count('tracks', distinct=True),
                  round_count=Count('rounds', distinct=True))
        .order_by('-created_at', '-pk'))

    paginator, page = paginate(request, queryset)

    query_params = QueryDict('', mutable=True)
    if search_query:
        query_params['q'] = search_query
    if status_filter:
        query_params['status'] = status_filter
    query_params['per_page'] = str(page.paginator.per_page)

    context = {
        'competitions': page,
        'page_obj': page,
        'paginator': paginator,
        'per_page': page.paginator.per_page,
        'page_sizes': PAGE_SIZES,
        'status_counts': counts,
        'status_filter': status_filter,
        'search_query': search_query,
        'querystring': query_params.urlencode(),
        'filtered_count': paginator.count,
    }

    template = 'service_domains/_partials/competition_results.html' if request.headers.get('x-requested-with') == 'XMLHttpRequest' else 'service_domains/competition_table.html'
    response = render(request, template, context)
    patch_vary_headers(response, ['X-Requested-With'])
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
@_competition_owner
def competition_create(request):
    """Create a new Competition and proceed directly to setup steps."""
    org_id = request.organization.pk if hasattr(request.organization, 'pk') else (
        request.organization['id'] if isinstance(request.organization, dict) else request.organization)

    if request.method == 'POST':
        form = CompetitionForm(request.POST, auto_id='competition_details_%s')
        if form.is_valid():
            competition = form.save(commit=False)
            competition.organization_id = org_id
            competition.created_by = request.user
            try:
                competition.full_clean()
            except ValidationError as exc:
                form.add_error(None, '; '.join(exc.messages))
                return render(request, 'service_domains/competition_create.html', {'form': form, 'stepper_steps': _COMPETITION_CREATE_STEPS})
            competition.save(force_insert=True)
            messages.success(request, f'"{competition.name}" created.')
            leave_to = request.POST.get('leave_to', '')
            if leave_to and url_has_allowed_host_and_scheme(
                    leave_to, allowed_hosts={request.get_host()},
                    require_https=request.is_secure()):
                return redirect(leave_to)
            if request.POST.get('advance') == '1':
                return redirect(f"{reverse('competition_manage', kwargs={'competition_id': competition.pk})}?step=tracks")
            return redirect(f"{reverse('competition_manage', kwargs={'competition_id': competition.pk})}?step=details")
        return render(request, 'service_domains/competition_create.html', {'form': form, 'stepper_steps': _COMPETITION_CREATE_STEPS})

    form = CompetitionForm(auto_id='competition_details_%s')
    return render(request, 'service_domains/competition_create.html', {'form': form, 'stepper_steps': _COMPETITION_CREATE_STEPS})


@require_POST
@never_cache
@_competition_owner
def competition_delete(request, competition_id):
    competition = _owned_competition(request, competition_id)
    name = competition.name
    delete_competitions([competition])
    messages.success(request, f'"{name}" deleted.')
    return redirect('competition_table')


def _competition_bulk_delete_redirect(request):
    """Return to the current filtered view, with a valid page-size."""
    params = QueryDict('', mutable=True)
    query = (request.POST.get('q') or '').strip()[:100]
    status_filter = request.POST.get('status', '')
    if query:
        params['q'] = query
    if status_filter in Competition.Status.values:
        params['status'] = status_filter
    try:
        per_page = int(request.POST.get('per_page', PAGE_SIZE))
    except (TypeError, ValueError):
        per_page = PAGE_SIZE
    if per_page not in PAGE_SIZES:
        per_page = PAGE_SIZE
    params['per_page'] = str(per_page)
    url = reverse('competition_table')
    return redirect(f'{url}?{params.urlencode()}' if params else url)


@require_POST
@never_cache
@_competition_owner
def competition_bulk_delete(request):
    """Delete an explicitly selected set of the current owner's competitions."""
    raw_ids = request.POST.getlist('competition_ids')
    if (not raw_ids or len(raw_ids) > max(PAGE_SIZES)
            or any(not re.fullmatch(r'[1-9][0-9]*', value)
                   or len(value) > 19
                   or (len(value) == 19 and value > '9223372036854775807')
                   for value in raw_ids)):
        messages.error(request, 'Select one or more valid competitions to delete.')
        return _competition_bulk_delete_redirect(request)

    competition_ids = {int(value) for value in raw_ids}
    owned_competitions = (Competition.objects.for_organization(request.organization)
                          .owned_by(request.user))
    with transaction.atomic():
        selected = list(owned_competitions.select_for_update().filter(pk__in=competition_ids))
        if len(selected) != len(competition_ids):
            messages.error(request, 'One or more selected competitions are no longer available to delete.')
            return _competition_bulk_delete_redirect(request)
        count = len(selected)
        delete_competitions(selected)

    messages.success(request, f'{count} competition{"s" if count != 1 else ""} deleted.')
    return _competition_bulk_delete_redirect(request)


@require_POST
@never_cache
@_competition_owner
def competition_criteria_import(request, competition_id):
    """Preview, apply or cancel a criteria spreadsheet; returns to the criteria step."""
    competition = _owned_competition(request, competition_id)
    manage_url = reverse('competition_manage', args=(competition.pk,))
    return embedded_import.handle_post(
        request, kind='competition-criteria', owner=competition, form_class=SpreadsheetImportForm,
        parse=parse_criteria, plan=plan_criteria, apply=apply_criteria,
        success='Judging criteria imported.',
        back=lambda: redirect(f'{manage_url}?step=criteria'))


@require_safe
@never_cache
@_competition_owner
def competition_criteria_import_sample(request, competition_id):
    competition = _owned_competition(request, competition_id)
    track_names = list(competition.tracks.order_by('pk').values_list('name', flat=True))
    response = HttpResponse(criteria_sample_workbook(track_names).getvalue(), content_type=(
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))
    response['Content-Disposition'] = 'attachment; filename="competition-criteria-sample.xlsx"'
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
@_competition_owner
def competition_manage(request, competition_id):
    competition = _owned_competition(request, competition_id)
    manage_url = reverse('competition_manage', kwargs={'competition_id': competition.pk})
    from_param = request.GET.get('from') or request.POST.get('from', '')
    if from_param == 'leaderboard' or competition.status in (Competition.Status.COMPLETE, Competition.Status.REVEALED):
        back_url = reverse('competition_public_leaderboard', kwargs={'competition_id': competition.public_id})
        back_label = 'Back to Leaderboard'
    elif from_param == 'live' or competition.status == Competition.Status.ACTIVE:
        back_url = reverse('competition_live_control', kwargs={'competition_id': competition.pk})
        back_label = 'Back to Live Arena'
    else:
        back_url = reverse('competition_list')
        back_label = 'Competitions'
    from_query = f'&from={quote(from_param)}' if from_param else ''
    details_form = CompetitionForm(instance=competition, auto_id='competition_details_%s')
    track_form = CompetitionTrackForm(auto_id='track_%s')
    first_round_form = CompetitionFirstRoundForm(auto_id='first_round_%s')
    criterion_form = CompetitionCriterionForm(organization=request.organization,
        user=request.user, competition=competition, auto_id='criterion_%s')
    entry_form = CompetitionEntryForm(competition=competition,
        organization=request.organization, user=request.user, auto_id='entry_%s')
    judge_form = None
    advance_round_form = None
    action_error = ''
    judge_notice = ''
    manual_invitation_url = ''
    tally_results = []
    audience_tally = None
    can_advance_round = False
    can_finalize_weighted = False
    final_tie_groups = []
    final_tie_form = None
    if competition.status == Competition.Status.COMPLETE:
        try:
            final_tie_groups = competition_final_tie_requirements(
                competition, organization=request.organization, user=request.user)
        except PermissionDenied:
            raise Http404
        final_tie_form = CompetitionFinalTieForm(tie_groups=final_tie_groups)
    if request.method == 'POST':
        action = request.POST.get('action')
        if (competition.status not in (Competition.Status.DRAFT, Competition.Status.ACTIVE,
                                       Competition.Status.COMPLETE, Competition.Status.REVEALED)
                or (competition.status == Competition.Status.ACTIVE
                    and action not in ('update_competition', 'invite_judge', 'tally_judges', 'revoke_judge',
                                       'advance_round', 'complete', 'tally_audience',
                                       'finalize_weighted_scores'))
                or (competition.status == Competition.Status.COMPLETE
                    and action not in ('reveal', 'resolve_final_ties'))
                or (competition.status == Competition.Status.REVEALED
                    and action not in ('show_leaderboard', 'hide_leaderboard'))
                or (competition.status == Competition.Status.DRAFT
                    and action in ('complete', 'reveal'))):
            raise Http404
        has_rounds = CompetitionRound.objects.filter(competition=competition).exists()
        if action == 'complete':
            try:
                complete_competition(competition, organization=request.organization,
                    user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = ('; '.join(getattr(exc, 'messages', [])) or
                    'Competition results could not be finalized.')
            else:
                return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'resolve_final_ties':
            final_tie_form = CompetitionFinalTieForm(request.POST,
                tie_groups=final_tie_groups)
            if final_tie_form.is_valid():
                try:
                    resolve_competition_final_ties(competition,
                        final_tie_form.decision_payloads(),
                        organization=request.organization, user=request.user)
                except PermissionDenied:
                    raise Http404
                except ValidationError as exc:
                    final_tie_form.add_error(None, '; '.join(exc.messages))
                else:
                    return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'reveal':
            try:
                reveal_competition_leaderboard(competition, organization=request.organization,
                    user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = ('; '.join(getattr(exc, 'messages', [])) or
                    'The leaderboard could not be revealed.')
            else:
                return redirect('competition_manage', competition_id=competition.pk)
        elif action in ('show_leaderboard', 'hide_leaderboard'):
            try:
                set_competition_public_leaderboard(competition,
                    action == 'show_leaderboard', organization=request.organization,
                    user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = ('; '.join(getattr(exc, 'messages', [])) or
                    'The public leaderboard could not be updated.')
            else:
                return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'revoke_judge':
            try:
                revoke_competition_judge_invitation(competition,
                    request.POST.get('invitation_id'), organization=request.organization,
                    user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = ('; '.join(getattr(exc, 'messages', [])) or
                    'This invitation cannot be revoked.')
            else:
                judge_notice = 'Judge invitation revoked.'
        elif action == 'tally_judges':
            target_track = (CompetitionTrack.objects.for_organization(request.organization)
                .owned_by(request.user).filter(pk=request.POST.get('track_id'),
                    competition=competition).first())
            latest_for_tally = (CompetitionRound.objects.for_organization(request.organization)
                .owned_by(request.user).filter(competition=competition)
                .order_by('-order').first())
            if target_track is None or latest_for_tally is None:
                raise Http404
            try:
                tally_results = compile_competition_judge_scores(
                    competition, latest_for_tally, target_track,
                    organization=request.organization, user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = ('; '.join(getattr(exc, 'messages', [])) or
                    'Judge scores could not be tallied.')
        elif action == 'tally_audience':
            if competition.effective_audience_weight > 0:
                action_error = ('Weighted events use the disclosed Borda ranking score; '
                    'review it after voting closes and weighted results are finalized.')
            else:
                target_track = (CompetitionTrack.objects.for_organization(request.organization)
                    .owned_by(request.user).filter(pk=request.POST.get('track_id'),
                        competition=competition).first())
                latest_for_tally = (CompetitionRound.objects.for_organization(request.organization)
                    .owned_by(request.user).filter(competition=competition)
                    .order_by('-order').first())
                if target_track is None or latest_for_tally is None:
                    raise Http404
                try:
                    result = competition_audience_ranked_choice(competition, latest_for_tally,
                        target_track, organization=request.organization, user=request.user)
                except PermissionDenied:
                    raise Http404
                except ValidationError as exc:
                    action_error = '; '.join(exc.messages)
                else:
                    rows = list(CompetitionRoundEntry.objects.for_organization(
                        request.organization).owned_by(request.user).filter(
                            round=latest_for_tally, track=target_track)
                        .select_related('entry').order_by('entry__order', 'entry_id'))
                    names = {row.entry_id: row.entry.name for row in rows}
                    audience_tally = {
                        'track': target_track.name,
                        'winner': names.get(result['winner']) if result['winner'] is not None else None,
                        'has_ballots': bool(result['rounds']) and any(
                            sum(counts.values()) for counts in result['rounds']),
                        'tied': [names.get(entry_id, 'Unavailable entry')
                                 for entry_id in result['tied']],
                        'resolved_by': result['resolved_by'],
                        'rounds': [{names.get(entry_id, 'Unavailable entry'): count
                                    for entry_id, count in tally.items()}
                                   for tally in result['rounds']],
                    }
        elif action == 'finalize_weighted_scores':
            latest_for_tally = (CompetitionRound.objects.for_organization(request.organization)
                .owned_by(request.user).filter(competition=competition)
                .order_by('-order').first())
            if latest_for_tally is None:
                raise Http404
            try:
                finalize_competition_weighted_scores(competition, latest_for_tally,
                    organization=request.organization, user=request.user)
            except (PermissionDenied, ValidationError) as exc:
                action_error = ('; '.join(getattr(exc, 'messages', [])) or
                    'Weighted results could not be finalized.')
            else:
                return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'advance_round':
            latest_for_advance = (CompetitionRound.objects.for_organization(request.organization)
                .owned_by(request.user).filter(competition=competition)
                .order_by('-order').first())
            if latest_for_advance is None:
                raise Http404
            scoped_tracks = list(CompetitionTrack.objects.for_organization(request.organization)
                .owned_by(request.user).filter(competition=competition).order_by('pk'))
            source_entries = list(CompetitionRoundEntry.objects.for_organization(
                request.organization).owned_by(request.user).filter(round=latest_for_advance)
                .select_related('entry').order_by('track_id', 'entry__order', 'entry_id'))
            tie_requirements = competition_head_judge_requirements(scoped_tracks, source_entries)
            advance_round_form = CompetitionAdvanceRoundForm(request.POST,
                tie_requirements=tie_requirements)
            if advance_round_form.is_valid():
                try:
                    create_next_competition_round(competition, latest_for_advance,
                        advance_round_form.cleaned_data['name'],
                        organization=request.organization, user=request.user,
                        head_judge_decisions=advance_round_form.head_judge_decisions(),
                        head_judge_reasons=advance_round_form.head_judge_reasons())
                except PermissionDenied:
                    raise Http404
                except ValidationError as exc:
                    advance_round_form.add_error(None, '; '.join(exc.messages))
                else:
                    return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'invite_judge':
            judge_form = CompetitionJudgeInvitationForm(request.POST,
                organization=request.organization, user=request.user,
                competition=competition)
            if judge_form.is_valid():
                latest_round = (CompetitionRound.objects.for_organization(request.organization)
                    .owned_by(request.user).filter(competition=competition)
                    .order_by('-order').first())
                if latest_round is None:
                    judge_form.add_error(None, 'Create a round before inviting judges.')
                else:
                    try:
                        invitation, token = issue_competition_judge_invitation(
                            competition, latest_round, judge_form.cleaned_data['track'],
                            judge_form.cleaned_data['email'], judge_form.cleaned_data['expires_at'],
                            organization=request.organization, user=request.user)
                    except (PermissionDenied, ValidationError) as exc:
                        judge_form.add_error(None, '; '.join(getattr(exc, 'messages', []))
                            or 'This competition cannot accept judge invitations.')
                    else:
                        manual_invitation_url = request.build_absolute_uri(reverse(
                            'competition_judge_scorecard', kwargs={'token': token}))
                        try:
                            sent = send_mail(
                                subject=f'Competition judge invitation: {competition.name}',
                                message=(f'You are invited to score entries in {competition.name} '
                                    f'({invitation.track.name}). Use this private link before '
                                    f'{invitation.expires_at}: {manual_invitation_url}'),
                                from_email=None, recipient_list=[invitation.email],
                                fail_silently=False)
                        except Exception:
                            sent = 0
                        if sent:
                            judge_notice = 'Judge invitation emailed. The private link is not shown again.'
                            manual_invitation_url = ''
                        else:
                            judge_notice = ('Email delivery failed. Copy this private link now; '
                                'it will not be shown again.')
        elif has_rounds and action in ('track', 'remove_track',
                                       'entry', 'remove_entry', 'criterion', 'delete_criterion'):
            action_error = 'Track, entry and scoring-criterion setup is locked after the initial round is created.'
        elif action == 'update_competition':
            details_form = CompetitionForm(
                request.POST, instance=competition, auto_id='competition_details_%s')
            if details_form.is_valid():
                updated = details_form.save(commit=False)
                try:
                    updated.full_clean()
                except ValidationError as exc:
                    details_form.add_error(None, '; '.join(exc.messages))
                else:
                    updated.save()
                    if request.POST.get('advance') == '1':
                        messages.success(request, 'Competition details saved.')
                        return redirect(f'{manage_url}?step=tracks{from_query}')
                    return_step = request.POST.get('return_step', '')
                    if return_step in COMPETITION_SETUP_STEPS:
                        messages.success(request, 'Competition details saved.')
                        return redirect(f'{manage_url}?step={return_step}{from_query}')
                    leave_to = request.POST.get('leave_to', '')
                    if leave_to and url_has_allowed_host_and_scheme(
                            leave_to, allowed_hosts={request.get_host()},
                            require_https=request.is_secure()):
                        messages.success(request, 'Competition details saved.')
                        return redirect(leave_to)
                    current_step = request.POST.get('setup_step', 'details')
                    if current_step not in COMPETITION_SETUP_STEPS:
                        current_step = 'details'
                    return redirect(f'{manage_url}?step={current_step}&saved=1{from_query}')
        elif action == 'criterion':
            criterion = CompetitionCriterion(organization_id=competition.organization_id,
                created_by=request.user)
            criterion_form = CompetitionCriterionForm(request.POST,
                instance=criterion, organization=request.organization, user=request.user,
                competition=competition, auto_id='criterion_%s')
            if criterion_form.is_valid():
                criterion = criterion_form.save(commit=False)
                criterion.organization_id = competition.organization_id
                criterion.created_by = request.user
                try:
                    criterion.full_clean()
                except ValidationError as exc:
                    criterion_form.add_error(None, '; '.join(exc.messages))
                else:
                    try:
                        criterion.save(force_insert=True)
                    except IntegrityError:
                        criterion_form.add_error('name', 'This track already has a criterion with that name.')
                    else:
                        messages.success(request, 'Judging criterion added.')
                        if request.POST.get('setup_step'):
                            return redirect(f"{manage_url}?step={request.POST['setup_step']}")
                        return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'delete_criterion':
            criterion = (CompetitionCriterion.objects.for_organization(request.organization)
                .owned_by(request.user).filter(pk=request.POST.get('criterion_id'),
                    track__competition=competition).first())
            if criterion is None:
                raise Http404
            criterion.delete()
            messages.success(request, 'Judging criterion removed.')
            if request.POST.get('setup_step'):
                return redirect(f"{manage_url}?step={request.POST['setup_step']}")
            return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'track':
            track_form = CompetitionTrackForm(request.POST, auto_id='track_%s')
            if track_form.is_valid():
                track = track_form.save(commit=False)
                track.competition = competition
                track.organization_id = competition.organization_id
                track.created_by = request.user
                try:
                    track.full_clean()
                except ValidationError as exc:
                    track_form.add_error(None, '; '.join(exc.messages))
                else:
                    try:
                        with transaction.atomic():
                            track.save(force_insert=True)
                    except IntegrityError:
                        track_form.add_error(None, 'A track with this name already exists.')
                    else:
                        messages.success(request, 'Track added.')
                        if request.POST.get('setup_step'):
                            return redirect(f"{manage_url}?step={request.POST['setup_step']}")
                        return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'remove_track':
            track = get_object_or_404(
                CompetitionTrack.objects.for_organization(request.organization).owned_by(request.user),
                competition=competition, pk=request.POST.get('track_id'))
            track.delete()
            messages.success(request, 'Track removed.')
            if request.POST.get('setup_step'):
                return redirect(f"{manage_url}?step={request.POST['setup_step']}")
            return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'entry':
            entry = CompetitionEntry(organization_id=competition.organization_id,
                created_by=request.user)
            entry_form = CompetitionEntryForm(request.POST, instance=entry,
                competition=competition, organization=request.organization,
                user=request.user, auto_id='entry_%s')
            if entry_form.is_valid():
                entry = entry_form.save(commit=False)
                try:
                    entry.full_clean()
                except ValidationError as exc:
                    entry_form.add_error(None, '; '.join(exc.messages))
                else:
                    try:
                        with transaction.atomic():
                            entry.save(force_insert=True)
                    except IntegrityError:
                        entry_form.add_error(None,
                            'An entry with this reference already exists in the track.')
                    else:
                        messages.success(request, 'Entry added.')
                        if request.POST.get('setup_step'):
                            return redirect(f"{manage_url}?step={request.POST['setup_step']}")
                        return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'remove_entry':
            entry = get_object_or_404(
                CompetitionEntry.objects.for_organization(request.organization).owned_by(request.user),
                track__competition=competition, pk=request.POST.get('entry_id'))
            entry.delete()
            messages.success(request, 'Entry removed.')
            if request.POST.get('setup_step'):
                return redirect(f"{manage_url}?step={request.POST['setup_step']}")
            return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'first_round':
            first_round_form = CompetitionFirstRoundForm(request.POST, auto_id='first_round_%s')
            if first_round_form.is_valid():
                try:
                    create_initial_competition_round(competition, first_round_form.cleaned_data['name'],
                        organization=request.organization, user=request.user)
                except ValidationError as exc:
                    first_round_form.add_error(None, '; '.join(exc.messages))
                else:
                    messages.success(request, 'Initial round created.')
                    if request.POST.get('setup_step'):
                        return redirect(f"{manage_url}?step={request.POST['setup_step']}")
                    return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'start':
            try:
                start_competition(competition,
                    organization=request.organization, user=request.user)
            except ValidationError as exc:
                action_error = '; '.join(exc.messages)
            else:
                return redirect('competition_manage', competition_id=competition.pk)
        else:
            action_error = 'Choose a valid Competition setup action.'

    entry_import_plan, entry_import_token = embedded_import.load_preview(
        request, 'competition-entry', competition, plan_entries)
    entry_import_errors = embedded_import.take_errors(request, 'competition-entry', competition)
    rounds = CompetitionRound.objects.for_organization(request.organization).owned_by(request.user)
    rounds = rounds.filter(competition=competition).order_by('order', 'pk')
    latest_round = rounds.last()
    tracks = list(CompetitionTrack.objects.for_organization(request.organization)
        .owned_by(request.user).filter(competition=competition).order_by('pk'))
    for track in tracks:
        track.organizer_entries = (CompetitionEntry.objects.for_organization(request.organization)
            .owned_by(request.user).filter(track=track).order_by('order', 'pk'))
        track.organizer_criteria = list(CompetitionCriterion.objects.for_organization(request.organization)
            .owned_by(request.user).filter(track=track).order_by('order', 'pk'))
        track.criteria_weight_total = sum((c.weight for c in track.organizer_criteria), Decimal('0'))
        track.judge_invitation_rows = (CompetitionJudgeInvitation.objects.for_organization(
            request.organization).owned_by(request.user).filter(
                competition=competition, round=latest_round, track=track)
            .order_by('-created_at', 'pk'))
    recorded_final_tie_decisions = []
    final_tie_sections = []
    if competition.status in (Competition.Status.COMPLETE, Competition.Status.REVEALED) and latest_round:
        recorded_final_tie_decisions = (CompetitionLeaderboardTieDecision.objects
            .for_organization(request.organization).owned_by(request.user)
            .filter(round=latest_round).select_related('track', 'decided_by')
            .order_by('track__name', '-tied_score'))
        entries_by_id = dict(CompetitionRoundEntry.objects.for_organization(request.organization)
            .owned_by(request.user).filter(round=latest_round)
            .select_related('entry').values_list('entry_id', 'entry__name'))
        for decision in recorded_final_tie_decisions:
            decision.ordered_entry_names = [entries_by_id.get(entry_id, 'Removed entry')
                for entry_id in decision.ordered_entry_ids]
    if final_tie_form is not None:
        final_tie_sections = final_tie_form.display_groups()
    if competition.status == Competition.Status.ACTIVE and latest_round:
        source_entries = list(CompetitionRoundEntry.objects.for_organization(request.organization)
            .owned_by(request.user).filter(round=latest_round).select_related('entry')
            .order_by('track_id', 'entry__order', 'entry_id'))
        tie_requirements = competition_head_judge_requirements(tracks, source_entries)
        if advance_round_form is None:
            advance_round_form = CompetitionAdvanceRoundForm(tie_requirements=tie_requirements)
        for track in tracks:
            track.head_judge_requirement = tie_requirements.get(track.pk)
        for track in tracks:
            track.audience_entries = (CompetitionRoundEntry.objects.for_organization(request.organization)
                .owned_by(request.user).filter(round=latest_round, track=track)
                .select_related('entry').order_by('entry__order', 'entry_id'))
            track.audience_ballot_available = bool(track.audience_entries) and (
                competition.effective_audience_weight > 0 or len(track.audience_entries) >= 2)
            track.audience_ballot_count = CompetitionAudienceBallot.objects.for_organization(
                request.organization).filter(round=latest_round, track=track).count()
            track.audience_join_code = (competition_audience_join_code(
                competition, latest_round, track) if track.audience_ballot_available else '')
            for row in track.audience_entries:
                row.judge_count = CompetitionJudgeScore.objects.filter(
                    round_entry=row).values('invitation_id').distinct().count()
            track.has_judge_scores = (competition.judge_weight == 0 or
                (bool(track.audience_entries) and all(
                    row.judge_count >= competition.effective_minimum_judges
                    for row in track.audience_entries)))
            track.has_final_scores = track.has_judge_scores and all(
                row.score is not None for row in track.audience_entries)
        can_advance_round = bool(tracks) and all(
            track.has_final_scores for track in tracks)
        can_finalize_weighted = (competition.effective_audience_weight > 0
            and latest_round.audience_locked_at is None and bool(tracks)
            and all(track.has_judge_scores
                    and track.audience_ballot_count >= competition.effective_minimum_audience_ballots
                    for track in tracks))
        if judge_form is None and competition.judge_weight > 0:
            judge_form = CompetitionJudgeInvitationForm(
                organization=request.organization, user=request.user, competition=competition)
    if competition.status == Competition.Status.DRAFT and latest_round and competition.judge_weight > 0 and judge_form is None:
        judge_form = CompetitionJudgeInvitationForm(
            organization=request.organization, user=request.user, competition=competition)
    entry_count = sum(len(track.organizer_entries) for track in tracks)
    criterion_count = sum(len(track.organizer_criteria) for track in tracks)
    round_count = len(rounds)
    tracks_ready = bool(tracks)
    entries_ready = tracks_ready and all(bool(track.organizer_entries) for track in tracks)
    criteria_ready = competition.judge_weight == 0 or (
        tracks_ready and all(bool(track.organizer_criteria) for track in tracks))
    judges_ready = competition.judge_weight == 0 or bool(latest_round and all(
        track.judge_invitation_rows for track in tracks))
    setup_steps = [
        {'key': 'details', 'label': 'Competition overview', 'complete': True},
        {'key': 'tracks', 'label': 'Tracks and categories', 'complete': tracks_ready},
        {'key': 'entries', 'label': 'Entries', 'complete': entries_ready},
        {'key': 'criteria',
         'label': 'Scoring criteria' if competition.status == Competition.Status.DRAFT
         else 'Criteria and judges',
         'complete': criteria_ready and (judges_ready or competition.status == Competition.Status.DRAFT)},
        {'key': 'launch', 'label': 'Review',
         'complete': competition.status != Competition.Status.DRAFT},
    ]
    criteria_plan, criteria_token = embedded_import.load_preview(
        request, 'competition-criteria', competition, plan_criteria)
    criteria_import_errors = embedded_import.take_errors(request, 'competition-criteria', competition)
    valid_steps = {step['key'] for step in setup_steps}
    requested_step = (request.POST.get('setup_step') or request.GET.get('step')
                      or ('entries' if entry_import_errors else None))
    if requested_step in valid_steps:
        active_step = requested_step
    elif entry_import_plan is not None:
        active_step = 'entries'
    elif criteria_plan is not None or criteria_import_errors:
        active_step = 'criteria'
    elif request.method == 'POST':
        action = request.POST.get('action')
        if action == 'update_competition':
            active_step = 'details'
        elif action in ('track', 'remove_track'):
            active_step = 'tracks'
        elif action in ('entry', 'remove_entry'):
            active_step = 'entries'
        elif action in ('criterion', 'delete_criterion'):
            active_step = 'criteria'
        else:
            active_step = 'launch'
    else:
        active_step = 'details'
    if request.method == 'POST' and action in ('track', 'entry', 'criterion', 'first_round', 'update_competition'):
        posted_form = {'track': track_form, 'entry': entry_form, 'criterion': criterion_form,
                       'first_round': first_round_form, 'update_competition': details_form}.get(action)
        if posted_form is not None and posted_form.errors:
            active_step = {'track': 'tracks', 'entry': 'entries', 'criterion': 'criteria',
                           'first_round': 'launch', 'update_competition': 'details'}[action]
    response = render(request, 'service_domains/competition_manage.html', {
        'competition': competition, 'details_form': details_form,
        'back_url': back_url, 'back_label': back_label, 'from_param': from_param,
        'tracks': tracks, 'track_form': track_form,
        'entry_form': entry_form,
        'entry_import_form': SpreadsheetImportForm(auto_id='entry_import_%s'),
        'entry_import_plan': entry_import_plan, 'entry_import_token': entry_import_token,
        'entry_import_errors': entry_import_errors,
        'criteria_import_form': SpreadsheetImportForm(auto_id='criteria_import_%s'),
        'criteria_import_plan': criteria_plan, 'criteria_import_token': criteria_token,
        'criteria_import_errors': criteria_import_errors,
        'first_round_form': first_round_form,
        'criterion_form': criterion_form,
        'rounds': rounds, 'latest_round': latest_round, 'action_error': action_error,
        'judge_form': judge_form, 'judge_notice': judge_notice,
        'manual_invitation_url': manual_invitation_url, 'tally_results': tally_results,
        'audience_tally': audience_tally,
        'advance_round_form': advance_round_form,
        'can_advance_round': can_advance_round,
        'can_finalize_weighted': can_finalize_weighted,
        'audience_weighted_round': competition.effective_audience_weight > 0,
        'audience_scores_locked': bool(latest_round and latest_round.audience_locked_at),
        'final_tie_groups': final_tie_groups,
        'final_tie_form': final_tie_form,
        'final_tie_sections': final_tie_sections,
        'recorded_final_tie_decisions': recorded_final_tie_decisions,
        'track_count': len(tracks), 'entry_count': entry_count,
        'criterion_count': criterion_count, 'round_count': round_count,
        'setup_steps': setup_steps,
        'stepper_steps': steps_from_setup(setup_steps, active_step),
        'active_step': active_step,
        'setup_ready': all(step['complete'] for step in setup_steps[:-1]),
    }, status=400 if action_error or details_form.errors or track_form.errors or entry_form.errors
        or first_round_form.errors or criterion_form.errors or (judge_form is not None and judge_form.errors)
        or (advance_round_form is not None and advance_round_form.errors)
        or (final_tie_form is not None and final_tie_form.errors) else 200)
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
@_competition_owner
def competition_entry_import(request, competition_id):
    """Preview, apply or cancel an entries spreadsheet; returns to the Entries step."""
    competition = _owned_competition(request, competition_id)
    manage_url = reverse('competition_manage', args=(competition.pk,))
    if request.method == 'GET':
        return redirect(f'{manage_url}?step=entries')
    return embedded_import.handle_post(
        request, kind='competition-entry', owner=competition, form_class=SpreadsheetImportForm,
        parse=parse_competition_entries, plan=plan_entries, apply=apply_competition_entry_import,
        success='Entries imported.', back=lambda: redirect(f'{manage_url}?step=entries'))


@require_safe
@never_cache
@_competition_owner
def competition_entry_import_sample(request, competition_id):
    _owned_competition(request, competition_id)
    response = HttpResponse(
        sample_competition_entries().getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename="competition-entries-sample.xlsx"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
def competition_audience_join(request):
    """Resolve a short public code to the current round's anonymous ballot."""
    if getattr(request, 'service', None) != 'competition':
        raise Http404
    form = CompetitionAudienceJoinForm(request.POST or None)
    if request.method == 'POST':
        # Limit every attempt before validation or database routing work.
        allowed = allow_public_event_request(request, service='competition-join-code',
            event_id='join', limit=settings.COMPETITION_JOIN_RATE_LIMIT,
            window=settings.PUBLIC_BEARER_UNKNOWN_RATE_WINDOW)
        if allowed is None:
            return render(request, 'service_domains/competition_join.html', {
                'form': form, 'error': 'Audience joining is temporarily unavailable.',
            }, status=503)
        if not allowed:
            response = render(request, 'service_domains/competition_join.html', {
                'form': form, 'error': 'Too many attempts. Try again shortly.',
            }, status=429)
            response['Retry-After'] = str(settings.PUBLIC_BEARER_UNKNOWN_RATE_WINDOW)
            return response
        if not form.is_valid():
            return render(request, 'service_domains/competition_join.html', {
                'form': form, 'error': '',
            }, status=400)
        from .models import CompetitionAudienceJoinCode
        routing = (CompetitionAudienceJoinCode.objects.select_related(
            'round__competition', 'track').filter(code=form.cleaned_data['code']).first())
        if routing:
            round_obj, track = routing.round, routing.track
            active = round_obj.competition
            latest_id = active.rounds.order_by('-order').values_list('pk', flat=True).first()
            if (active.status == Competition.Status.ACTIVE
                    and round_obj.pk == latest_id and round_obj.audience_locked_at is None
                    and track.competition_id == active.pk):
                minimum = 1 if active.effective_audience_weight > 0 else 2
                if CompetitionRoundEntry.objects.filter(round=round_obj, track=track).count() >= minimum:
                    return redirect('competition_audience_vote', active.public_id, round_obj.pk, track.pk)
        return render(request, 'service_domains/competition_join.html', {
            'form': form, 'error': 'That code is unavailable or voting has closed.',
        }, status=400)
    return render(request, 'service_domains/competition_join.html', {
        'form': form, 'error': '',
    })


@require_safe
def competition_public_leaderboard(request, competition_id):
    """Serve results only after the owner explicitly reveals a frozen leaderboard."""
    if getattr(request, 'service', None) != 'competition':
        raise Http404
    if request.headers.get('Accept') == 'application/json':
        request_json = True
    else:
        request_json = False
    competition = Competition.objects.filter(
        public_id=competition_id, status=Competition.Status.REVEALED,
        public_leaderboard_enabled=True).first()
    if competition is None or competition.revealed_at is None:
        raise Http404
    latest = CompetitionRound.objects.filter(competition=competition).order_by('-order').first()
    if latest is None:
        raise Http404
    cache_key = (f'livegrade:competition:leaderboard:v3:{competition.public_id.hex}:'
                 f'{latest.pk}:{int(competition.revealed_at.timestamp())}')
    try:
        projection = cache.get(cache_key)
    except Exception:
        projection = None
    if projection is None:
        rows = list(CompetitionRoundEntry.objects.filter(round=latest)
            # One judge now has one score row per criterion and entry. Count
            # invitations, not score rows, so multi-criterion rubrics do not
            # inflate the public judge count.
            .annotate(judge_count=Count('judge_scores__invitation', distinct=True))
            .select_related('entry', 'track').order_by(
                'track__name', '-score', 'entry__order', 'entry_id'))
        if not rows or any(row.score is None for row in rows):
            raise Http404
        tie_groups = {}
        for row in rows:
            tie_groups.setdefault((row.track_id, row.score), []).append(row)
        tie_groups = {key: entries for key, entries in tie_groups.items() if len(entries) > 1}
        decisions = {(decision.track_id, decision.tied_score): decision.ordered_entry_ids
            for decision in CompetitionLeaderboardTieDecision.objects.filter(round=latest)}
        if any(key not in decisions for key in tie_groups):
            raise Http404
        grouped = []
        audience_counts = dict(CompetitionAudienceBallot.objects.filter(
            round=latest).values('track_id').annotate(total=Count('pk'))
            .values_list('track_id', 'total'))
        for track in sorted({row.track_id: row.track for row in rows}.values(),
                            key=lambda item: item.name.casefold()):
            track_rows = [row for row in rows if row.track_id == track.pk]
            track_rows.sort(key=lambda row: (-row.score,
                decisions.get((row.track_id, row.score), []).index(row.entry_id)
                    if (row.track_id, row.score) in decisions else row.entry.order,
                row.entry.order, row.entry_id))
            grouped.append({
                'track': {'pk': track.pk, 'name': track.name},
                'entries': [
                    {'display_rank': position, 'entry': {'name': row.entry.name},
                     'score': row.score, 'judge_score': row.judge_score,
                     'audience_score': row.audience_score,
                     'tie_break': (row.track_id, row.score) in decisions,
                     'judge_count': row.judge_count,
                     'score_breakdown': row.score_breakdown,
                     'audience_ballot_count': audience_counts.get(row.track_id, 0)}
                    for position, row in enumerate(track_rows, start=1)
                ],
            })
        projection = {'track_results': grouped,
            'has_head_judge_tie_decisions': bool(tie_groups),
            'audience_weight': str(competition.effective_audience_weight),
            'judge_weight': str(competition.judge_weight)}
        try:
            cache.set(cache_key, projection, COMPETITION_LEADERBOARD_CACHE_SECONDS)
        except Exception:
            pass
    if request_json:
        response = JsonResponse({
            'competition': competition.name,
            'round': latest.name,
            'revealed_at': competition.revealed_at.isoformat(),
            'track_results': projection['track_results'],
            'has_head_judge_tie_decisions': projection['has_head_judge_tie_decisions'],
            'audience_weight': projection['audience_weight'],
            'judge_weight': projection['judge_weight'],
        })
        response['Cache-Control'] = 'private, no-store'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response
    response = render(request, 'service_domains/competition_public_leaderboard.html', {
        'competition': competition, 'round': latest,
        'track_results': projection['track_results'],
        'has_head_judge_tie_decisions': projection['has_head_judge_tie_decisions'],
        'audience_weight': competition.effective_audience_weight,
        'judge_weight': competition.judge_weight,
    })
    response['Cache-Control'] = 'private, no-store'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@require_safe
@never_cache
@_competition_owner
def competition_results_export(request, competition_id):
    """Owner-only handoff of frozen aggregate results; never export judge identities."""
    import csv
    from django.http import HttpResponse

    competition = _owned_competition(request, competition_id)
    if competition.status not in (Competition.Status.COMPLETE, Competition.Status.REVEALED):
        raise Http404
    rows = (CompetitionRoundEntry.objects.for_organization(request.organization).owned_by(request.user)
        .filter(round__competition=competition).select_related('round', 'track', 'entry')
        .order_by('round__order', 'track__name', '-score', 'entry__order'))
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="competition-{competition.pk}-results.csv"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    writer = csv.writer(response)
    writer.writerow(['Event', 'Round', 'Track', 'Entry', 'Reference', 'Judge score', 'Audience score', 'Final score'])
    def safe_text(value):
        value = str(value or '')
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value
    for row in rows.iterator(chunk_size=500):
        writer.writerow([safe_text(competition.name), safe_text(row.round.name), safe_text(row.track.name),
            safe_text(row.entry.name), safe_text(row.entry.reference), row.judge_score,
            row.audience_score, row.score])
    return response


def _competition_readiness(request, competition):
    """Checklist rows for a draft competition, and whether it can start."""
    tracks = list(CompetitionTrack.objects.for_organization(request.organization)
        .owned_by(request.user).filter(competition=competition).order_by('pk'))
    entry_counts = {track.pk: CompetitionEntry.objects.filter(track=track).count() for track in tracks}
    criteria_totals = {track.pk: sum((item.weight for item in CompetitionCriterion.objects.filter(
        track=track)), Decimal('0')) for track in tracks}
    tracks_ok = bool(tracks)
    entries_ok = tracks_ok and all(entry_counts[track.pk] > 0 for track in tracks)
    criteria_ok = all(total in (Decimal('0'), Decimal('100')) for total in criteria_totals.values())
    readiness = [
        {'label': 'Tracks', 'step': 'tracks', 'ok': tracks_ok,
         'detail': f'{len(tracks)} track{"s" if len(tracks) != 1 else ""}' if tracks else 'Add at least one track'},
        {'label': 'Entries', 'step': 'entries', 'ok': entries_ok,
         'detail': (f'{sum(entry_counts.values())} entries' if entries_ok
                    else 'Every track needs at least one entry')},
        {'label': 'Judging criteria', 'step': 'criteria', 'ok': criteria_ok,
         'detail': ('Ready (tracks without criteria use one overall score)' if criteria_ok
                    else 'Each track\'s criteria must total exactly 100%')},
    ]
    return readiness, tracks_ok and entries_ok and criteria_ok


@require_safe
@never_cache
@_competition_owner
def competition_live_control(request, competition_id):
    """Render the arena stage pitch runner and live round command console."""
    competition = _owned_competition(request, competition_id)
    if competition.status == Competition.Status.ACTIVE:
        context = get_competition_live_state_context(competition)
        context['competition_status'] = 'active'
    elif competition.status == Competition.Status.DRAFT:
        readiness, can_start = _competition_readiness(request, competition)
        context = {'competition': competition, 'competition_status': 'draft',
                   'readiness': readiness, 'can_start': can_start}
    else:
        context = {'competition': competition, 'competition_status': 'closed'}
    response = render(request, 'service_domains/competition_live_control.html', context)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    return response


@require_safe
@never_cache
@_competition_owner
def competition_projector(request, competition_id):
    """Render the full-screen arena presentation display supporting stage pitch, QR voting, standings, and winner modes."""
    competition = _owned_competition(request, competition_id)
    if competition.status == Competition.Status.DRAFT:
        return redirect('competition_manage', competition_id=competition.pk)
    context = get_competition_live_state_context(competition)
    response = render(request, 'service_domains/competition_projector.html', context)
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    return response


@require_safe
@never_cache
@_competition_owner
def competition_projector_qr(request, competition_id):
    """Generate dynamic PNG QR code for audience ballot voting or join page."""
    competition = _owned_competition(request, competition_id)
    context = get_competition_live_state_context(competition)
    active_round = context.get('active_round')
    active_track = context.get('active_track')
    if active_round and active_track:
        target_url = request.build_absolute_uri(
            reverse('competition_audience_vote', kwargs={
                'competition_id': competition.public_id,
                'round_id': active_round.pk,
                'track_id': active_track.pk,
            })
        )
    else:
        target_url = request.build_absolute_uri(reverse('competition_audience_join'))
    response = HttpResponse(qr_png(target_url), content_type='image/png')
    response['Cache-Control'] = 'private, no-store'
    return response


@require_safe
@never_cache
@_competition_owner
def competition_live_state(request, competition_id):
    """Real-time JSON state endpoint returning arena telemetry and HTML fragments."""
    competition = _owned_competition(request, competition_id)
    if competition.status != Competition.Status.ACTIVE:
        return JsonResponse({
            'ok': False,
            'status': competition.status,
            'redirect': reverse('competition_manage', args=(competition.pk,))
        })
    context = get_competition_live_state_context(competition)
    return JsonResponse({
        'ok': True,
        'competition_status': competition.status,
        'active_round_id': context['active_round'].pk if context['active_round'] else None,
        'active_track_id': context['active_track'].pk if context['active_track'] else None,
        'active_entry_id': context['active_entry'].pk if context['active_entry'] else None,
        'pitch_seconds_remaining': context['pitch_seconds_remaining'],
        'timer_paused': context['timer_paused'],
        'audience_voting_open': context['audience_voting_open'],
        'ballots_count': context['ballots_count'],
        'has_boundary_tie': context['has_boundary_tie'],
        'unresolved_ties': context['unresolved_ties'],
        'can_advance': context['can_advance'],
        'projector_mode': context['projector_mode'],
        'hero_html': render_to_string('service_domains/_partials/competition_live_hero.html', context, request=request),
        'judges_html': render_to_string('service_domains/_partials/competition_live_judges.html', context, request=request),
        'audience_html': render_to_string('service_domains/_partials/competition_live_audience.html', context, request=request),
        'standings_html': render_to_string('service_domains/_partials/competition_live_standings.html', context, request=request),
        'projector_html': render_to_string('service_domains/_partials/competition_live_projector.html', context, request=request),
    })


@require_POST
@never_cache
@_competition_owner
def competition_live_action(request, competition_id):
    """Execute atomic live arena stage, timer, judge nudge, and round advancement actions."""
    competition = _owned_competition(request, competition_id)
    action = request.POST.get('action')
    starting = (action == 'start_competition' and competition.status == Competition.Status.DRAFT)
    if competition.status != Competition.Status.ACTIVE and not starting:
        return JsonResponse({'error': 'Competition is not active.'}, status=400)
    is_ajax = (request.headers.get('x-requested-with') == 'XMLHttpRequest' or
               request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest')
    try:
        if action == 'start_competition':
            if not CompetitionRound.objects.filter(competition=competition).exists():
                create_initial_competition_round(competition, 'Round 1',
                    organization=request.organization, user=request.user)
            start_competition(competition, organization=request.organization, user=request.user)
            messages.success(request, 'The competition has started. Scoring is open.')
            return JsonResponse({'status': 'active',
                'redirect': reverse('competition_live_control', args=(competition.pk,))})
        elif action == 'set_active_entry':
            entry_id = request.POST.get('entry_id')
            set_competition_stage_entry(competition, entry_id, organization=request.organization, user=request.user)
        elif action == 'adjust_pitch_timer':
            delta = int(request.POST.get('delta', 30))
            adjust_competition_pitch_timer(competition, delta, organization=request.organization, user=request.user)
        elif action == 'toggle_pitch_pause':
            toggle_competition_pitch_pause(competition, organization=request.organization, user=request.user)
        elif action == 'toggle_audience_voting':
            toggle_competition_audience_voting(competition, organization=request.organization, user=request.user)
        elif action == 'nudge_judge':
            judge_id = request.POST.get('judge_id')
            invitation = CompetitionJudgeInvitation.objects.filter(
                pk=judge_id, competition=competition, organization_id=competition.organization_id
            ).first()
            if invitation and not invitation.submitted_at and not invitation.revoked_at:
                messages.info(request, f'Nudge sent to judge {invitation.email}.')
        elif action == 'resolve_tie':
            track_id = int(request.POST.get('track_id'))
            tied_score = Decimal(request.POST.get('tied_score'))
            reason = request.POST.get('reason', '').strip()
            ordered_ids = [int(eid) for eid in request.POST.getlist('ordered_entry_ids')]
            active_round = CompetitionRound.objects.filter(competition=competition, organization_id=competition.organization_id).order_by('-order').first()
            if active_round and reason and len(ordered_ids) >= 2:
                CompetitionLeaderboardTieDecision.objects.create(
                    organization_id=competition.organization_id,
                    created_by=request.user,
                    round=active_round,
                    track_id=track_id,
                    tied_score=tied_score,
                    ordered_entry_ids=ordered_ids,
                    reason=reason,
                    decided_by=request.user,
                )
            messages.success(request, 'Boundary tie resolved.')
            if not is_ajax:
                return redirect('competition_live_control', competition_id=competition.pk)
        elif action == 'advance_round':
            active_round = CompetitionRound.objects.filter(competition=competition, organization_id=competition.organization_id).order_by('-order').first()
            next_round_name = request.POST.get('next_round_name') or f'Round {active_round.order + 1}'
            create_next_competition_round(competition, active_round, next_round_name, organization=request.organization, user=request.user)
            messages.success(request, f'Advanced to {next_round_name}. Qualified entrants are ready on the stage docket.')
            if not is_ajax:
                return redirect('competition_live_control', competition_id=competition.pk)
        elif action == 'conclude_competition':
            complete_competition(competition, organization=request.organization, user=request.user)
            messages.success(request, 'Competition completed. Final leaderboard is ready for review.')
            if not is_ajax:
                return redirect('competition_manage', competition_id=competition.pk)
            return JsonResponse({'status': 'complete', 'redirect': reverse('competition_manage', args=(competition.pk,))})
        elif action == 'set_projector_mode':
            mode = request.POST.get('mode', 'stage')
            set_competition_projector_mode(competition, mode, organization=request.organization, user=request.user)
        else:
            if not is_ajax:
                return redirect('competition_live_control', competition_id=competition.pk)
            return JsonResponse({'error': f'Unknown action: {action}'}, status=400)
    except (ValidationError, PermissionDenied) as exc:
        msg = '; '.join(getattr(exc, 'messages', [])) or str(exc)
        if not is_ajax:
            messages.error(request, msg)
            return redirect('competition_live_control', competition_id=competition.pk)
        return JsonResponse({'error': msg}, status=400)

    context = get_competition_live_state_context(competition)
    return JsonResponse({
        'ok': True,
        'competition_status': competition.status,
        'pitch_seconds_remaining': context['pitch_seconds_remaining'],
        'timer_paused': context['timer_paused'],
        'audience_voting_open': context['audience_voting_open'],
        'ballots_count': context['ballots_count'],
        'has_boundary_tie': context['has_boundary_tie'],
        'unresolved_ties': context['unresolved_ties'],
        'can_advance': context['can_advance'],
        'projector_mode': context['projector_mode'],
        'hero_html': render_to_string('service_domains/_partials/competition_live_hero.html', context, request=request),
        'judges_html': render_to_string('service_domains/_partials/competition_live_judges.html', context, request=request),
        'audience_html': render_to_string('service_domains/_partials/competition_live_audience.html', context, request=request),
        'standings_html': render_to_string('service_domains/_partials/competition_live_standings.html', context, request=request),
        'projector_html': render_to_string('service_domains/_partials/competition_live_projector.html', context, request=request),
    })


@require_POST
@never_cache
@_competition_owner
def competition_live_projector_toggle(request, competition_id):
    """Switch the public projector display mode."""
    competition = _owned_competition(request, competition_id)
    mode = request.POST.get('mode', 'stage')
    set_competition_projector_mode(competition, mode, organization=request.organization, user=request.user)
    context = get_competition_live_state_context(competition)
    return JsonResponse({'ok': True, 'projector_mode': context['projector_mode']})
