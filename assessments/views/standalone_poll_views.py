"""Owner workspace and identity-free public ballots for standalone polls."""
import uuid

from assessments.poll_access import public_poll_budget

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from accounts.access import lecturer_required
from assessments import poll_import
from assessments.forms import PollQuestionImportForm
from assessments.models import (AnonymousClassroomPollResponse, ClassroomPoll,
                                ClassroomPollQuestion)
from assessments.polls import (PollForm, changed, create_imported_standalone_draft,
                               create_standalone_draft, poll_results,
                               save_anonymous_response, transition)
from assessments.qr import public_url, qr_png

TOKEN_KEY = 'standalone_poll_tokens'
IMPORT_KEY = 'standalone_poll_import'


def _poll_json_requested(request):
    return (request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or 'application/json' in request.headers.get('Accept', '').lower())


def _private(response):
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def _owner(view):
    from functools import wraps
    @wraps(view)
    @lecturer_required
    def scoped(request, *args, **kwargs):
        if getattr(request, 'service', None) != 'polls':
            raise Http404
        return view(request, *args, **kwargs)
    return scoped


def _poll_service(view):
    from functools import wraps
    @wraps(view)
    def scoped(request, *args, **kwargs):
        if getattr(request, 'service', None) != 'polls':
            raise Http404
        return view(request, *args, **kwargs)
    return scoped


def _owned(request, value, *, lock=False):
    try:
        poll_uuid = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise Http404
    polls = ClassroomPoll.objects.filter(
        standalone=True, organization_id=request.organization['id'], created_by=request.user)
    if lock:
        polls = polls.select_for_update()
    return get_object_or_404(polls, uuid=poll_uuid)


