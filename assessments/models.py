"""LiveGrade — live, QR-joined peer-assessment models.

A lecturer (anybody DevPerf says may run LiveGrade for the organization —
see ``accounts.access``) runs a
live, QR-code-joined peer grading session for presentations, demos, or any
group activity worth grading in the room — final-year-project defenses and
developer sprint/demo assessments alike. Students never get a Django
account: they identify themselves against a roster the lecturer controls,
grade whichever group is currently presenting against a lecturer-defined
rubric, and cannot grade their own group.

Anonymity model: ``Evaluation.evaluator`` is always recorded (it is what
blocks self-voting and duplicate submission, and it is what populates
participation tracking), so anonymity is *not* achieved by omitting who
graded. It is a rule about what lecturer-facing code is allowed to query:
only aggregates over ``EvaluationScore`` (avg/weighted sum), never a
row that joins an evaluator identity to a raw score. See
``assessments/scoring.py`` and ``test_lecturer_cannot_see_individual_scores``.
"""

import uuid

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from .privacy_copy import (
    DEFAULT_PRIVACY_NOTICE_BODY, DEFAULT_PRIVACY_NOTICE_TITLE,
    DEFAULT_PRIVACY_NOTICE_VERSION,
)

from accounts.scoping import OrganizationScopedModel, OwnedManager

#: What a session's rubric weights add up to. A category's weight is its real
#: share of this many points.
TOTAL_WEIGHT = Decimal('100')


class AssessmentSession(OrganizationScopedModel):
    """One LiveGrade session (e.g. 'Sprint Demo — Q1 2026')."""

    class IdentifyBy(models.TextChoices):
        FULL_NAME = 'full_name', 'Full name'
        STUDENT_ID = 'student_id', 'Student ID'

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        LIVE = 'live', 'Live'
        CLOSED = 'closed', 'Closed'

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False,
        help_text='Used in the public join URL instead of the numeric id, '
                  'so a student cannot guess or enumerate other sessions.')
    service = models.CharField(max_length=20, default='classroom', db_index=True,
        help_text='The LiveGrade service this session belongs to (a slug from portal/services.py). '
                  'Every owner page works on one service only, so a session of one service is '
                  'never reachable from another.')
    name = models.CharField(max_length=150)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='assessment_sessions_created')
    identify_by = models.CharField(max_length=20, choices=IdentifyBy.choices,
        default=IdentifyBy.FULL_NAME,
        help_text='Which roster field a student types in to join.')
    status = models.CharField(max_length=10, choices=Status.choices,
        default=Status.DRAFT)
    ungraded_penalty = models.DecimalField(max_digits=5, decimal_places=2,
        default=Decimal('1.00'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
        help_text='Points taken off a student\'s final score for every other group '
                  'they did not grade. 0 turns the penalty off.')
    instructions = models.TextField(blank=True, default='', max_length=4000,
        help_text='Instructions shown when students join and before they score. '
                  'Explain how to use the rubric and how their scores count.')
    voting_paused = models.BooleanField(default=False,
        help_text='Lecturer-controlled kill switch: no evaluation can be '
                  'submitted while true, independent of which turn is active.')
    joining_locked = models.BooleanField(default=False,
        help_text='Prevent additional students from joining after the room is ready.')
    default_presentation_seconds = models.PositiveIntegerField(null=True, blank=True,
        help_text='Optional target presentation duration (seconds) shown as a countdown '
                  'to the presenting group and on the public screen. Does not auto-advance '
                  'the session - it is informational only, giving the lecturer a visible '
                  'cue for when to open voting. Null means no presentation timer is set.')
    default_turn_seconds = models.PositiveIntegerField(null=True, blank=True,
        help_text='Remembered voting-timer length (seconds) from the last '
                  'time the lecturer activated a group - prefills the '
                  'control next time rather than resetting it. Null means '
                  '"no timer" was last chosen.')
    transition_gap_seconds = models.PositiveIntegerField(default=0,
        help_text='Pause (seconds) between one group\'s voting closing and '
                  'the next group automatically starting, so an auto-'
                  'advancing session gives everyone a breather instead of '
                  'snapping straight into the next group with zero delay. '
                  '0 means no gap - advance immediately, as before.')
    pending_next_group = models.ForeignKey('PresentationGroup', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
        help_text='Set while a timed transition gap is counting down '
                  '(see `transition_gap_seconds`) - the group that will '
                  'become active once `pending_transition_at` arrives.')
    pending_transition_at = models.DateTimeField(null=True, blank=True)
    designated_next_group = models.ForeignKey('PresentationGroup', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
        help_text='Lecturer-chosen next group in line. Takes precedence over default order.')
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    public_leaderboard_show_scores = models.BooleanField(default=False,
        help_text='Show numeric group scores on the public leaderboard; rankings are always visible.')
    public_results_released_at = models.DateTimeField(null=True, blank=True,
        help_text='When the owner last enabled the aggregate group leaderboard on the public join screen.')
    public_results_released_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='public_classroom_results_released',
        help_text='Owner who enabled the aggregate group leaderboard.')

    objects = OwnedManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def active_turn(self):
        return self.presentation_turns.filter(
            status=PresentationTurn.Status.ACTIVE).first()

    def next_group_after(self, group=None):
        """The group to present next: returns `designated_next_group` if set
        and hasn't presented yet, otherwise the first unpresented group by order."""
        presented_ids = set(self.presentation_turns
            .filter(status=PresentationTurn.Status.CLOSED)
            .values_list('group_id', flat=True))
        if group:
            presented_ids.add(group.pk)
        active_turn = self.active_turn()
        if active_turn:
            presented_ids.add(active_turn.group_id)

        if self.designated_next_group_id and self.designated_next_group_id not in presented_ids:
            return self.designated_next_group

        return (self.groups.exclude(pk__in=presented_ids)
                .order_by('order', 'id').first())

    def get_effective_next_group(self):
        """The group that will present next, taking designated next into account."""
        active = self.active_turn()
        return self.next_group_after(active.group if active else None)

    def rubric_weight_total(self):
        """All category weights added up, whichever scope they are in."""
        return sum((c.weight for c in self.rubric_categories.all()), Decimal('0'))

    def group_weight_total(self):
        return sum((c.weight for c in self.rubric_categories.filter(
            scope=RubricCategory.Scope.GROUP)), Decimal('0'))

    def individual_weight_total(self):
        return sum((c.weight for c in self.rubric_categories.filter(
            scope=RubricCategory.Scope.INDIVIDUAL)), Decimal('0'))

    def can_go_live(self):
        """The rubric's weights, group and individual together, must add up to
        exactly ``TOTAL_WEIGHT``: a category's weight is its real share of the
        100-point final score, so anything less would cap the best possible
        score below 100."""
        return (self.rubric_categories.exists() and self.groups.exists()
                and self.rubric_weight_total() == TOTAL_WEIGHT)


