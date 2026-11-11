"""Organizer endpoints for linking existing Classroom and Panel assessments."""

from decimal import Decimal, InvalidOperation
from functools import wraps

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from accounts.access import can_oversee, lecturer_required
from assessments.models import AssessmentSession, PresentationGroup
from assessments.pagination import PAGE_SIZES, paginate
from assessments.turns import activate_turn, close_turn
from .combined_assessment import (combined_assessment_results, mapping_is_complete,
                                  record_combined_moderation)
from .models import (CombinedModerationDecision, CombinedPresentationAssessment,
                     CombinedPresentationMapping, PanelAuditEvent, PanelCandidate,
                     PanelHearing)
from .operations import activate_panel_candidate, deactivate_panel_candidate


def _panel_owner(view):
    authenticated_view = lecturer_required(view)

    @wraps(view)
    def scoped(request, *args, **kwargs):
        if getattr(request, 'service', None) != 'panel':
            raise Http404
        return authenticated_view(request, *args, **kwargs)
    return scoped


class CombinedAssessmentForm(forms.Form):
    name = forms.CharField(max_length=160, label='Assessment name')
    classroom_session = forms.ModelChoiceField(queryset=AssessmentSession.objects.none(),
        label='Classroom peer assessment')
    panel_hearing = forms.ModelChoiceField(queryset=PanelHearing.objects.none(),
        label='Panel assessment')
    result_policy = forms.ChoiceField(choices=CombinedPresentationAssessment.ResultPolicy.choices)
    peer_weight = forms.DecimalField(min_value=0, max_value=100, decimal_places=2,
        initial=50, required=False)
    panel_weight = forms.DecimalField(min_value=0, max_value=100, decimal_places=2,
        initial=50, required=False)
    moderation_threshold = forms.DecimalField(min_value=0, max_value=100, decimal_places=2,
        required=False, label='Moderation threshold (score points)',
        help_text='A larger peer–Panel difference requires a recorded moderator decision before the combined mark is released.')
    minimum_peer_ratings = forms.IntegerField(min_value=1, max_value=500, initial=1)
    minimum_panel_raters = forms.IntegerField(min_value=1, max_value=50, initial=1)

    def __init__(self, *args, organization, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].help_text = 'A label for this linked assessment. It does not rename either source assessment.'
        self.fields['classroom_session'].help_text = 'Choose the existing Classroom session where peers submit ratings.'
        self.fields['panel_hearing'].help_text = 'Choose the existing Panel hearing for examiner scoring or decisions.'
        self.fields['result_policy'].help_text = (
            'Separate results keep peer and Panel outcomes distinct. A weighted mark combines numeric scores after '
            'minimum coverage and moderation checks.')
        self.fields['peer_weight'].label = 'Peer weight (%)'
        self.fields['panel_weight'].label = 'Panel weight (%)'
        self.fields['minimum_peer_ratings'].label = 'Minimum peer ratings per entry'
        self.fields['minimum_peer_ratings'].help_text = 'Minimum submitted peer ratings required for a combined mark.'
        self.fields['minimum_panel_raters'].label = 'Minimum Panel raters per entry'
        self.fields['minimum_panel_raters'].help_text = 'Minimum submitted Panel scores required for a combined mark.'
        for field in self.fields.values():
            field.widget.attrs['class'] = (
                'form-select' if isinstance(field.widget, forms.Select) else 'form-control')
            if isinstance(field.widget, forms.NumberInput):
                field.widget.attrs.setdefault(
                    'inputmode', 'decimal' if getattr(field, 'decimal_places', 0) else 'numeric')
        self.fields['classroom_session'].queryset = AssessmentSession.objects.filter(
            organization_id=organization['id'], created_by=user, service='classroom').order_by('-created_at')
        self.fields['panel_hearing'].queryset = PanelHearing.objects.for_organization(
            organization).owned_by(user).order_by('-created_at')

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('result_policy') == CombinedPresentationAssessment.ResultPolicy.WEIGHTED:
            if cleaned.get('peer_weight') is None or cleaned.get('panel_weight') is None:
                raise forms.ValidationError('Enter both weights for a weighted combined mark.')
            if cleaned['peer_weight'] + cleaned['panel_weight'] != Decimal('100'):
                raise forms.ValidationError('Peer and Panel weights must total 100%.')
            if cleaned.get('moderation_threshold') is None:
                self.add_error('moderation_threshold', 'Set the difference that will require a reasoned review.')
            hearing = cleaned.get('panel_hearing')
            if hearing and hearing.workflow not in (PanelHearing.Workflow.SCORED, PanelHearing.Workflow.HYBRID):
                self.add_error('panel_hearing', 'Weighted marks require numeric Panel rubric scoring.')
        return cleaned


