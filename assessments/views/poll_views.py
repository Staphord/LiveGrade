"""Owner controls and separate, roster-bound participant poll pages."""
import csv
import uuid

from assessments.poll_access import public_poll_budget

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from accounts.access import lecturer_required
from ..forms import PollQuestionImportForm
from ..models import ClassroomPoll, ClassroomPollVoter, SessionChange
from .. import poll_import
from ..polls import (PollForm, append_imported_questions, changed,
                    create_imported_draft, poll_results, save_draft,
                    save_response, transition)
from ..qr import public_url, qr_png
from ..realtime import broadcast_session_event
from ..spreadsheet_safety import spreadsheet_safe_text
from .lecturer_views import _session

POLL_IDENTITIES = 'classroom_poll_voters'
POLL_IMPORT_KEY = 'classroom_poll_question_import'


def _poll_json_requested(request):
    return (request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or 'application/json' in request.headers.get('Accept', '').lower())


def _poll(queryset, value):
    try:
        value = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise Http404
    return get_object_or_404(queryset, uuid=value)


def _private(response, *, bearer=False):
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer' if bearer else 'same-origin'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@lecturer_required
@require_POST
def leaderboard_scores(request, pk):
    action = request.POST.get('action')
    if action not in {'show', 'hide'}:
        raise Http404
    with transaction.atomic():
        session = _session(request, pk, lock=True)
        if session.service != 'classroom':
            raise Http404
        show = action == 'show'
        if session.public_leaderboard_show_scores != show:
            session.public_leaderboard_show_scores = show
            session.public_results_released_at = timezone.now() if show else None
            session.public_results_released_by = request.user if show else None
            session.save(update_fields=['public_leaderboard_show_scores',
                'public_results_released_at', 'public_results_released_by'])
            SessionChange.objects.create(session=session, by_user=request.user,
                summary='Public leaderboard scores shown.' if show else 'Public leaderboard scores hidden.')
            transaction.on_commit(lambda: broadcast_session_event(session, 'public.results.changed', {}))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'public_show_scores': show})
    return redirect('assessment_session_detail', pk=pk)