class RubricCategory(models.Model):
    """A gradeable dimension, e.g. 'Technical implementation'.

    Defaults to group scope per the lecturer's instruction ('by default iwe
    group but you can change it individual'). ``weight`` is the category's share
    of the 100-point final score, and a student scores it from 0 up to that weight.
    """

    class Scope(models.TextChoices):
        GROUP = 'group', 'Group / project'
        INDIVIDUAL = 'individual', 'Individual student'

    assessment_session = models.ForeignKey(AssessmentSession,
        on_delete=models.CASCADE, related_name='rubric_categories')
    name = models.CharField(max_length=150)
    description = models.CharField(max_length=255, blank=True)
    scope = models.CharField(max_length=10, choices=Scope.choices,
        default=Scope.GROUP)
    weight = models.DecimalField(max_digits=5, decimal_places=2,
        default=Decimal('0.00'),
        help_text='This category\'s real share of the 100-point final score. All '
                  'weights in a session, group and individual together, add up '
                  'to at most 100 (exactly 100 to go live).')
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['scope', 'order', 'id']
        verbose_name_plural = 'rubric categories'

    def __str__(self):
        return f'{self.name} ({self.get_scope_display()}, {self.weight}%)'


class LevelDescriptor(models.Model):
    """Owner-written guidance anchored to a score value on one criterion."""
    rubric_category = models.ForeignKey(RubricCategory, on_delete=models.CASCADE,
        related_name='level_descriptors')
    score = models.DecimalField(max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(TOTAL_WEIGHT)])
    description = models.CharField(max_length=500)

    class Meta:
        ordering = ['score', 'pk']
        constraints = [models.UniqueConstraint(fields=['rubric_category', 'score'],
            name='unique_rubric_level_score')]

    def clean(self):
        if self.rubric_category_id and self.score is not None and self.score > self.rubric_category.weight:
            raise ValidationError({'score': 'A descriptor score cannot exceed the criterion weight.'})
        if not self.description or not self.description.strip():
            raise ValidationError({'description': 'Enter guidance for this score level.'})

    def __str__(self):
        return f'{self.rubric_category.name} - {self.score}: {self.description}'


