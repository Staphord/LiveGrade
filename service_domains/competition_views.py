"""Owner-scoped Competition organizer setup; results remain behind policy gates."""

from functools import wraps
import hmac

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django.views.decorators.http import require_safe

from accounts.access import lecturer_required

from .competition_forms import (CompetitionEntryForm, CompetitionFirstRoundForm,
    CompetitionAdvanceRoundForm, CompetitionAudienceJoinForm, CompetitionFinalTieForm, CompetitionForm,
    CompetitionCriterionForm, CompetitionJudgeInvitationForm,
    CompetitionTrackForm, competition_head_judge_requirements)
from .models import (Competition, CompetitionAudienceBallot, CompetitionCriterion, CompetitionEntry, CompetitionJudgeInvitation,
    CompetitionJudgeScore, CompetitionRound, CompetitionRoundEntry, CompetitionTrack)
from .operations import (create_initial_competition_round,
    compile_competition_judge_scores, issue_competition_judge_invitation,
    create_next_competition_round, revoke_competition_judge_invitation,
    start_competition, complete_competition, reveal_competition_leaderboard)
from .operations import set_competition_public_leaderboard
from .operations import (competition_final_tie_requirements,
    resolve_competition_final_ties, competition_audience_join_code,
    competition_audience_ranked_choice, finalize_competition_weighted_scores)
from .models import CompetitionLeaderboardTieDecision
from .throttling import allow_unknown_bearer_request
from .onboarding import service_onboarding


COMPETITION_LEADERBOARD_CACHE_SECONDS = 300


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


@require_http_methods(['GET', 'POST'])
@never_cache
@_competition_owner
def competition_list(request):
    competitions = (Competition.objects.for_organization(request.organization)
                    .owned_by(request.user)
                    .annotate(track_count=Count('tracks', distinct=True),
                              round_count=Count('rounds', distinct=True))
                    .order_by('-created_at', 'pk'))
    latest_competition = competitions.first()
    form = CompetitionForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        competition = form.save(commit=False)
        competition.organization_id = request.organization['id']
        competition.created_by = request.user
        try:
            competition.full_clean()
        except ValidationError as exc:
            form.add_error(None, '; '.join(exc.messages))
        else:
            competition.save(force_insert=True)
            return redirect('competition_manage', competition_id=competition.pk)
    return render(request, 'service_domains/competition_list.html', {
        'competitions': competitions, 'form': form,
        'onboarding': service_onboarding('competition',
            resume_url=(reverse('competition_manage', args=(latest_competition.pk,))
                        if latest_competition else ''),
            resume_name=(latest_competition.name if latest_competition else '')),
    })


@require_http_methods(['GET', 'POST'])
@never_cache
@_competition_owner
def competition_manage(request, competition_id):
    competition = _owned_competition(request, competition_id)
    track_form = CompetitionTrackForm()
    first_round_form = CompetitionFirstRoundForm()
    criterion_form = CompetitionCriterionForm(organization=request.organization,
        user=request.user, competition=competition)
    entry_form = CompetitionEntryForm(competition=competition,
        organization=request.organization, user=request.user)
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
                    and action not in ('invite_judge', 'tally_judges', 'revoke_judge',
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
        elif has_rounds and action in ('track', 'entry', 'criterion', 'delete_criterion'):
            action_error = 'Track, entry and scoring-criterion setup is locked after the initial round is created.'
        elif action == 'criterion':
            criterion = CompetitionCriterion(organization_id=competition.organization_id,
                created_by=request.user)
            criterion_form = CompetitionCriterionForm(request.POST,
                instance=criterion, organization=request.organization, user=request.user,
                competition=competition)
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
                        return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'delete_criterion':
            criterion = (CompetitionCriterion.objects.for_organization(request.organization)
                .owned_by(request.user).filter(pk=request.POST.get('criterion_id'),
                    track__competition=competition).first())
            if criterion is None:
                raise Http404
            criterion.delete()
            return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'track':
            track_form = CompetitionTrackForm(request.POST)
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
                        return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'entry':
            entry = CompetitionEntry(organization_id=competition.organization_id,
                created_by=request.user)
            entry_form = CompetitionEntryForm(request.POST, instance=entry,
                competition=competition, organization=request.organization, user=request.user)
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
                        return redirect('competition_manage', competition_id=competition.pk)
        elif action == 'first_round':
            first_round_form = CompetitionFirstRoundForm(request.POST)
            if first_round_form.is_valid():
                try:
                    create_initial_competition_round(competition, first_round_form.cleaned_data['name'],
                        organization=request.organization, user=request.user)
                except ValidationError as exc:
                    first_round_form.add_error(None, '; '.join(exc.messages))
                else:
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
    entry_count = sum(len(track.organizer_entries) for track in tracks)
    criterion_count = sum(len(track.organizer_criteria) for track in tracks)
    round_count = len(rounds)
    response = render(request, 'service_domains/competition_manage.html', {
        'competition': competition, 'tracks': tracks, 'track_form': track_form,
        'entry_form': entry_form, 'first_round_form': first_round_form,
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
    }, status=400 if action_error or track_form.errors or entry_form.errors
        or first_round_form.errors or criterion_form.errors or (judge_form is not None and judge_form.errors)
        or (advance_round_form is not None and advance_round_form.errors)
        or (final_tie_form is not None and final_tie_form.errors) else 200)
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET', 'POST'])
@never_cache
def competition_audience_join(request):
    """Resolve a short public code to the current round's anonymous ballot."""
    if getattr(request, 'service', None) != 'competition':
        raise Http404
    form = CompetitionAudienceJoinForm(request.POST or None)
    if request.method == 'POST':
        if not form.is_valid():
            allowed = allow_unknown_bearer_request(request, service='competition-join-code')
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
            return render(request, 'service_domains/competition_join.html', {
                'form': form, 'error': '',
            }, status=400)
        code = form.cleaned_data['code']
        matches = []
        active_competitions = Competition.objects.filter(status=Competition.Status.ACTIVE)
        for active in active_competitions.iterator():
            latest = (CompetitionRound.objects.filter(competition=active)
                .order_by('-order').first())
            if latest is None or latest.audience_locked_at is not None:
                continue
            for track in CompetitionTrack.objects.filter(competition=active).order_by('pk'):
                expected = competition_audience_join_code(active, latest, track)
                if hmac.compare_digest(expected, code):
                    entry_count = CompetitionRoundEntry.objects.filter(
                        round=latest, track=track).count()
                    minimum_entries = (1 if active.effective_audience_weight > 0 else 2)
                    if entry_count >= minimum_entries:
                        matches.append((active, latest, track))
        if len(matches) == 1:
            active, latest, track = matches[0]
            return redirect('competition_audience_vote', active.public_id, latest.pk, track.pk)
        allowed = allow_unknown_bearer_request(request, service='competition-join-code')
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
    cache_key = (f'livegrade:competition:leaderboard:v2:{competition.public_id.hex}:'
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
