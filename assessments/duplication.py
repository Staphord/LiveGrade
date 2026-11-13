"""Copy reusable session setup into a fresh draft (FA-03).

Explicit field lists prevent runtime state, results and future sensitive fields
from silently becoming part of a copy. The caller supplies an authorised owner
queryset; it is checked again under the transaction's session lock.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404

from .models import AssessmentSession, GroupMembership, LevelDescriptor, PresentationGroup, RubricCategory, Student


SESSION_SETTINGS = ('identify_by', 'ungraded_penalty', 'instructions', 'default_presentation_seconds',
                    'default_turn_seconds', 'transition_gap_seconds')
RUBRIC_FIELDS = ('name', 'description', 'scope', 'weight', 'order')
STUDENT_FIELDS = ('full_name', 'student_id', 'email', 'programme')
GROUP_FIELDS = ('name', 'topic', 'location', 'order', 'max_size', 'is_practice')


def values(instance, fields):
    return {field: getattr(instance, field) for field in fields}


@transaction.atomic
def duplicate_session(source_queryset, source_pk, *, owner, name, include_roster=False, include_groups=False):
    source = get_object_or_404(source_queryset.select_for_update(), pk=source_pk)
    # Never grant ownership through the copy helper, even if a caller accidentally
    # passes an oversight queryset. Organisation and service come from the source.
    if source.created_by_id != owner.pk:
        from django.http import Http404
        raise Http404
    duplicate = AssessmentSession(
        name=name, organization_id=source.organization_id, service=source.service,
        created_by=owner, **values(source, SESSION_SETTINGS))
    duplicate.full_clean()
    duplicate.save()
    for category in source.rubric_categories.prefetch_related('level_descriptors'):
        copied_category = RubricCategory.objects.create(
            assessment_session=duplicate, **values(category, RUBRIC_FIELDS))
        LevelDescriptor.objects.bulk_create([
            LevelDescriptor(rubric_category=copied_category, score=level.score,
                            description=level.description)
            for level in category.level_descriptors.all()])
    students = {}
    if include_roster:
        for student in source.students.all():
            students[student.pk] = Student.objects.create(
                assessment_session=duplicate, **values(student, STUDENT_FIELDS))
    if include_groups:
        for group in source.groups.prefetch_related('memberships').all():
            copied_group = PresentationGroup.objects.create(
                assessment_session=duplicate, **values(group, GROUP_FIELDS))
            if include_roster:
                memberships = list(group.memberships.all())
                if any(membership.student_id not in students for membership in memberships):
                    raise ValidationError('A source group contains a student outside this session. '
                                          'Correct its membership before duplicating.')
                GroupMembership.objects.bulk_create([
                    GroupMembership(group=copied_group, student=students[membership.student_id])
                    for membership in memberships])
    return duplicate