class Student(models.Model):
    """One roster row for one assessment session. Not a Django User."""

    assessment_session = models.ForeignKey(AssessmentSession,
        on_delete=models.CASCADE, related_name='students')
    full_name = models.CharField(max_length=150)
    student_id = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    programme = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['full_name']

    def __str__(self):
        return self.full_name or self.student_id

    def save(self, *args, **kwargs):
        """Keep Classroom's expanded roster identity complete on every save path."""
        using = kwargs.get('using') or self._state.db or 'default'
        with transaction.atomic(using=using):
            super().save(*args, **kwargs)
            session = self._state.fields_cache.get('assessment_session')
            if session is None:
                session = AssessmentSession.objects.using(using).only('service').get(
                    pk=self.assessment_session_id)
            if session.service == 'classroom':
                Evaluator.objects.using(using).get_or_create(
                    assessment_session_id=self.assessment_session_id,
                    student_id=self.pk,
                    defaults={'kind': Evaluator.Kind.ROSTER},
                )

    def clean(self):
        if not self.assessment_session_id:
            return
        identify_by = self.assessment_session.identify_by
        if identify_by == AssessmentSession.IdentifyBy.STUDENT_ID and not self.student_id:
            raise ValidationError(
                'This session identifies students by student ID, so every '
                'roster row needs one.')
        if identify_by == AssessmentSession.IdentifyBy.FULL_NAME and not self.full_name:
            raise ValidationError(
                'This session identifies students by full name, so every '
                'roster row needs one.')


class Evaluator(models.Model):
    """Session-scoped identity for roster, invited, and anonymous evaluators.

    Classroom evaluations keep the legacy Student relation alongside this
    identity. Backfill and dual-write are implemented, but reads must remain on
    the legacy relation until parity is verified on representative data and a
    separately reviewed read switch is approved.
    """

    class Kind(models.TextChoices):
        ROSTER = 'roster', 'Roster participant'
        INVITED = 'invited', 'Invited evaluator'
        OPEN = 'open', 'Open-floor evaluator'

    assessment_session = models.ForeignKey(AssessmentSession,
        on_delete=models.CASCADE, related_name='evaluators')
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.ROSTER)
    student = models.ForeignKey(Student, null=True, blank=True,
        on_delete=models.CASCADE, related_name='evaluator_identities')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('assessment_session', 'student'),
                condition=models.Q(student__isnull=False), name='evaluator_roster_once'),
            models.CheckConstraint(
                condition=(models.Q(kind='roster', student__isnull=False)
                    | models.Q(kind__in=('invited', 'open'), student__isnull=True)),
                name='evaluator_kind_student_consistent'),
        ]

    def __str__(self):
        if self.kind == self.Kind.ROSTER and self.student_id:
            return str(self.student)
        return f'{self.get_kind_display()} #{self.pk or "new"}'

    def clean(self):
        if self.kind not in self.Kind.values:
            raise ValidationError({'kind': 'Choose a supported evaluator kind.'})
        if self.kind == self.Kind.ROSTER:
            if self.student_id is None:
                raise ValidationError({'student': 'Roster evaluators must link to a session student.'})
            if self.student.assessment_session_id != self.assessment_session_id:
                raise ValidationError({'student': 'A roster evaluator must belong to the same session.'})
        elif self.student_id is not None:
            raise ValidationError({'student': 'Invited and open evaluators cannot link to a roster student.'})