@never_cache
@require_http_methods(['GET', 'POST'])
@_owner
def standalone_poll_list(request):
    polls = ClassroomPoll.objects.filter(standalone=True, organization_id=request.organization['id'],
        created_by=request.user).annotate(response_count=Count('anonymous_responses', distinct=True),
        question_count=Count('questions', distinct=True))
    selected = request.POST.get('poll') if request.method == 'POST' else request.GET.get('poll')
    poll = _owned(request, selected) if selected else None
    form = PollForm(instance=poll if poll and poll.status == 'draft' else None)
    form.fields.pop('personal_links_only', None)
    stash = request.session.get(IMPORT_KEY)
    if stash and stash.get('poll_owner') != request.user.pk:
        stash = None
    question_import_form = PollQuestionImportForm(auto_id='id_poll_question_import_%s')
    if request.method == 'POST':
        action = request.POST.get('action')
        error = ''
        with transaction.atomic():
            if poll:
                poll = _owned(request, selected, lock=True)
            try:
                if action == 'save':
                    if poll and poll.status != 'draft':
                        raise ValidationError('Published poll questions cannot be edited. Create a new poll instead.')
                    form = PollForm(request.POST, instance=poll)
                    form.fields.pop('personal_links_only', None)
                    if form.is_valid():
                        if poll:
                            poll = form.save(commit=False)
                            poll.deadline = form.cleaned_data['deadline']
                            poll.personal_links_only = False
                            poll.save(update_fields=['question', 'description', 'allow_multiple',
                                'personal_links_only', 'allow_abstain', 'timezone_name', 'deadline'])
                            poll.questions.all().delete()
                            from assessments.polls import _write_questions
                            _write_questions(poll, [{'prompt': poll.question,
                                'allow_multiple': poll.allow_multiple,
                                'allow_abstain': form.cleaned_data['allow_abstain'],
                                'options': form.cleaned_data['option_lines']},
                                *form.cleaned_data.get('extra_questions_json', [])])
                            changed(poll, request.user, 'Updated standalone draft.')
                        else:
                            poll = create_standalone_draft(form, request.organization, request.user)
                    else:
                        error = 'Please correct the poll below.'
                elif action == 'upload-questions':
                    upload = PollQuestionImportForm(request.POST, request.FILES)
                    question_import_form = upload
                    if upload.is_valid():
                        parsed = poll_import.parse_questions(upload.cleaned_data['file'])
                        if parsed['fatal']:
                            error = parsed['fatal']
                        else:
                            stash = {'poll_owner': request.user.pk,
                                'target_poll': poll.pk if poll and poll.status == 'draft' else None,
                                'questions': parsed['questions'], 'errors': parsed['errors']}
                            request.session[IMPORT_KEY] = stash
                    else:
                        error = 'Choose a valid Excel (.xlsx) question file.'
                elif action == 'apply-question-import':
                    stash = request.session.get(IMPORT_KEY)
                    if not stash or stash.get('poll_owner') != request.user.pk or stash.get('errors'):
                        error = 'The question preview expired or contains errors. Upload the file again.'
                    elif stash.get('target_poll'):
                        if not poll or poll.pk != stash['target_poll'] or poll.status != 'draft':
                            error = 'The selected draft changed. Choose it again and upload the file.'
                        else:
                            from assessments.polls import append_imported_questions
                            append_imported_questions(poll, stash['questions'], request.user)
                    else:
                        poll = create_imported_standalone_draft(stash['questions'], request.organization, request.user)
                    if not error:
                        request.session.pop(IMPORT_KEY, None)
                elif action == 'cancel-question-import':
                    request.session.pop(IMPORT_KEY, None)
                elif poll:
                    transition(poll, action, request.user, option_id=request.POST.get('option'),
                        note=request.POST.get('decision_note', '').strip(),
                        question_id=request.POST.get('question'))
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
            return redirect(reverse('standalone_poll_list') + ('?poll=' + str(poll.uuid) if poll else ''))
        elif action == 'apply-question-import':
            return redirect(reverse('standalone_poll_list') + '?poll=' + str(poll.uuid))
        else:
            if action == 'save' and _poll_json_requested(request):
                return JsonResponse({'ok': True, 'message': 'Poll saved.', 'uuid': str(poll.uuid)})
            leave_to = request.POST.get('leave_to', '')
            if action == 'save' and leave_to and url_has_allowed_host_and_scheme(
                    leave_to, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                return redirect(leave_to)
            messages.success(request, 'Poll saved.' if action == 'save' else 'Poll updated.')
            return redirect(reverse('standalone_poll_list') + '?poll=' + str(poll.uuid))
    polls = ClassroomPoll.objects.filter(standalone=True, organization_id=request.organization['id'],
        created_by=request.user).annotate(response_count=Count('anonymous_responses', distinct=True),
        question_count=Count('questions', distinct=True))
    results = poll_results(poll) if poll and poll.status != 'draft' else None
    state_url = reverse('standalone_poll_state', args=[poll.uuid]) if poll else ''
    vote_url = public_url(request, reverse('standalone_poll_public', args=[poll.uuid])) if poll else ''
    return _private(render(request, 'assessments/polls/standalone_manage.html', {
        'polls': polls, 'poll': poll, 'form': form, 'results': results,
        'pending_question_import': stash, 'poll_url': vote_url, 'state_url': state_url,
        'question_import_form': question_import_form,
        'poll_question_sample_url': reverse('standalone_poll_sample'),
    }))


@never_cache
@require_http_methods(['POST'])
@_owner
def standalone_poll_delete(request):
    ids = request.POST.getlist('polls')
    if request.POST.get('action') == 'delete' and request.POST.get('poll'):
        ids = [request.POST['poll']]
    try:
        poll_ids = [uuid.UUID(value) for value in ids]
    except (ValueError, TypeError, AttributeError):
        raise Http404
    polls = ClassroomPoll.objects.filter(standalone=True,
        organization_id=request.organization['id'], created_by=request.user, uuid__in=poll_ids)
    count, _ = polls.delete()
    messages.success(request, f'{count} poll{"s" if count != 1 else ""} deleted.')
    return redirect(reverse('standalone_poll_list'))


@never_cache
@require_http_methods(['GET'])
@_owner
def standalone_poll_sample(request):
    from django.http import HttpResponse
    return _private(HttpResponse(poll_import.sample_workbook().getvalue(), content_type=(
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')))


def _public_poll(poll_uuid):
    return get_object_or_404(ClassroomPoll.objects.prefetch_related('questions__options'),
        uuid=poll_uuid, standalone=True, status__in=['open', 'closed', 'confirmed', 'cancelled'])


@never_cache
@require_http_methods(['GET', 'POST'])
@_poll_service
@public_poll_budget
def standalone_poll_public(request, poll_uuid):
    poll = _public_poll(poll_uuid)
    tokens = request.session.get(TOKEN_KEY, {})
    try:
        token = uuid.UUID(str(tokens.get(str(poll.uuid))))
    except (ValueError, TypeError, AttributeError):
        token = uuid.uuid4()
        tokens[str(poll.uuid)] = str(token)
        request.session[TOKEN_KEY] = tokens
    response = AnonymousClassroomPollResponse.objects.filter(poll=poll, token=token).first()
    error, saved = '', request.GET.get('saved') == '1'
    if request.method == 'POST':
        if request.POST.get('action') != 'vote':
            raise Http404
        answers = {}
        for question in poll.questions.all():
            key = str(question.pk)
            answers[key] = {'options': request.POST.getlist(f'question-{key}'),
                'abstained': request.POST.get(f'abstain-{key}') == 'yes'}
        try:
            save_anonymous_response(poll.pk, token, answers)
        except ValidationError as exc:
            error = ' '.join(exc.messages)
        else:
            return _private(redirect(reverse('standalone_poll_public', args=[poll.uuid]) + '?saved=1'))
    if request.GET.get('state') == '1':
        results = poll_results(poll)
        return _private(JsonResponse({'status': poll.status, 'accepting': poll.accepting_responses,
            'html': render_to_string('assessments/polls/_results.html', {
                'poll': poll, **results, 'public_results': True,
                'hide_option_bars': True, 'hide_option_percentages': True}, request=request)}))
    results = poll_results(poll)
    questions = list(poll.questions.all())
    existing = response.answers if response else {}
    for question in questions:
        answer = existing.get(str(question.pk), [])
        question.user_selections = set(answer or [])
        question.user_abstained = answer is None
        if str(question.pk) in existing:
            question.user_answer_display = ('None of these options' if answer is None else ', '.join(
                option.label for option in question.options.all() if option.pk in answer))
    return _private(render(request, 'assessments/polls/standalone_respond.html', {
        'poll': poll, 'questions': questions, 'response': response,
        'saved': saved, 'error': error,
        'state_url': reverse('standalone_poll_public', args=[poll.uuid]) + '?state=1',
        'results': results, 'public_results': True,
        'results_url': reverse('standalone_poll_live_results', args=[poll.uuid]),
    }))


@never_cache
@require_http_methods(['GET'])
@_poll_service
@public_poll_budget
def standalone_poll_qr(request, poll_uuid):
    poll = _public_poll(poll_uuid)
    url = public_url(request, reverse('standalone_poll_public', args=[poll.uuid]))
    response = HttpResponse(qr_png(url), content_type='image/png')
    if request.GET.get('display') != '1':
        response['Content-Disposition'] = 'attachment; filename="anonymous-poll-qr.png"'
    return _private(response)


@never_cache
@require_http_methods(['GET'])
@_poll_service
@public_poll_budget
def standalone_poll_qr_screen(request, poll_uuid):
    """Public, projector-friendly QR and poll status screen."""
    poll = _public_poll(poll_uuid)
    return _private(render(request, 'assessments/polls/qr_screen.html', {
        'poll': poll,
        'qr_image_url': reverse('standalone_poll_qr', args=[poll.uuid]) + '?display=1',
        'voting_url': reverse('standalone_poll_public', args=[poll.uuid]),
        'results_url': reverse('standalone_poll_live_results', args=[poll.uuid]),
        'state_url': reverse('standalone_poll_live_state', args=[poll.uuid]),
        'qr_available': poll.accepting_responses,
    }))


@never_cache
@require_http_methods(['GET'])
@_poll_service
@public_poll_budget
def standalone_poll_live_results(request, poll_uuid):
    poll = _public_poll(poll_uuid)
    results = poll_results(poll)
    return _private(render(request, 'assessments/polls/public_results.html', {
        'poll': poll, **results, 'public_results': True,
        'state_url': reverse('standalone_poll_live_state', args=[poll.uuid]),
    }))


@never_cache
@require_http_methods(['GET'])
@_poll_service
@public_poll_budget
def standalone_poll_live_state(request, poll_uuid):
    poll = _public_poll(poll_uuid)
    results = poll_results(poll)
    return _private(JsonResponse({'status': poll.status, 'accepting': poll.accepting_responses,
        'html': render_to_string('assessments/polls/_results.html', {
            'poll': poll, **results, 'public_results': True,
            'hide_option_bars': True, 'hide_option_percentages': True}, request=request)}))


@never_cache
@require_http_methods(['GET'])
@_owner
def standalone_poll_state(request, poll_uuid):
    poll = _owned(request, poll_uuid)
    if poll.status == 'draft':
        raise Http404
    return _private(JsonResponse({'status': poll.status, 'accepting': poll.accepting_responses,
        'html': render_to_string('assessments/polls/_results.html', {
            'poll': poll, **poll_results(poll),
            'hide_option_bars': True, 'hide_option_percentages': True}, request=request)}))
