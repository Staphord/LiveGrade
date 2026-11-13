"""FA-06: service-scoped configuration snapshots and starter examples.

Only explicit configuration fields enter a template. Templates contain no roster,
groups, identifiers, results or runtime state. Personal templates never grant access
to their source session, and continue working if that source is deleted.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404

from .duplication import RUBRIC_FIELDS, SESSION_SETTINGS, values
from .level_descriptors import MAX_DESCRIPTOR_TEXT, MAX_LEVELS_PER_CRITERION, score_text
from .models import (LevelDescriptor, RubricCategory, SessionTemplate, TemplateLevelDescriptor,
                     TemplateRubricCategory)
from .rubric_rules import check_rubric


@dataclass(frozen=True)
class StarterTemplate:
    key: str
    name: str
    description: str
    settings: dict
    categories: tuple


def _starter(key, name, description, categories):
    return StarterTemplate(key, name, description, {
        'identify_by': 'full_name', 'ungraded_penalty': Decimal('1.00'),
        'instructions': 'Use evidence from the presentation to score each criterion independently.',
        'default_presentation_seconds': None, 'default_turn_seconds': None, 'transition_gap_seconds': 0,
    }, tuple({'name': title, 'description': guidance, 'scope': scope, 'weight': Decimal(weight), 'order': i}
             for i, (title, guidance, scope, weight) in enumerate(categories)))


CLASSROOM_STARTERS = (
    _starter('presentation', 'Class presentation', 'A group presentation with questions and discussion.', (
        ('Content', 'Accuracy, understanding and supporting evidence.', 'group', '50'),
        ('Delivery', 'Clear structure and communication.', 'group', '30'),
        ('Questions', 'Reasoned responses to questions.', 'group', '20'),
    )),
    _starter('project-demo', 'Project demonstration', 'A project rubric with an individual contribution criterion.', (
        ('Implementation', 'How well the demonstrated solution works.', 'group', '50'),
        ('Evidence', 'Demonstrated testing and explanation of choices.', 'group', '30'),
        ('Contribution', 'The individual presenter\'s understanding and contribution.', 'individual', '20'),
    )),
    _starter('individual', 'Individual presentation', 'Assess each presenter individually; place presenters in groups for the live queue.', (
        ('Understanding', 'Accurate explanation supported by evidence.', 'individual', '60'),
        ('Communication', 'Clear delivery and responses.', 'individual', '40'),
    )),
)


TEAM_STARTERS = (
    _starter('team-demo', 'Team demo',
        'Review a working increment, evidence, and team communication.', (
            ('Working outcome', 'What the team demonstrated and how well it works.', 'group', '45'),
            ('Evidence and testing', 'Evidence that the result was checked and limitations understood.', 'group', '30'),
            ('Team explanation', 'How clearly the team explained decisions and answered questions.', 'group', '25'),
        )),
    _starter('capstone-checkpoint', 'Capstone checkpoint',
        'Review progress, evidence, and the next milestone during a longer project.', (
            ('Problem and progress', 'Progress against the agreed problem and current milestone.', 'group', '30'),
            ('Prototype or deliverable', 'Quality and functionality of the demonstrated work.', 'group', '35'),
            ('Evidence and reflection', 'Testing, learning, risks, and next steps are supported with evidence.', 'group', '35'),
        )),
    _starter('pitch-practice', 'Pitch practice',
        'Practice a short proposal or product pitch and questions.', (
            ('Value and audience', 'The need, intended audience, and value are clear.', 'group', '35'),
            ('Evidence and feasibility', 'Claims, assumptions, and delivery plan are credible.', 'group', '35'),
            ('Communication and responses', 'The pitch is clear and questions are answered thoughtfully.', 'group', '30'),
        )),
)


def starters_for(service):
    if service == 'classroom':
        return CLASSROOM_STARTERS
    if service == 'team':
        return TEAM_STARTERS
    return ()


def template_queryset(request):
    return (SessionTemplate.objects.for_request(request)
            .for_service(getattr(request, 'service', None)).owned_by(request.user))


def blueprint(template):
    if isinstance(template, StarterTemplate):
        # Return new dictionaries: using a starter must never mutate the registry.
        return dict(template.settings), [dict(row, level_descriptors=list(row.get('level_descriptors', [])))
                                          for row in template.categories]
    rows = []
    for category in template.rubric_categories.prefetch_related('level_descriptors'):
        row = values(category, RUBRIC_FIELDS)
        row['level_descriptors'] = [
            {'score': level.score, 'description': level.description}
            for level in category.level_descriptors.all()]
        rows.append(row)
    return values(template, SESSION_SETTINGS), rows


def validate_categories(rows):
    for row in rows:
        if len(row.get('level_descriptors', [])) > MAX_LEVELS_PER_CRITERION:
            raise ValidationError(f'A criterion can have at most {MAX_LEVELS_PER_CRITERION} score descriptors.')
        descriptor_text_length = sum(
            len(f'{score_text(level["score"])} | {level["description"]}') + 1
            for level in row.get('level_descriptors', []))
        if descriptor_text_length > MAX_DESCRIPTOR_TEXT:
            raise ValidationError(f'Descriptor text is longer than {MAX_DESCRIPTOR_TEXT} characters.')
        seen = set()
        for level in row.get('level_descriptors', []):
            if level['score'] < 0 or level['score'] > row['weight']:
                raise ValidationError('A descriptor point value cannot exceed the criterion weight.')
            if level['score'] in seen:
                raise ValidationError('A rubric criterion cannot repeat a descriptor point value.')
            if not level['description'].strip() or len(level['description']) > 500:
                raise ValidationError('Each descriptor needs guidance of 1 to 500 characters.')
            seen.add(level['score'])
    for row in rows:
        category = TemplateRubricCategory(**{key: value for key, value in row.items()
                                             if key != 'level_descriptors'})
        category.full_clean(exclude=['template'])
        for level in row.get('level_descriptors', []):
            descriptor = TemplateLevelDescriptor(rubric_category=category, **level)
            descriptor.full_clean(exclude=['rubric_category'])
    verdict = check_rubric([dict(row, row=None) for row in rows], strict=False)
    if verdict['errors']:
        raise ValidationError([message for _, message in verdict['errors']])


@transaction.atomic
def save_session_template(source_queryset, source_pk, *, owner, name, description=''):
    source = get_object_or_404(source_queryset.select_for_update(), pk=source_pk)
    if source.created_by_id != owner.pk:
        raise Http404
    rows = []
    for category in source.rubric_categories.prefetch_related('level_descriptors'):
        row = values(category, RUBRIC_FIELDS)
        row['level_descriptors'] = [
            {'score': level.score, 'description': level.description}
            for level in category.level_descriptors.all()]
        rows.append(row)
    validate_categories(rows)
    template = SessionTemplate(name=name, description=description, created_by=owner,
        organization_id=source.organization_id, service=source.service, **values(source, SESSION_SETTINGS))
    template.full_clean()
    template.save()
    for row in rows:
        levels = row.get('level_descriptors', [])
        category = TemplateRubricCategory.objects.create(
            template=template, **{key: value for key, value in row.items() if key != 'level_descriptors'})
        TemplateLevelDescriptor.objects.bulk_create([
            TemplateLevelDescriptor(rubric_category=category, **level) for level in levels])
    return template


@transaction.atomic
def save_draft_from_template(session, template):
    """Save an already validated new session and attach only the selected rubric.

    The view derives destination scope and ownership from the current request.
    Recheck persisted-template scope before any write, including for callers
    outside the view. Starter membership is validated by the service registry.
    """
    if session.pk is not None or session.status != session.Status.DRAFT or not session.created_by_id:
        raise ValidationError('A template can only create a new owned draft session.')
    if template is not None:
        if isinstance(template, StarterTemplate):
            if template not in starters_for(session.service):
                raise Http404
        elif (template.organization_id != session.organization_id or template.service != session.service
              or template.created_by_id != session.created_by_id):
            raise Http404
        _, rows = blueprint(template)
        validate_categories(rows)
    else:
        rows = []
    session.full_clean()
    session.save()
    for row in rows:
        levels = row.get('level_descriptors', [])
        category = RubricCategory.objects.create(
            assessment_session=session,
            **{key: value for key, value in row.items() if key != 'level_descriptors'})
        LevelDescriptor.objects.bulk_create([
            LevelDescriptor(rubric_category=category, **level) for level in levels])
    return session