class PresentationGroup(models.Model):
    assessment_session = models.ForeignKey(AssessmentSession,
        on_delete=models.CASCADE, related_name='groups')
    is_practice = models.BooleanField(default=False,
        help_text='A practice presentation uses the rubric but never counts towards '
                  'results, rankings or participation penalties.')
    name = models.CharField(max_length=100)
    topic = models.CharField(max_length=200, blank=True)
    location = models.CharField(max_length=120, blank=True)
    order = models.PositiveIntegerField(default=0)
    max_size = models.PositiveIntegerField(null=True, blank=True,
        help_text='Optional cap on members (e.g. 2). Leave blank for no limit.')

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.pk and self.turns.exists():
            original = type(self).objects.filter(pk=self.pk).values_list('is_practice', flat=True).first()
            if original != self.is_practice:
                raise ValidationError({'is_practice': 'Practice status cannot change after this group has presented.'})

    def member_ids(self):
        return set(self.memberships.values_list('student_id', flat=True))

    def member_count(self):
        return self.memberships.count()

    def is_full(self):
        return self.max_size is not None and self.member_count() >= self.max_size

    def room_left(self):
        """None means unlimited."""
        if self.max_size is None:
            return None
        return max(self.max_size - self.member_count(), 0)


class GroupMembership(models.Model):
    """One student in one presenting group.

    A student belongs to exactly one group per session — Section 4 of the
    spec assumes this throughout (a group's members are *the* group a
    presentation belongs to, and self-vote exclusion only makes sense if
    membership is unambiguous). Enforced here, not just hidden in the UI's
    dropdown, so bulk import and any other write path can't create a second
    assignment either.
    """

    group = models.ForeignKey(PresentationGroup, on_delete=models.CASCADE,
        related_name='memberships')
    student = models.ForeignKey(Student, on_delete=models.CASCADE,
        related_name='group_memberships')

    class Meta:
        unique_together = ('group', 'student')

    def clean(self):
        if not (self.student_id and self.group_id):
            return
        conflict = (GroupMembership.objects
                    .filter(student_id=self.student_id,
                            group__assessment_session_id=self.group.assessment_session_id)
                    .exclude(group_id=self.group_id))
        if self.pk:
            conflict = conflict.exclude(pk=self.pk)
        existing = conflict.select_related('group').first()
        if existing:
            raise ValidationError(
                f'{self.student} is already in "{existing.group.name}" - a '
                f'student can only belong to one group per session.')

        if self.group.max_size is not None:
            current_count = self.group.memberships.exclude(pk=self.pk).count()
            if current_count >= self.group.max_size:
                raise ValidationError(
                    f'"{self.group.name}" is already full ({self.group.max_size} '
                    f'member{"s" if self.group.max_size != 1 else ""}).')