def _owned_assessment(request, assessment_id):
    return (CombinedPresentationAssessment.objects.for_organization(request.organization)
            .owned_by(request.user).filter(pk=assessment_id).select_related(
                'classroom_session', 'panel_hearing').first())


@_panel_owner
@require_http_methods(['GET'])
def combined_assessment_list(request):
    """Searchable, filterable, and paginated list of combined assessments."""
    base_qs = (CombinedPresentationAssessment.objects.for_organization(request.organization)
        .owned_by(request.user)
        .select_related('classroom_session', 'panel_hearing')
        .annotate(mappings_count=Count('mappings', distinct=True)))

    status_counts = {
        'total': base_qs.count(),
        'draft': base_qs.filter(status=CombinedPresentationAssessment.Status.DRAFT).count(),
        'active': base_qs.filter(status=CombinedPresentationAssessment.Status.ACTIVE).count(),
        'closed': base_qs.filter(status=CombinedPresentationAssessment.Status.CLOSED).count(),
    }

    filtered_qs = base_qs
    search_query = (request.GET.get('q') or '').strip()[:100]
    if search_query:
        filtered_qs = filtered_qs.filter(
            Q(name__icontains=search_query) |
            Q(classroom_session__name__icontains=search_query) |
            Q(panel_hearing__name__icontains=search_query)
        )

    status_filter = request.GET.get('status', '').strip()
    if status_filter in CombinedPresentationAssessment.Status.values:
        filtered_qs = filtered_qs.filter(status=status_filter)
    else:
        status_filter = ''

    policy_filter = request.GET.get('policy', '').strip()
    if policy_filter in CombinedPresentationAssessment.ResultPolicy.values:
        filtered_qs = filtered_qs.filter(result_policy=policy_filter)
    else:
        policy_filter = ''

    filtered_qs = filtered_qs.order_by('-created_at', '-pk')
    filtered_count = filtered_qs.count()

    paginator, page_obj = paginate(request, filtered_qs)

    query_params = QueryDict('', mutable=True)
    if search_query:
        query_params['q'] = search_query
    if status_filter:
        query_params['status'] = status_filter
    if policy_filter:
        query_params['policy'] = policy_filter
    query_params['per_page'] = str(paginator.per_page)
    querystring = query_params.urlencode()

    hearing_count = (PanelHearing.objects.for_organization(request.organization)
                     .owned_by(request.user).count())

    context = {
        'assessments': filtered_qs,
        'page_obj': page_obj,
        'paginator': paginator,
        'status_counts': status_counts,
        'search_query': search_query,
        'status_filter': status_filter,
        'policy_filter': policy_filter,
        'filtered_count': filtered_count,
        'per_page': paginator.per_page,
        'page_sizes': PAGE_SIZES,
        'querystring': querystring,
        'active_tab': 'combined',
        'combined_count': status_counts['total'],
        'hearing_count': hearing_count,
        'can_oversee_panel': can_oversee(request),
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        response = render(request, 'service_domains/_partials/combined_assessment_results.html', context)
        response['Vary'] = 'X-Requested-With'
        return response

    return render(request, 'service_domains/combined_assessment_list.html', context)


def _combined_bulk_delete_redirect(request):
    redirect_url = reverse('combined_assessment_list')
    query_params = QueryDict('', mutable=True)
    q = (request.POST.get('q') or '').strip()
    status = (request.POST.get('status') or '').strip()
    policy = (request.POST.get('policy') or '').strip()
    per_page = (request.POST.get('per_page') or '').strip()
    if q:
        query_params['q'] = q
    if status in CombinedPresentationAssessment.Status.values:
        query_params['status'] = status
    if policy in CombinedPresentationAssessment.ResultPolicy.values:
        query_params['policy'] = policy
    if per_page:
        query_params['per_page'] = per_page
    qs = query_params.urlencode()
    if qs:
        redirect_url = f'{redirect_url}?{qs}'
    return redirect(redirect_url)


@_panel_owner
@require_POST
def combined_assessment_bulk_delete(request):
    """Delete multiple selected draft combined assessments."""
    raw_ids = request.POST.getlist('assessment_ids')
    if not raw_ids:
        messages.error(request, 'Select at least one combined assessment to delete.')
        return _combined_bulk_delete_redirect(request)

    valid_ids = []
    for raw_id in raw_ids:
        raw_id = raw_id.strip()
        if not raw_id.isdigit() or len(raw_id) > 10:
            messages.error(request, 'Invalid combined assessment ID submitted.')
            return _combined_bulk_delete_redirect(request)
        valid_ids.append(int(raw_id))

    if len(valid_ids) > 100:
        messages.error(request, 'You cannot delete more than 100 combined assessments at once.')
        return _combined_bulk_delete_redirect(request)

    with transaction.atomic():
        locked = list(
            CombinedPresentationAssessment.objects.for_organization(request.organization)
            .owned_by(request.user)
            .filter(pk__in=valid_ids, status=CombinedPresentationAssessment.Status.DRAFT)
            .select_for_update()
        )
        if len(locked) != len(set(valid_ids)):
            messages.error(request, 'One or more selected combined assessments cannot be deleted or are no longer available.')
            return _combined_bulk_delete_redirect(request)

        count = len(locked)
        for item in locked:
            item.delete()

    messages.success(request, f'{count} combined assessment{"s" if count != 1 else ""} deleted.')
    return _combined_bulk_delete_redirect(request)


@_panel_owner
@require_POST
def combined_assessment_delete(request, assessment_id):
    """Delete a single draft combined assessment."""
    item = _owned_assessment(request, assessment_id)
    if item is None:
        raise Http404
    if item.status != CombinedPresentationAssessment.Status.DRAFT:
        messages.error(request, 'Published combined assessments cannot be deleted.')
        return redirect('combined_assessment_list')
    name = item.name
    item.delete()
    messages.success(request, f'Combined assessment "{name}" deleted.')
    return redirect('combined_assessment_list')


@_panel_owner
@require_http_methods(['GET', 'POST'])
def combined_assessment_create(request):
    if request.method == 'POST':
        form = CombinedAssessmentForm(request.POST, organization=request.organization, user=request.user)
        if form.is_valid():
            values = form.cleaned_data
            item = CombinedPresentationAssessment(
                name=values['name'].strip(), classroom_session=values['classroom_session'],
                panel_hearing=values['panel_hearing'], result_policy=values['result_policy'],
                peer_weight=values['peer_weight'] if values['peer_weight'] is not None else Decimal('50'),
                panel_weight=values['panel_weight'] if values['panel_weight'] is not None else Decimal('50'),
                moderation_threshold=values['moderation_threshold'],
                minimum_peer_ratings=values['minimum_peer_ratings'],
                minimum_panel_raters=values['minimum_panel_raters'], status='draft',
                organization_id=request.organization['id'], created_by=request.user)
            try:
                groups = list(PresentationGroup.objects.filter(
                    assessment_session=values['classroom_session'], is_practice=False))
                candidates = list(PanelCandidate.objects.for_organization(request.organization)
                    .owned_by(request.user).filter(hearing=values['panel_hearing']))
                pairs = []
                for group in groups:
                    linked = [candidate for candidate in candidates
                        if candidate.source_group_id == group.pk]
                    if len(linked) == 1:
                        pairs.append((group, linked[0]))
                with transaction.atomic():
                    item.full_clean()
                    item.save()
                    for group, candidate in pairs:
                        mapping = CombinedPresentationMapping(assessment=item, group=group,
                            candidate=candidate, organization_id=item.organization_id,
                            created_by=request.user)
                        mapping.full_clean()
                        mapping.save()
            except (ValidationError, InvalidOperation, IntegrityError) as exc:
                form.add_error(None, '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc))
            else:
                if item.mappings.count() != len(groups) or item.mappings.count() != len(candidates):
                    messages.success(request,
                        'Draft created. Confirm each group-to-candidate link; matching names are not proof of identity.')
                else:
                    messages.success(request, 'Combined assessment created. Review the mapped entries before publishing.')
                return redirect('combined_assessment_detail', assessment_id=item.pk)
    else:
        form = CombinedAssessmentForm(organization=request.organization, user=request.user,
            initial={'classroom_session': request.GET.get('classroom_session'),
                     'panel_hearing': request.GET.get('panel_hearing')})
    return render(request, 'service_domains/combined_assessment_form.html', {'form': form})


@_panel_owner
@require_http_methods(['GET', 'POST'])
def combined_assessment_detail(request, assessment_id):
    item = _owned_assessment(request, assessment_id)
    if item is None:
        raise Http404
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'delete_draft' and item.status == CombinedPresentationAssessment.Status.DRAFT:
            item.delete()
            messages.success(request, 'Draft combined assessment deleted. You can now choose the correct source assessments.')
            return redirect('combined_assessment_list')
        elif action == 'publish' and item.status == CombinedPresentationAssessment.Status.DRAFT:
            if item.result_policy == CombinedPresentationAssessment.ResultPolicy.WEIGHTED:
                if item.peer_weight + item.panel_weight != Decimal('100'):
                    messages.error(request, 'Weights must total 100%.')
                    return redirect('combined_assessment_detail', assessment_id=item.pk)
                if item.panel_hearing.workflow not in (PanelHearing.Workflow.SCORED, PanelHearing.Workflow.HYBRID):
                    messages.error(request, 'This Panel workflow does not produce a numeric mark.')
                    return redirect('combined_assessment_detail', assessment_id=item.pk)
            if not mapping_is_complete(item):
                messages.error(request, 'Map every presentation group to exactly one Panel candidate before publishing.')
            else:
                item.status = CombinedPresentationAssessment.Status.ACTIVE
                item.save(update_fields=['status', 'updated_at'])
                messages.success(request, 'Combined assessment published. Its result policy is now locked.')
        elif action == 'create_candidate_from_group' and item.status == CombinedPresentationAssessment.Status.DRAFT:
            group = PresentationGroup.objects.filter(
                assessment_session=item.classroom_session, is_practice=False,
                pk=request.POST.get('group_id')).first()
            if group is None:
                raise Http404
            if item.panel_hearing.status != PanelHearing.Status.DRAFT:
                messages.error(request,
                    'Panel entries can be imported from Classroom groups only while the Panel hearing is a draft.')
            elif PanelCandidate.objects.filter(hearing=item.panel_hearing, source_group=group).exists():
                messages.info(request, 'This Classroom group already has a linked Panel entry.')
            else:
                candidate = PanelCandidate(hearing=item.panel_hearing, source_group=group,
                    display_name=group.name, order=group.order,
                    organization_id=item.organization_id, created_by=request.user)
                event = PanelAuditEvent(hearing=item.panel_hearing,
                    action=PanelAuditEvent.Action.CANDIDATE_ADDED,
                    reason=f'Imported from Classroom group #{group.pk} for combined assessment.',
                    actor_user=request.user, candidate=None,
                    organization_id=item.organization_id, created_by=request.user)
                try:
                    with transaction.atomic():
                        candidate.full_clean()
                        candidate.save(force_insert=True)
                        event.candidate = candidate
                        event.full_clean()
                        event.save(force_insert=True)
                        mapping = CombinedPresentationMapping(assessment=item, group=group,
                            candidate=candidate, organization_id=item.organization_id,
                            created_by=request.user)
                        mapping.full_clean()
                        mapping.save(force_insert=True)
                except (ValidationError, IntegrityError) as exc:
                    messages.error(request, '; '.join(exc.messages) if hasattr(exc, 'messages') else
                        'The Panel entry could not be created. Refresh and review the source rosters.')
                else:
                    messages.success(request, f'Created and linked Panel entry for {group.name}.')
        elif action == 'add_mapping' and item.status == CombinedPresentationAssessment.Status.DRAFT:
            group = PresentationGroup.objects.filter(
                assessment_session=item.classroom_session, is_practice=False,
                pk=request.POST.get('group_id')).first()
            candidate = PanelCandidate.objects.for_organization(request.organization).owned_by(
                request.user).filter(hearing=item.panel_hearing,
                    pk=request.POST.get('candidate_id')).filter(
                        Q(source_group__isnull=True) | Q(source_group=group)).first() if group else None
            if group is None or candidate is None:
                raise Http404
            try:
                with transaction.atomic():
                    if candidate.source_group_id is None:
                        candidate.source_group = group
                        candidate.full_clean()
                        candidate.save(update_fields=['source_group', 'updated_at'])
                        event = PanelAuditEvent(hearing=item.panel_hearing,
                            action=PanelAuditEvent.Action.CANDIDATE_LINKED,
                            reason=(f'Organizer linked existing candidate to Classroom group '
                                f'#{group.pk} for combined assessment.'),
                            actor_user=request.user, candidate=candidate,
                            organization_id=item.organization_id, created_by=request.user)
                        event.full_clean()
                        event.save(force_insert=True)
                    mapping = CombinedPresentationMapping(assessment=item, group=group,
                        candidate=candidate, organization_id=item.organization_id, created_by=request.user)
                    mapping.full_clean()
                    mapping.save()
                messages.success(request,
                    f'Confirmed {candidate.display_name} as the Panel entry for {group.name}.')
            except (ValidationError, IntegrityError) as exc:
                messages.error(request, '; '.join(exc.messages) if hasattr(exc, 'messages') else
                    'That group or candidate is already mapped. Refresh and choose another entry.')
        elif action == 'remove_mapping' and item.status == CombinedPresentationAssessment.Status.DRAFT:
            mapping = item.mappings.filter(pk=request.POST.get('mapping_id')).first()
            if mapping is None:
                raise Http404
            mapping.delete()
            messages.success(request, 'Mapping removed. Confirm it again to capture the current group membership.')
        elif action == 'activate_entry' and item.status == CombinedPresentationAssessment.Status.ACTIVE:
            mapping = item.mappings.select_related('group', 'candidate').filter(
                pk=request.POST.get('mapping_id')).first()
            if mapping is None:
                raise Http404
            if not mapping_is_complete(item):
                messages.error(request, 'The source rosters changed after publishing. Review the source assessments before continuing.')
            elif (item.classroom_session.status != AssessmentSession.Status.LIVE or
                    item.panel_hearing.status != PanelHearing.Status.OPEN):
                messages.error(request, 'Open the linked Classroom session and Panel hearing before paired scoring.')
            elif mapping.group.turns.filter(status='closed').exists():
                messages.error(request, 'This presentation has already been completed and cannot be started again.')
            elif (item.classroom_session.active_turn() and
                  item.classroom_session.active_turn().group_id == mapping.group_id and
                  item.panel_hearing.active_candidate_id == mapping.candidate_id):
                messages.info(request, 'This presentation is already active in both scoring sources.')
            else:
                if item.panel_hearing.active_candidate_id != mapping.candidate_id:
                    activate_panel_candidate(item.panel_hearing, mapping.candidate,
                        organization=request.organization, user=request.user)
                active_turn = item.classroom_session.active_turn()
                if not active_turn or active_turn.group_id != mapping.group_id:
                    activate_turn(item.classroom_session, mapping.group)
                messages.success(request, f'Peer and Panel scoring are now active for {mapping.group.name}.')
        elif action == 'moderate' and item.status in (
                CombinedPresentationAssessment.Status.ACTIVE, CombinedPresentationAssessment.Status.CLOSED):
            mapping = item.mappings.select_related('group', 'candidate').filter(
                pk=request.POST.get('mapping_id')).first()
            if mapping is None:
                raise Http404
            decision = request.POST.get('decision')
            reason = (request.POST.get('reason') or '').strip()
            try:
                record_combined_moderation(mapping, decision, reason,
                    organization=request.organization, user=request.user)
                messages.success(request, 'Moderation decision recorded against the displayed source scores.')
            except ValidationError as exc:
                messages.error(request, '; '.join(exc.messages))
        elif action == 'pause_entry' and item.status == CombinedPresentationAssessment.Status.ACTIVE:
            active_turn = item.classroom_session.active_turn()
            active_mapping = (item.mappings.filter(group_id=active_turn.group_id).first()
                if active_turn else None)
            if (active_mapping is None or
                    item.panel_hearing.active_candidate_id != active_mapping.candidate_id):
                messages.error(request,
                    'The peer and Panel active entries do not match. Check both assessments before pausing.')
            else:
                close_turn(active_turn)
                if item.panel_hearing.status == PanelHearing.Status.OPEN:
                    deactivate_panel_candidate(item.panel_hearing,
                        organization=request.organization, user=request.user)
                messages.success(request, 'The active peer and Panel scoring slot is paused.')
        elif action == 'close' and item.status == CombinedPresentationAssessment.Status.ACTIVE:
            active_turn = item.classroom_session.active_turn()
            if active_turn:
                close_turn(active_turn)
            if item.panel_hearing.status == PanelHearing.Status.OPEN:
                deactivate_panel_candidate(item.panel_hearing,
                    organization=request.organization, user=request.user)
            item.status = CombinedPresentationAssessment.Status.CLOSED
            item.save(update_fields=['status', 'updated_at'])
            messages.success(request, 'Combined assessment closed.')
        else:
            messages.error(request, 'That action is not available in the current assessment state.')
        return redirect('combined_assessment_detail', assessment_id=item.pk)
    active_turn = item.classroom_session.active_turn()
    active_mapping = (item.mappings.filter(group_id=active_turn.group_id).first()
        if active_turn else None)
    paired_slot_active = bool(active_mapping and
        item.panel_hearing.active_candidate_id == active_mapping.candidate_id)
    has_unpaired_active_entry = bool(active_turn or item.panel_hearing.active_candidate_id)
    rows = combined_assessment_results(item)
    mapped_group_ids = item.mappings.values_list('group_id', flat=True)
    mapped_candidate_ids = item.mappings.values_list('candidate_id', flat=True)
    unmapped_groups = (item.classroom_session.groups.filter(is_practice=False)
        .exclude(pk__in=mapped_group_ids).prefetch_related('memberships__student'))
    all_unmapped_candidates = item.panel_hearing.candidates.exclude(pk__in=mapped_candidate_ids)
    unmapped_candidates = all_unmapped_candidates.filter(
        Q(source_group__isnull=True) | Q(source_group__in=unmapped_groups))
    unusable_candidates = all_unmapped_candidates.exclude(pk__in=unmapped_candidates.values('pk'))
    mapping_complete = mapping_is_complete(item)
    roster_changed_mappings = []
    for mapping in item.mappings.select_related('group').prefetch_related('group__memberships__student'):
        current_member_ids = sorted(membership.student_id for membership in mapping.group.memberships.all())
        if current_member_ids != mapping.group_member_ids_snapshot:
            roster_changed_mappings.append(mapping)
    result_view = request.GET.get('results', 'all')
    permitted_views = {'all', 'peer', 'panel'}
    if item.result_policy == CombinedPresentationAssessment.ResultPolicy.WEIGHTED:
        permitted_views.add('combined')
    if result_view not in permitted_views:
        result_view = 'all'
    for row in rows:
        row['can_start'] = mapping_complete and not row['already_presented'] and not (
            active_turn and active_turn.group_id == row['group'].pk)
    return render(request, 'service_domains/combined_assessment_detail.html', {
        'assessment': item,
        'rows': rows,
        'active_turn': active_turn,
        'paired_slot_active': paired_slot_active,
        'has_unpaired_active_entry': has_unpaired_active_entry and not paired_slot_active,
        'result_view': result_view,
        'unmapped_groups': unmapped_groups,
        'unmapped_candidates': unmapped_candidates,
        'unusable_candidates': unusable_candidates,
        'mapping_complete': mapping_complete,
        'roster_changed_mappings': roster_changed_mappings,
    })