@never_cache
@lecturer_required
@require_http_methods(['GET', 'POST'])
def classroom_polls(request, pk):
    session = _session(request, pk)
    if session.service != 'classroom':
        raise Http404
    selected_uuid = request.POST.get('poll') if request.method == 'POST' else request.GET.get('poll')
    poll = _poll(session.polls.all(), selected_uuid) if selected_uuid else None
    stash = request.session.get(POLL_IMPORT_KEY)
    if stash and stash.get('session') != session.pk:
        stash = None
    question_import_form = PollQuestionImportForm(auto_id='id_poll_question_import_%s')
    form = PollForm(instance=poll if poll and poll.status == 'draft' else None)
    if request.method == 'POST':
        action = request.POST.get('action')
        error = None
        with transaction.atomic():
            # Serialize roster snapshot against session-level owner mutations.
            session = _session(request, pk, lock=True)
            if poll:
                poll = get_object_or_404(session.polls.select_for_update(), pk=poll.pk)
            try:
                if action == 'save':
                    if poll and poll.status != 'draft':
                        raise ValidationError('Published poll questions and options cannot be edited. Create a new poll instead.')
                    form = PollForm(request.POST, instance=poll)
                    if form.is_valid():
                        poll = save_draft(form, session, request.user)
                    else:
                        error = 'Please correct the poll below.'
                elif action == 'upload-questions':
                    upload_form = PollQuestionImportForm(request.POST, request.FILES)
                    question_import_form = upload_form
                    if upload_form.is_valid():
                        parsed = poll_import.parse_questions(upload_form.cleaned_data['file'])
                        if parsed['fatal']:
                            error = parsed['fatal']
                        else:
                            request.session[POLL_IMPORT_KEY] = {
                                'session': session.pk,
                                'target_poll': poll.pk if poll and poll.status == 'draft' else None,
                                'questions': parsed['questions'],
                                'errors': parsed['errors'],
                            }
                            stash = request.session[POLL_IMPORT_KEY]
                    else:
                        error = 'Choose a valid Excel (.xlsx) question file.'
                elif action == 'apply-question-import':
                    stash = request.session.get(POLL_IMPORT_KEY)
                    if not stash or stash.get('session') != session.pk:
                        error = 'The question preview expired. Upload the file again.'
                    elif stash.get('errors') or not stash.get('questions'):
                        error = 'Fix the listed file problems before importing questions.'
                    elif stash.get('target_poll') and (not poll or poll.pk != stash['target_poll']):
                        error = 'The draft selected for this import changed. Choose it again and upload the file.'
                    else:
                        imported_poll = (append_imported_questions(poll, stash['questions'], request.user)
                                         if poll else create_imported_draft(session, stash['questions'], request.user))
                        request.session.pop(POLL_IMPORT_KEY, None)
                elif action == 'cancel-question-import':
                    request.session.pop(POLL_IMPORT_KEY, None)
                elif action == 'rotate-link' and poll:
                    voter_id = request.POST.get('voter', '')
                    if not voter_id.isdigit():
                        raise Http404
                    voter = get_object_or_404(poll.voters, pk=voter_id)
                    voter.token = uuid.uuid4()
                    voter.save(update_fields=['token'])
                    changed(poll, request.user, 'Replaced a personal voting link.')
                elif poll:
                    option_id = request.POST.get('option')
                    if option_id and not option_id.isdigit():
                        raise ValidationError('Choose a valid option.')
                    question_id = request.POST.get('question')
                    if question_id and not question_id.isdigit():
                        raise ValidationError('Choose a valid question.')
                    transition(poll, action, request.user, option_id=option_id,
                        note=request.POST.get('decision_note', '').strip(), question_id=question_id)
                else:
                    raise ValidationError('Choose a poll first.')
            except ValidationError as exc:
                error = ' '.join(exc.messages)
        if action == 'upload-questions' and _poll_json_requested(request):
            preview_html = render_to_string('assessments/polls/_question_import_preview.html', {
                'poll': poll, 'pending_question_import': stash,
            }, request=request)
            return _private(JsonResponse({
                'ok': not bool(error), 'error': error or '', 'html': preview_html,
            }, status=400 if error else 200))
        if error:
            if _poll_json_requested(request) and action == 'save':
                return JsonResponse({'ok': False, 'error': error}, status=400)
            messages.error(request, error)
        elif action in {'upload-questions', 'cancel-question-import'}:
            url = reverse('assessment_polls', args=[pk])
            return redirect(url + ('?poll=' + str(poll.uuid) if poll else ''))
        elif action == 'apply-question-import' and stash and not stash.get('errors'):
            if stash.get('target_poll'):
                messages.success(request, f'Added {len(stash["questions"])} questions to the poll draft.')
            else:
                messages.success(request, f'Created one poll draft with {len(stash["questions"])} questions.')
            return redirect(reverse('assessment_polls', args=[pk]) + '?poll=' + str(imported_poll.uuid))
        else:
            if action == 'save' and _poll_json_requested(request):
                return JsonResponse({'ok': True, 'message': 'Poll saved.', 'uuid': str(poll.uuid)})
            messages.success(request, 'Poll saved.' if action == 'save' else 'Poll updated.')
            leave_to = request.POST.get('leave_to', '')
            if action == 'save' and leave_to and url_has_allowed_host_and_scheme(
                    leave_to, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                return redirect(leave_to)
            return redirect(reverse('assessment_polls', args=[pk]) + '?poll=' + str(poll.uuid))
    results = poll_results(poll) if poll else None
    polls = session.polls.annotate(
        question_count=Count('questions', distinct=True),
        invited_count=Count('voters', distinct=True),
        responded_count=Count('voters__response', distinct=True),
    )
    if poll and request.GET.get('state') == '1':
        return _private(JsonResponse({'status': poll.status, 'accepting': poll.accepting_responses,
            'html': render_to_string('assessments/polls/_results.html',
                {'poll': poll, **results, 'hide_option_bars': True, 'hide_option_percentages': True}, request=request),
            'responded_voters': list(poll.voters.filter(response__isnull=False).values_list('pk', flat=True))}))
    voters = list(poll.voters.select_related('response')) if poll else []
    for voter in voters:
        voter.link = public_url(request, reverse('classroom_poll_invitation', args=[voter.token]))
    if poll and request.GET.get('download') == '1':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="poll-invitations.csv"'
        writer = csv.writer(response)
        writer.writerow(['Name', 'Roster identifier', 'Personal voting link'])
        for voter in voters:
            writer.writerow([spreadsheet_safe_text(voter.name), spreadsheet_safe_text(voter.identifier), voter.link])
        return _private(response)
    return _private(render(request, 'assessments/polls/manage.html', {
        'session': session, 'polls': polls, 'poll': poll, 'form': form,
        'results': results, 'voters': voters,
        'pending_question_import': stash,
        'question_import_form': question_import_form,
        'poll_question_sample_url': reverse('assessment_poll_questions_sample', args=[session.pk]),
        'poll_url': public_url(request, reverse('classroom_poll', args=[poll.uuid])) if poll else '',
    }))


@lecturer_required
@require_POST
def classroom_poll_delete(request, pk):
    session = _session(request, pk)
    if session.service != 'classroom':
        raise Http404
    action = request.POST.get('action')
    ids = request.POST.getlist('polls')
    if action == 'delete' and request.POST.get('poll'):
        ids = [request.POST['poll']]
    try:
        poll_ids = [uuid.UUID(value) for value in ids]
    except (ValueError, TypeError, AttributeError):
        raise Http404
    with transaction.atomic():
        polls = session.polls.select_for_update().filter(uuid__in=poll_ids)
        count = polls.count()
        polls.delete()
    messages.success(request, f'{count} poll{"s" if count != 1 else ""} deleted.')
    return redirect(reverse('assessment_polls', args=[pk]))


@lecturer_required
@require_http_methods(['GET'])
def classroom_poll_import_sample(request, pk):
    session = _session(request, pk)
    if session.service != 'classroom':
        raise Http404
    response = HttpResponse(poll_import.sample_workbook().getvalue(), content_type=(
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))
    response['Content-Disposition'] = 'attachment; filename="poll-questions-sample.xlsx"'
    return _private(response)


def _public_poll(poll_uuid):
    return get_object_or_404(ClassroomPoll.objects.select_related('assessment_session', 'confirmed_option'),
        uuid=poll_uuid, assessment_session__service='classroom', status__in=['open', 'closed', 'confirmed', 'cancelled'])


@never_cache
@require_http_methods(['GET'])
@public_poll_budget
def public_poll_results(request, poll_uuid):
    """A read-only display page: aggregate counts only, never voter rows or ballots."""
    poll = _public_poll(poll_uuid)
    results = poll_results(poll)
    return _private(render(request, 'assessments/polls/public_results.html', {
        'poll': poll, **results, 'public_results': True,
        'state_url': reverse('classroom_poll', args=[poll.uuid]) + '?public_state=1',
    }))


@never_cache
@require_http_methods(['GET'])
@public_poll_budget
def poll_qr_screen(request, poll_uuid):
    """Public, projector-friendly entry point for a classroom poll."""
    poll = _public_poll(poll_uuid)
    return _private(render(request, 'assessments/polls/qr_screen.html', {
        'poll': poll,
        'qr_image_url': reverse('classroom_poll_qr', args=[poll.uuid]),
        'voting_url': reverse('classroom_poll', args=[poll.uuid]),
        'results_url': reverse('classroom_poll_results', args=[poll.uuid]),
        'state_url': reverse('classroom_poll', args=[poll.uuid]) + '?public_state=1',
        'qr_available': poll.accepting_responses and not poll.personal_links_only,
    }))


@never_cache
@require_http_methods(['GET'])
def poll_invitation(request, token):
    voter = get_object_or_404(ClassroomPollVoter.objects.select_related('poll'), token=token,
        student__isnull=False, poll__assessment_session__service='classroom',
        poll__status__in=['open', 'closed', 'confirmed', 'cancelled'])
    identities = request.session.get(POLL_IDENTITIES, {})
    identities[str(voter.poll.uuid)] = str(voter.token)
    request.session[POLL_IDENTITIES] = identities
    return _private(redirect('classroom_poll', poll_uuid=voter.poll.uuid), bearer=True)


def _may_identify(request, poll):
    # A classroom often shares one public IP. Limit repeated attempts for an
    # identifier rather than blocking the whole class after 30 valid joins.
    identity = request.POST.get('identifier', '').strip().casefold()
    digest = salted_hmac('classroom-poll-identification',
        request.META.get('REMOTE_ADDR', '') + ':' + identity).hexdigest()
    key = f'poll-identify:{poll.pk}:{digest}'
    if cache.add(key, 1, timeout=300):
        return True
    try:
        return cache.incr(key) <= 30
    except ValueError:
        return cache.add(key, 1, timeout=300)


@never_cache
@require_http_methods(['GET', 'POST'])
@public_poll_budget
def public_poll(request, poll_uuid):
    poll = _public_poll(poll_uuid)
    token = request.session.get(POLL_IDENTITIES, {}).get(str(poll.uuid))
    try:
        token = uuid.UUID(str(token))
    except (ValueError, TypeError, AttributeError):
        token = None
    voter = poll.voters.filter(token=token, student__isnull=False).first() if token else None
    error, saved = None, request.GET.get('saved') == '1'
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'identify' and not poll.personal_links_only:
            if not _may_identify(request, poll):
                return _private(HttpResponse('Too many attempts. Try again in five minutes.', status=429))
            identifier = request.POST.get('identifier', '').strip()
            matches = list(poll.voters.filter(identifier__iexact=identifier, student__isnull=False)[:2]) if identifier else []
            if len(matches) == 1:
                identities = request.session.get(POLL_IDENTITIES, {})
                identities[str(poll.uuid)] = str(matches[0].token)
                request.session[POLL_IDENTITIES] = identities
                return _private(redirect('classroom_poll', poll_uuid=poll.uuid))
            error = 'We could not identify you uniquely. Check your roster name or ID, or ask the teacher for your personal voting link.'
        elif action == 'vote' and voter:
            try:
                question_answers = {}
                vote_questions = list(poll.questions.all())
                legacy_choices = request.POST.getlist('options')
                for index, question in enumerate(vote_questions):
                    key = str(question.pk)
                    selected = request.POST.getlist(f'question-{key}')
                    abstained = request.POST.get(f'abstain-{key}') == 'yes'
                    if len(vote_questions) == 1 and not selected and not abstained:
                        selected = legacy_choices
                        abstained = request.POST.get('abstain') == 'yes'
                    question_answers[key] = {'options': selected, 'abstained': abstained}
                save_response(poll.pk, voter.pk, [], token=token,
                              question_answers=question_answers)
                return _private(redirect(reverse('classroom_poll', args=[poll.uuid]) + '?saved=1'))
            except ValidationError as exc:
                error = ' '.join(exc.messages)
        elif action == 'forget':
            identities = request.session.get(POLL_IDENTITIES, {})
            identities.pop(str(poll.uuid), None)
            request.session[POLL_IDENTITIES] = identities
            return _private(redirect('classroom_poll', poll_uuid=poll.uuid))
        else:
            error = 'Open your voting link or identify yourself before responding.'
    results = poll_results(poll)
    if request.GET.get('public_state') == '1':
        return _private(JsonResponse({'status': poll.status, 'accepting': poll.accepting_responses,
            'html': render_to_string('assessments/polls/_results.html',
                {'poll': poll, **results, 'public_results': True,
                 'hide_option_bars': True, 'hide_option_percentages': True}, request=request)}))
    if request.GET.get('state') == '1':
        if not voter:
            return _private(JsonResponse({'error': 'Identify yourself first.'}, status=403))
        return _private(JsonResponse({'status': poll.status, 'accepting': poll.accepting_responses,
            'html': render_to_string('assessments/polls/_results.html',
                {'poll': poll, **results, 'public_results': True,
                 'hide_option_bars': True, 'hide_option_percentages': True}, request=request)}))
    response = getattr(voter, 'response', None) if voter else None
    questions = list(poll.questions.prefetch_related('options').all())
    saved_answers = response.answers if response else {}
    for question in questions:
        key = str(question.pk)
        answer = saved_answers.get(key, [])
        question.user_selections = set(answer or [])
        question.user_abstained = key in saved_answers and answer is None
        if key in saved_answers:
            question.user_answer_display = ('None of these options' if answer is None else ', '.join(
                option.label for option in question.options.all() if option.pk in answer))
        if error and request.POST.get('action') == 'vote':
            posted = request.POST.getlist(f'question-{key}')
            question.user_selections = {int(value) for value in posted if value.isdigit()}
            question.user_abstained = request.POST.get(f'abstain-{key}') == 'yes'
    return _private(render(request, 'assessments/polls/respond.html', {
        'session': poll.assessment_session, 'poll': poll, 'voter': voter,
        'questions': questions, 'results': results, 'public_results': True,
        'results_url': reverse('classroom_poll_results', args=[poll.uuid]),
        'error': error, 'saved': saved, 'response': response,
    }))


@never_cache
@require_http_methods(['GET'])
@public_poll_budget
def poll_qr(request, poll_uuid):
    poll = _public_poll(poll_uuid)
    return _private(HttpResponse(qr_png(public_url(request, reverse('classroom_poll', args=[poll.uuid]))), content_type='image/png'))