class PresentationTurn(models.Model):
    """One group's window as 'currently presenting'."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACTIVE = 'active', 'Active'
        CLOSED = 'closed', 'Closed'

    assessment_session = models.ForeignKey(AssessmentSession,
        on_delete=models.CASCADE, related_name='presentation_turns')
    group = models.ForeignKey(PresentationGroup, on_delete=models.CASCADE,
        related_name='turns')
    status = models.CharField(max_length=10, choices=Status.choices,
        default=Status.PENDING)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    voting_opened_at = models.DateTimeField(null=True, blank=True,
        help_text='When voting actually started. If null on an active turn, '
                  'the group is presenting but voting has not yet opened.')
    voting_ends_at = models.DateTimeField(null=True, blank=True,
        help_text='When set, voting for this turn auto-closes at this time '
                  'and the session automatically advances to the next group '
                  '(assessments.turns.auto_advance, scheduled via Celery). '
                  'Cleared while the session is paused — see '
                  'paused_remaining_seconds — so a stale scheduled task '
                  'that fires during the pause has nothing to act on.')
    paused_remaining_seconds = models.PositiveIntegerField(null=True, blank=True,
        help_text='Time left on the countdown at the moment voting was '
                  'paused, so resuming restores exactly that much time '
                  'instead of the countdown having kept running unseen.')
    presentation_ends_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_voting_open(self):
        """True if voting has opened and not expired or closed."""
        if self.status != self.Status.ACTIVE:
            return False
        if self.voting_opened_at is None:
            if self.presentation_ends_at:
                return False
            # Backwards compatibility: if voting_opened_at was not explicitly set,
            # consider open as long as the turn is active.
            return True
        from django.utils import timezone
        if self.voting_ends_at and self.voting_ends_at <= timezone.now():
            return False
        return True

    class Meta:
        ordering = ['-opened_at', '-id']

    def __str__(self):
        return f'{self.group} - {self.get_status_display()}'


class PrivacyNotice(models.Model):
    """One published, versioned participant privacy notice for a service."""

    service = models.CharField(max_length=20, default='classroom', db_index=True)
    version = models.CharField(max_length=40, default=DEFAULT_PRIVACY_NOTICE_VERSION)
    title = models.CharField(max_length=160, default=DEFAULT_PRIVACY_NOTICE_TITLE)
    body = models.TextField(default=DEFAULT_PRIVACY_NOTICE_BODY)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['service', '-created_at', '-pk']
        constraints = [
            models.UniqueConstraint(fields=['service', 'version'], name='unique_privacy_notice_version'),
            models.UniqueConstraint(fields=['service'], condition=models.Q(is_active=True),
                                    name='one_active_privacy_notice_per_service'),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            immutable = ('service', 'version', 'title', 'body')
            if any(getattr(original, field) != getattr(self, field) for field in immutable):
                raise ValidationError(
                    'Published privacy notice content is versioned and cannot be edited; create a new version.')
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.service} privacy notice {self.version}'


class ParticipationRecord(models.Model):
    """A student has joined a session — independent of whether they voted."""

    assessment_session = models.ForeignKey(AssessmentSession,
        on_delete=models.CASCADE, related_name='participation_records')
    student = models.ForeignKey(Student, on_delete=models.CASCADE,
        related_name='participation_records')
    privacy_notice = models.ForeignKey(PrivacyNotice, null=True, blank=True,
        on_delete=models.PROTECT, related_name='participation_records',
        help_text='The privacy notice version confirmed as read before this participant could score.')
    joined_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('assessment_session', 'student')

    def __str__(self):
        return f'{self.student} joined {self.assessment_session}'


class Evaluation(models.Model):
    """One student's submitted grading of one presentation turn.

    One row per (turn, evaluator, target) — ``target_student`` null for a
    group-scope submission, set for an individual-scope one (one row per
    teammate). The uniqueness constraint is what stops double voting.
    """

    presentation_turn = models.ForeignKey(PresentationTurn,
        on_delete=models.CASCADE, related_name='evaluations')
    evaluator = models.ForeignKey(Student, on_delete=models.CASCADE,
        related_name='evaluations_given')
    evaluator_record = models.ForeignKey(Evaluator, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='evaluation_rows')
    target_student = models.ForeignKey(Student, on_delete=models.CASCADE,
        null=True, blank=True, related_name='evaluations_received')
    submission_key = models.UUIDField(null=True, blank=True, unique=True,
        help_text='Retry key on the group evaluation anchoring one atomic submission.')
    submission_digest = models.CharField(max_length=64, blank=True, default='')
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('presentation_turn', 'evaluator', 'target_student')
        constraints = [models.UniqueConstraint(fields=['presentation_turn', 'evaluator'],
            condition=models.Q(target_student__isnull=True), name='one_group_submission_per_rater_turn')]

    def __str__(self):
        target = f' -> {self.target_student}' if self.target_student else ''
        return f'{self.evaluator} on {self.presentation_turn}{target}'


class EvaluationScore(models.Model):
    evaluation = models.ForeignKey(Evaluation, on_delete=models.CASCADE,
        related_name='scores')
    rubric_category = models.ForeignKey(RubricCategory, on_delete=models.CASCADE,
        related_name='scores')
    value = models.DecimalField(max_digits=6, decimal_places=2)
    max_value = models.DecimalField(max_digits=6, decimal_places=2,
        help_text='The category\'s weight when this score was given: ``value`` is out '
                  'of this. Results use value / max_value against the category\'s '
                  'current weight, so a weight edited later rescales past scores '
                  'instead of invalidating them.')

    class Meta:
        unique_together = ('evaluation', 'rubric_category')

    def clean(self):
        if self.value is None or self.value < 0:
            raise ValidationError('Score cannot be negative.')
        if self.max_value is None or self.max_value <= 0:
            raise ValidationError('This category has no weight to score against.')
        if self.value > self.max_value:
            name = self.rubric_category.name if self.rubric_category_id else 'this category'
            raise ValidationError(f'Score cannot exceed {self.max_value.normalize():f} for "{name}".')

    def save(self, *args, **kwargs):
        if self.max_value is None:
            self.max_value = self.rubric_category.weight
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.rubric_category.name}: {self.value}'


class SessionTransfer(models.Model):
    """One hand-over of a session from one lecturer to another.

    A log, not a permission: it records who gave what to whom and when, so a
    session that changed hands is never a mystery. ``by_user`` is empty when an
    operator did it from the command line.
    """

    session = models.ForeignKey(AssessmentSession, on_delete=models.CASCADE,
        related_name='transfers')
    from_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+')
    to_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+')
    by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+')
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return f'{self.session_id}: {self.from_user_id} -> {self.to_user_id}'


class SessionChange(models.Model):
    """One edit made to a session's setup after it went live.

    Results are worked out from the setup as it stands, so an edit made while
    people are grading is part of the record: this says who changed what, and when.
    """

    session = models.ForeignKey(AssessmentSession, on_delete=models.CASCADE,
        related_name='changes')
    by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+')
    summary = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return f'{self.session_id}: {self.summary}'


class SessionTemplate(OrganizationScopedModel):
    """An owner's reusable service configuration, with no participants or results."""
    service = models.CharField(max_length=20, default='classroom', db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name='assessment_templates')
    name = models.CharField(max_length=150)
    description = models.CharField(max_length=500, blank=True)
    identify_by = models.CharField(max_length=20, choices=AssessmentSession.IdentifyBy.choices,
        default=AssessmentSession.IdentifyBy.FULL_NAME)
    ungraded_penalty = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('1.00'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))])
    instructions = models.TextField(blank=True, default='', max_length=4000)
    default_presentation_seconds = models.PositiveIntegerField(null=True, blank=True)
    default_turn_seconds = models.PositiveIntegerField(null=True, blank=True)
    transition_gap_seconds = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OwnedManager()

    class Meta:
        ordering = ['name', 'pk']

    def __str__(self):
        return self.name


class TemplateRubricCategory(models.Model):
    template = models.ForeignKey(SessionTemplate, on_delete=models.CASCADE, related_name='rubric_categories')
    name = models.CharField(max_length=150)
    description = models.CharField(max_length=255, blank=True)
    scope = models.CharField(max_length=10, choices=RubricCategory.Scope.choices, default=RubricCategory.Scope.GROUP)
    weight = models.DecimalField(max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01')), MaxValueValidator(TOTAL_WEIGHT)])
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['scope', 'order', 'pk']

    def __str__(self):
        return self.name


class TemplateLevelDescriptor(models.Model):
    """A rubric level stored with its reusable template snapshot."""
    rubric_category = models.ForeignKey(TemplateRubricCategory, on_delete=models.CASCADE,
        related_name='level_descriptors')
    score = models.DecimalField(max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(TOTAL_WEIGHT)])
    description = models.CharField(max_length=500)

    class Meta:
        ordering = ['score', 'pk']
        constraints = [models.UniqueConstraint(fields=['rubric_category', 'score'],
            name='unique_template_level_score')]

    def clean(self):
        if (self.rubric_category_id and self.score is not None
                and self.score > self.rubric_category.weight):
            raise ValidationError({'score': 'A descriptor score cannot exceed the criterion weight.'})
        if not self.description or not self.description.strip():
            raise ValidationError({'description': 'Enter guidance for this score level.'})


class ClassroomPoll(models.Model):
    """Shared poll definition used by Classroom and the standalone Polls service."""

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        OPEN = 'open', 'Open'
        CLOSED = 'closed', 'Closed'
        CONFIRMED = 'confirmed', 'Decision confirmed'
        CANCELLED = 'cancelled', 'Cancelled'

    result_counts = models.JSONField(default=dict, blank=True, editable=False)
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    assessment_session = models.ForeignKey(AssessmentSession, on_delete=models.CASCADE,
        related_name='polls', null=True, blank=True)
    organization_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='standalone_polls_created')
    standalone = models.BooleanField(default=False, db_index=True)
    question = models.CharField(max_length=250)
    description = models.TextField(blank=True, max_length=4000)
    allow_multiple = models.BooleanField(default=False)
    personal_links_only = models.BooleanField(default=False)
    allow_abstain = models.BooleanField(default=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    deadline = models.DateTimeField(null=True, blank=True)
    timezone_name = models.CharField(max_length=64, default='Africa/Dar_es_Salaam')
    confirmed_option = models.ForeignKey('ClassroomPollOption', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+')
    decision_note = models.TextField(blank=True, max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [models.CheckConstraint(
            condition=(models.Q(standalone=True, assessment_session__isnull=True,
                                organization_id__isnull=False)
                       | models.Q(standalone=False, assessment_session__isnull=False)),
            name='classroom_poll_session_or_standalone_scope')]

    @property
    def accepting_responses(self):
        return self.status == self.Status.OPEN and (self.deadline is None or timezone.now() < self.deadline)

    def clean(self):
        if self.standalone:
            if self.assessment_session_id:
                raise ValidationError({'assessment_session': 'Standalone polls cannot belong to a Classroom session.'})
            if not self.organization_id or not self.created_by_id:
                raise ValidationError('Standalone polls need an organization and owner.')
        if self.assessment_session_id and self.assessment_session.service != 'classroom':
            raise ValidationError('Polls belong to Classroom sessions.')
        if self.confirmed_option_id and self.confirmed_option.poll_id != self.pk:
            raise ValidationError({'confirmed_option': 'Choose an option from this poll.'})


class ClassroomPollQuestion(models.Model):
    """A question within a poll; each question has its own answer options."""
    poll = models.ForeignKey(ClassroomPoll, on_delete=models.CASCADE, related_name='questions')
    prompt = models.CharField(max_length=250)
    allow_multiple = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    confirmed_option = models.ForeignKey('ClassroomPollOption', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='confirmed_for_questions')
    decision_note = models.TextField(blank=True, max_length=2000)

    class Meta:
        ordering = ['order', 'pk']


class ClassroomPollOption(models.Model):
    poll = models.ForeignKey(ClassroomPoll, on_delete=models.CASCADE, related_name='options')
    question = models.ForeignKey(ClassroomPollQuestion, null=True, blank=True,
        on_delete=models.CASCADE, related_name='options')
    label = models.CharField(max_length=250)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'pk']
        constraints = [
            models.UniqueConstraint(fields=['question', 'label'],
                condition=models.Q(question__isnull=False),
                name='classroom_poll_question_option_once'),
            models.UniqueConstraint(fields=['poll', 'label'],
                condition=models.Q(question__isnull=True),
                name='classroom_poll_legacy_option_once'),
        ]


class ClassroomPollVoter(models.Model):
    """Publication-time electorate; personal links are revocable bearer credentials."""
    poll = models.ForeignKey(ClassroomPoll, on_delete=models.CASCADE, related_name='voters')
    student = models.ForeignKey(Student, null=True, on_delete=models.SET_NULL, related_name='poll_invitations')
    name = models.CharField(max_length=150)
    identifier = models.CharField(max_length=150)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        ordering = ['name', 'pk']
        constraints = [models.UniqueConstraint(fields=['poll', 'student'], name='classroom_poll_student_once')]


class ClassroomPollResponse(models.Model):
    voter = models.OneToOneField(ClassroomPollVoter, on_delete=models.CASCADE, related_name='response')
    abstained = models.BooleanField(default=False)
    answers = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    options = models.ManyToManyField(ClassroomPollOption, related_name='responses', blank=True)


class AnonymousClassroomPollResponse(models.Model):
    """One anonymous browser ballot; no roster identity or response-status roster is stored."""
    poll = models.ForeignKey(ClassroomPoll, on_delete=models.CASCADE, related_name='anonymous_responses')
    token = models.UUIDField(default=uuid.uuid4, editable=False)
    answers = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('poll', 'token'), name='anonymous_poll_browser_once')]


class StandalonePollAudit(models.Model):
    poll = models.ForeignKey(ClassroomPoll, on_delete=models.CASCADE, related_name='standalone_audit')
    by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+')
    summary = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
