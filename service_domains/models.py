"""Organization-scoped domain records for the four planned LiveGrade services.

These records deliberately do not reuse Classroom's roster-evaluator or score
models. Each service's workflow can evolve behind its own release gate.
"""

import hashlib
import re
import secrets
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.core import signing
from django.utils import timezone

from accounts.scoping import OrganizationScopedModel, OwnedManager, OwnedQuerySet
from assessments.level_descriptors import MAX_LEVEL_DESCRIPTION


SHOWCASE_DEVICE_TOKEN_MAX_AGE = 7 * 24 * 60 * 60
SHOWCASE_BRAND_ACCENTS = {
    'blue': '#174c7b', 'green': '#17683a', 'purple': '#5b3f8c', 'red': '#8b2432',
}


class OwnedDomainRecord(OrganizationScopedModel):
    """Common organization and owner boundary for service-owned records."""

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='%(app_label)s_%(class)s_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OwnedManager()

    def clean(self):
        super().clean()
        # Parent records always inherit the same tenant and owner boundary.
        # Keep this validation generic so new service child rows cannot
        # accidentally bind an organization-A child to an organization-B root.
        for field in self._meta.fields:
            if not isinstance(field, models.ForeignKey) or not field.remote_field.model:
                continue
            parent_model = field.remote_field.model
            if parent_model._meta.abstract or not issubclass(parent_model, OwnedDomainRecord):
                continue
            parent = getattr(self, field.name, None)
            if parent is None:
                continue
            if parent.organization_id != self.organization_id:
                raise ValidationError({field.name: 'Related records must belong to the same organization.'})
            if parent.created_by_id != self.created_by_id:
                raise ValidationError({field.name: 'Related records must have the same owner.'})

    class Meta:
        abstract = True


class PanelHearing(OwnedDomainRecord):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        OPEN = 'open', 'Open'
        REVIEW = 'review', 'Under review'
        SIGNED_OFF = 'signed_off', 'Signed off'
        RELEASED = 'released', 'Released'

    class Aggregation(models.TextChoices):
        MEAN = 'mean', 'Mean'
        MEDIAN = 'median', 'Median'
        TRIMMED_MEAN = 'trimmed_mean', 'Trimmed mean'

    class Workflow(models.TextChoices):
        SCORED = 'scored', 'Independent rubric scoring'
        OUTCOME = 'outcome', 'Chair-recorded outcome with rationale'
        VOTE = 'vote', 'Private panel vote followed by chair outcome'
        HYBRID = 'hybrid', 'Rubric scoring, panel vote and chair outcome'
        REVIEW = 'review', 'Independent written reviews and chair outcome'
        REVIEW_VOTE = 'review_vote', 'Written reviews, private vote and chair outcome'

    name = models.CharField(max_length=160)
    hearing_date = models.DateField(null=True, blank=True)
    venue = models.CharField(max_length=200, blank=True)
    workflow = models.CharField(max_length=20, choices=Workflow.choices, default=Workflow.SCORED)
    workflow_version = models.PositiveSmallIntegerField(default=1, editable=False)
    workflow_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    active_candidate = models.ForeignKey('PanelCandidate', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='active_for_hearings')
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    aggregation = models.CharField(max_length=16, choices=Aggregation.choices, default=Aggregation.MEAN)
    minimum_examiners = models.PositiveSmallIntegerField(default=1,
        validators=[MinValueValidator(1), MaxValueValidator(50)])
    trim_fraction = models.DecimalField(max_digits=4, decimal_places=3,
        null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(0.499)])
    signed_off_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if (self.active_candidate_id and self.pk
                and self.active_candidate.hearing_id != self.pk):
            raise ValidationError({'active_candidate': 'The active candidate must belong to this hearing.'})
        if self.status in (self.Status.SIGNED_OFF, self.Status.RELEASED) and not self.signed_off_at:
            raise ValidationError({'signed_off_at': 'Signed-off results require a recorded sign-off time.'})
        if self.status == self.Status.RELEASED and not self.released_at:
            raise ValidationError({'released_at': 'Released results require a recorded release time.'})
        if self.aggregation == self.Aggregation.TRIMMED_MEAN and self.trim_fraction is None:
            raise ValidationError({'trim_fraction': 'Trimmed mean requires an explicit trim fraction.'})


class PanelScoringPanel(OwnedDomainRecord):
    """A configured examiner group contributing a disclosed share of results."""

    class AdmissionMethod(models.TextChoices):
        INVITED = 'invited', 'Invited examiners'

    hearing = models.ForeignKey(PanelHearing, on_delete=models.CASCADE, related_name='panels')
    name = models.CharField(max_length=120)
    admission_method = models.CharField(max_length=16, choices=AdmissionMethod.choices,
        default=AdmissionMethod.INVITED)
    share = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('100.00'),
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    configuration_managed = models.BooleanField(default=False)
    aggregation = models.CharField(max_length=16, choices=PanelHearing.Aggregation.choices,
        default=PanelHearing.Aggregation.MEAN)
    minimum_examiners = models.PositiveSmallIntegerField(default=1,
        validators=[MinValueValidator(1), MaxValueValidator(50)])
    trim_fraction = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(0.499)])

    class Meta:
        ordering = ('created_at', 'pk')
        constraints = [models.UniqueConstraint(fields=('hearing', 'name'),
            name='panel_scoring_panel_name_unique')]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.aggregation == PanelHearing.Aggregation.TRIMMED_MEAN and self.trim_fraction is None:
            raise ValidationError({'trim_fraction': 'Trimmed mean requires an explicit trim fraction.'})


class PanelCandidate(OwnedDomainRecord):
    hearing = models.ForeignKey(PanelHearing, on_delete=models.CASCADE, related_name='candidates')
    source_group = models.ForeignKey('assessments.PresentationGroup', null=True, blank=True,
        on_delete=models.PROTECT, related_name='panel_candidates',
        help_text='Optional Classroom group this Panel entry represents.')
    display_name = models.CharField(max_length=160)
    reference = models.CharField(max_length=80, blank=True)
    order = models.PositiveIntegerField(default=0)
    scheduled_time = models.TimeField(null=True, blank=True)

    class Meta:
        ordering = ('order', 'pk')
        constraints = [
            models.UniqueConstraint(fields=('hearing', 'reference'),
                condition=~Q(reference=''), name='panel_candidate_ref_unique'),
            models.UniqueConstraint(fields=('hearing', 'source_group'),
                condition=Q(source_group__isnull=False), name='panel_candidate_source_group_once'),
        ]

    def __str__(self):
        return self.display_name

    def clean(self):
        super().clean()
        if self.source_group_id:
            session = self.source_group.assessment_session
            if session.service != 'classroom':
                raise ValidationError({'source_group': 'A linked source must be a Classroom group.'})
            if session.organization_id != self.organization_id:
                raise ValidationError({'source_group': 'The source group must belong to this organization.'})
            if session.created_by_id != self.created_by_id:
                raise ValidationError({'source_group': 'The source group must have the same owner.'})


class PanelCriterion(OwnedDomainRecord):
    weight = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal('1'),
        validators=[MinValueValidator(Decimal('0.01')), MaxValueValidator(Decimal('100'))])
    hearing = models.ForeignKey(PanelHearing, on_delete=models.CASCADE, related_name='criteria')
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, max_length=1000)
    order = models.PositiveIntegerField(default=0)
    panels = models.ManyToManyField('PanelScoringPanel', related_name='criteria', blank=True)

    class Meta:
        ordering = ('order', 'pk')
        constraints = [models.UniqueConstraint(fields=('hearing', 'name'), name='panel_criterion_name_unique')]

    def __str__(self):
        return self.name


class PanelCriterionLevel(OwnedDomainRecord):
    """Optional owner-authored score anchors; scoring remains continuous."""
    criterion = models.ForeignKey(PanelCriterion, on_delete=models.CASCADE,
        related_name='levels')
    score = models.DecimalField(max_digits=5, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    description = models.CharField(max_length=MAX_LEVEL_DESCRIPTION)

    class Meta:
        ordering = ('score', 'pk')
        constraints = [models.UniqueConstraint(fields=('criterion', 'score'),
            name='panel_criterion_level_score_unique')]

    def clean(self):
        super().clean()
        if not self.description or not self.description.strip():
            raise ValidationError({'description': 'Enter guidance for this score level.'})


class PanelInvitation(OwnedDomainRecord):
    class DeliveryStatus(models.TextChoices):
        PENDING = 'pending', 'Queued'
        SENDING = 'sending', 'Sending'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Delivery failed'
        UNKNOWN = 'unknown', 'Delivery status unknown'

    hearing = models.ForeignKey(PanelHearing, on_delete=models.CASCADE, related_name='invitations')
    panel = models.ForeignKey(PanelScoringPanel, on_delete=models.PROTECT, related_name='invitations',
        null=True, blank=True)
    email = models.EmailField(max_length=254)
    token_digest = models.CharField(max_length=64, unique=True, editable=False, null=True, blank=True)
    device_digest = models.CharField(max_length=64, blank=True, default='', editable=False)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True,
        help_text='When the invited examiner confirmed this link in its bound browser.')
    delivery_status = models.CharField(max_length=12, choices=DeliveryStatus.choices,
        default=DeliveryStatus.UNKNOWN)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivery_attempts = models.PositiveSmallIntegerField(default=0)
    delivery_error = models.CharField(max_length=32, blank=True, default='')

    @staticmethod
    def digest_token(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def clean(self):
        super().clean()
        if self.panel_id and self.hearing_id != self.panel.hearing_id:
            raise ValidationError({'panel': 'The panel must belong to the invited hearing.'})
        if self.expires_at and self.expires_at <= timezone.now():
            raise ValidationError({'expires_at': 'Invitation expiry must be in the future.'})

    def usable_at(self, at=None):
        at = at or timezone.now()
        return (self.revoked_at is None and self.declined_at is None and self.accepted_at is None
            and self.expires_at > at)


class PanelInvitationAttemptQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Panel invitation attempts are append-only.')

    def delete(self):
        raise ValidationError('Panel invitation attempts are append-only.')


class PanelInvitationAttemptManager(OwnedManager.from_queryset(PanelInvitationAttemptQuerySet)):
    pass


class PanelInvitationAttempt(OwnedDomainRecord):
    """Minimal durable activity record for a known Panel invitation."""

    class Action(models.TextChoices):
        OPEN = 'open', 'Open invitation'
        CONFLICT = 'conflict', 'Declare conflict'
        SCORE = 'score', 'Submit score'
        ABSTAIN = 'abstain', 'Abstain from criterion'
        VOTE = 'vote', 'Submit private vote'
        REVIEW = 'review', 'Submit written review'
        INVALID = 'invalid', 'Invalid action'

    class Outcome(models.TextChoices):
        ACCEPTED = 'accepted', 'Accepted'
        REJECTED = 'rejected', 'Rejected'
        RATE_LIMITED = 'rate_limited', 'Rate limited'
        UNAVAILABLE = 'unavailable', 'Temporarily unavailable'

    class Reason(models.TextChoices):
        SUBMITTED = 'submitted', 'Submitted'
        INVALID_ACTION = 'invalid_action', 'Invalid action'
        INVALID_TARGET = 'invalid_target', 'Invalid target'
        NOT_ALLOWED = 'not_allowed', 'Not allowed'
        INVALID_INPUT = 'invalid_input', 'Invalid input'
        DUPLICATE = 'duplicate', 'Duplicate submission'
        INVITATION_UNUSABLE = 'invitation_unusable', 'Invitation expired or revoked'
        HEARING_CLOSED = 'hearing_closed', 'Hearing not accepting submissions'
        RATE_LIMIT = 'rate_limit', 'Request limit exceeded'
        THROTTLE_UNAVAILABLE = 'throttle_unavailable', 'Rate limiter unavailable'
        DEVICE_MISMATCH = 'device_mismatch', 'Invitation used from another browser'

    invitation = models.ForeignKey(PanelInvitation, on_delete=models.PROTECT,
        related_name='attempts')
    action = models.CharField(max_length=12, choices=Action.choices)
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    reason = models.CharField(max_length=32, choices=Reason.choices)

    objects = PanelInvitationAttemptManager()

    class Meta:
        ordering = ('created_at', 'pk')

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Panel invitation attempts are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Panel invitation attempts are append-only.')


class PanelConflict(OwnedDomainRecord):
    invitation = models.ForeignKey(PanelInvitation, on_delete=models.CASCADE, related_name='conflicts')
    candidate = models.ForeignKey(PanelCandidate, on_delete=models.CASCADE, related_name='conflicts')
    reason = models.TextField(max_length=1000)
    declared_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('invitation', 'candidate'), name='panel_conflict_once')]

    def clean(self):
        super().clean()
        if self.invitation_id and self.candidate_id and self.invitation.hearing_id != self.candidate.hearing_id:
            raise ValidationError({'candidate': 'The candidate must belong to the invited hearing.'})
        if not self.reason or not self.reason.strip():
            raise ValidationError({'reason': 'A conflict reason is required.'})


class PanelScore(OwnedDomainRecord):
    invitation = models.ForeignKey(PanelInvitation, on_delete=models.PROTECT, related_name='scores')
    candidate = models.ForeignKey(PanelCandidate, on_delete=models.CASCADE, related_name='panel_scores')
    criterion = models.ForeignKey(PanelCriterion, on_delete=models.PROTECT, related_name='scores')
    score = models.DecimalField(max_digits=5, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    comment = models.TextField(blank=True, max_length=2000)
    comment_released_to_entrant = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('invitation', 'candidate', 'criterion'),
            name='panel_score_once'),
            models.CheckConstraint(condition=Q(score__gte=0, score__lte=100), name='panel_score_range')]

    def clean(self):
        super().clean()
        if self.invitation_id and self.candidate_id and self.invitation.hearing_id != self.candidate.hearing_id:
            raise ValidationError({'candidate': 'The candidate must belong to the invited hearing.'})
        if self.invitation_id and self.criterion_id and self.invitation.hearing_id != self.criterion.hearing_id:
            raise ValidationError({'criterion': 'The criterion must belong to the invited hearing.'})


class PanelAbstentionQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Panel abstentions are immutable.')

    def delete(self):
        raise ValidationError('Panel abstentions are immutable.')


class PanelAbstentionManager(OwnedManager.from_queryset(PanelAbstentionQuerySet)):
    pass


class PanelAbstention(OwnedDomainRecord):
    """An examiner's immutable decision not to score one criterion."""

    invitation = models.ForeignKey(PanelInvitation, on_delete=models.PROTECT,
        related_name='abstentions')
    candidate = models.ForeignKey(PanelCandidate, on_delete=models.PROTECT,
        related_name='panel_abstentions')
    criterion = models.ForeignKey(PanelCriterion, on_delete=models.PROTECT,
        related_name='panel_abstentions')
    reason = models.CharField(max_length=1000, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

    objects = PanelAbstentionManager()

    class Meta:
        ordering = ('submitted_at', 'pk')
        constraints = [models.UniqueConstraint(fields=('invitation', 'candidate', 'criterion'),
            name='panel_abstention_once')]

    def clean(self):
        super().clean()
        if self.invitation_id and self.candidate_id and self.invitation.hearing_id != self.candidate.hearing_id:
            raise ValidationError({'candidate': 'The candidate must belong to the invited hearing.'})
        if self.invitation_id and self.criterion_id and self.invitation.hearing_id != self.criterion.hearing_id:
            raise ValidationError({'criterion': 'The criterion must belong to the invited hearing.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Panel abstentions are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Panel abstentions are immutable.')


class PanelWrittenReviewQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Submitted reviewer reports are immutable.')

    def delete(self):
        raise ValidationError('Submitted reviewer reports are immutable.')


class PanelWrittenReviewManager(OwnedManager.from_queryset(PanelWrittenReviewQuerySet)):
    pass


class PanelWrittenReview(OwnedDomainRecord):
    """One independent, private reviewer report for one candidate."""

    hearing = models.ForeignKey(PanelHearing, on_delete=models.CASCADE, related_name='written_reviews')
    invitation = models.ForeignKey(PanelInvitation, on_delete=models.PROTECT, related_name='written_reviews')
    candidate = models.ForeignKey(PanelCandidate, on_delete=models.PROTECT, related_name='written_reviews')
    report = models.TextField(max_length=6000)
    revision = models.PositiveSmallIntegerField(default=1)
    revision_reason = models.CharField(max_length=500, blank=True)

    objects = PanelWrittenReviewManager()

    class Meta:
        ordering = ('candidate__order', 'created_at', 'pk')
        constraints = [models.UniqueConstraint(fields=('invitation', 'candidate', 'revision'),
            name='panel_written_review_revision_unique')]

    def clean(self):
        super().clean()
        if self.invitation_id and self.candidate_id and self.invitation.hearing_id != self.candidate.hearing_id:
            raise ValidationError({'candidate': 'The candidate must belong to the invited hearing.'})
        if self.hearing_id and self.candidate_id and self.candidate.hearing_id != self.hearing_id:
            raise ValidationError({'candidate': 'The candidate must belong to this hearing.'})
        if self.hearing_id and self.invitation_id and self.invitation.hearing_id != self.hearing_id:
            raise ValidationError({'invitation': 'The invitation must belong to this hearing.'})
        if not self.report.strip():
            raise ValidationError({'report': 'Write an evidence-based review before submitting.'})
        if self.revision > 1 and not self.revision_reason.strip():
            raise ValidationError({'revision_reason': 'Explain why this report is being corrected.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Submitted reviewer reports are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Submitted reviewer reports are immutable.')


class PanelDecisionQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Panel decisions are append-only.')

    def delete(self):
        raise ValidationError('Panel decisions are append-only.')


class PanelDecisionManager(OwnedManager.from_queryset(PanelDecisionQuerySet)):
    pass


class PanelDecision(OwnedDomainRecord):
    """Chair-recorded recommendation or final outcome for a single candidate."""

    class Outcome(models.TextChoices):
        ACCEPT = 'accept', 'Accept / pass'
        CONDITIONAL = 'conditional', 'Accept with conditions / revisions'
        DEFER = 'defer', 'Defer / further review'
        REJECT = 'reject', 'Do not accept'
        OTHER = 'other', 'Other outcome'

    hearing = models.ForeignKey(PanelHearing, on_delete=models.CASCADE, related_name='decisions')
    candidate = models.ForeignKey(PanelCandidate, on_delete=models.PROTECT, related_name='decisions')
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    rationale = models.TextField(max_length=3000)
    conditions = models.TextField(max_length=2000, blank=True)
    dissent = models.TextField(max_length=2000, blank=True)
    revision = models.PositiveSmallIntegerField(default=1)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='panel_decisions_made')

    objects = PanelDecisionManager()

    class Meta:
        ordering = ('candidate__order', 'candidate_id', 'revision')
        constraints = [models.UniqueConstraint(fields=('candidate', 'revision'),
            name='panel_decision_candidate_revision_unique')]

    def clean(self):
        super().clean()
        if self.hearing_id and self.candidate_id and self.candidate.hearing_id != self.hearing_id:
            raise ValidationError({'candidate': 'The candidate must belong to this hearing.'})
        if self.hearing_id and PanelHearing.objects.filter(pk=self.hearing_id).exclude(
                workflow__in=(PanelHearing.Workflow.OUTCOME, PanelHearing.Workflow.VOTE,
                              PanelHearing.Workflow.HYBRID, PanelHearing.Workflow.REVIEW,
                              PanelHearing.Workflow.REVIEW_VOTE)).exists():
            raise ValidationError({'hearing': 'This workflow does not collect chair outcomes.'})
        if not self.rationale.strip():
            raise ValidationError({'rationale': 'Record the reasons for this decision.'})
        if self.outcome == self.Outcome.CONDITIONAL and not self.conditions.strip():
            raise ValidationError({'conditions': 'Describe the required conditions or revisions.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Panel decisions are append-only; record a new revision instead.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Panel decisions are append-only.')


class PanelVoteQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Panel votes are immutable after submission.')

    def delete(self):
        raise ValidationError('Panel votes are immutable after submission.')


class PanelVoteManager(OwnedManager.from_queryset(PanelVoteQuerySet)):
    pass


class PanelVote(OwnedDomainRecord):
    """One private, auditable ballot per invited examiner and candidate."""

    class Choice(models.TextChoices):
        SUPPORT = 'support', 'Support recommendation'
        OPPOSE = 'oppose', 'Do not support recommendation'
        ABSTAIN = 'abstain', 'Abstain'

    hearing = models.ForeignKey(PanelHearing, on_delete=models.CASCADE, related_name='votes')
    invitation = models.ForeignKey(PanelInvitation, on_delete=models.PROTECT, related_name='votes')
    candidate = models.ForeignKey(PanelCandidate, on_delete=models.PROTECT, related_name='votes')
    choice = models.CharField(max_length=12, choices=Choice.choices)

    objects = PanelVoteManager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=('invitation', 'candidate'),
            name='panel_vote_invitation_candidate_unique')]

    def clean(self):
        super().clean()
        if self.invitation_id and self.candidate_id and self.invitation.hearing_id != self.candidate.hearing_id:
            raise ValidationError({'candidate': 'The candidate must belong to the invited hearing.'})
        if self.invitation_id and self.hearing_id and self.invitation.hearing_id != self.hearing_id:
            raise ValidationError({'invitation': 'The invitation must belong to this hearing.'})
        if self.hearing_id and PanelHearing.objects.filter(pk=self.hearing_id).exclude(
                workflow__in=(PanelHearing.Workflow.VOTE, PanelHearing.Workflow.HYBRID,
                              PanelHearing.Workflow.REVIEW_VOTE)).exists():
            raise ValidationError({'hearing': 'This workflow does not collect panel votes.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Submitted panel votes are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Submitted panel votes are immutable.')


class PanelResultSnapshotQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Panel result snapshots are immutable.')

    def delete(self):
        raise ValidationError('Panel result snapshots are immutable.')


class PanelResultSnapshotManager(OwnedManager.from_queryset(PanelResultSnapshotQuerySet)):
    pass


class PanelResultSnapshot(OwnedDomainRecord):
    """Disclosed examiner aggregate at review; contains no examiner identities."""

    hearing = models.ForeignKey(PanelHearing, on_delete=models.PROTECT, related_name='result_snapshots')
    candidate = models.ForeignKey(PanelCandidate, on_delete=models.PROTECT, related_name='result_snapshots')
    criterion = models.ForeignKey(PanelCriterion, on_delete=models.PROTECT, related_name='result_snapshots')
    score = models.DecimalField(max_digits=9, decimal_places=6,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    rater_count = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    below_minimum = models.BooleanField(default=False)
    aggregation = models.CharField(max_length=16, choices=PanelHearing.Aggregation.choices)
    trim_fraction = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    panel_methodology = models.JSONField(default=list, blank=True)

    objects = PanelResultSnapshotManager()

    class Meta:
        ordering = ('candidate__order', 'criterion__order', 'pk')
        constraints = [models.UniqueConstraint(fields=('hearing', 'candidate', 'criterion'),
            name='panel_result_snapshot_once')]

    def clean(self):
        super().clean()
        if self.candidate_id and self.candidate.hearing_id != self.hearing_id:
            raise ValidationError({'candidate': 'The candidate must belong to this hearing.'})
        if self.criterion_id and self.criterion.hearing_id != self.hearing_id:
            raise ValidationError({'criterion': 'The criterion must belong to this hearing.'})
        if self.aggregation == PanelHearing.Aggregation.TRIMMED_MEAN and self.trim_fraction is None:
            raise ValidationError({'trim_fraction': 'A trimmed result needs its configured trim fraction.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Panel result snapshots are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Panel result snapshots are immutable.')


class PanelResultAdjustmentQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Panel result adjustments are append-only.')

    def delete(self):
        raise ValidationError('Panel result adjustments are append-only.')


class PanelResultAdjustmentManager(OwnedManager.from_queryset(PanelResultAdjustmentQuerySet)):
    pass


class PanelResultAdjustment(OwnedDomainRecord):
    """A reasoned score correction that preserves the original snapshot."""

    snapshot = models.ForeignKey(PanelResultSnapshot, on_delete=models.PROTECT,
        related_name='adjustments')
    adjusted_score = models.DecimalField(max_digits=9, decimal_places=6,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    reason = models.TextField(max_length=1000)
    revision = models.PositiveIntegerField(default=1)
    supersedes = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT,
        related_name='corrections')
    adjusted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='panel_result_adjustments')
    adjusted_at = models.DateTimeField(auto_now_add=True)

    objects = PanelResultAdjustmentManager()

    class Meta:
        ordering = ('snapshot_id', 'revision')
        constraints = [models.UniqueConstraint(fields=('snapshot', 'revision'),
            name='panel_result_adjustment_revision_unique')]

    def clean(self):
        super().clean()
        if not self.reason or not self.reason.strip():
            raise ValidationError({'reason': 'A reason is required for every result adjustment.'})
        if self.supersedes_id:
            previous = self.supersedes
            if previous.snapshot_id != self.snapshot_id or previous.revision + 1 != self.revision:
                raise ValidationError({'supersedes': 'An adjustment must supersede the latest same-result revision.'})
        elif self.revision != 1:
            raise ValidationError({'revision': 'The first adjustment revision must be one.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Panel result adjustments are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Panel result adjustments are append-only.')


class PanelCommentRedactionQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Panel comment redactions are append-only.')

    def delete(self):
        raise ValidationError('Panel comment redactions are append-only.')


class PanelCommentRedactionManager(OwnedManager.from_queryset(PanelCommentRedactionQuerySet)):
    pass


class PanelCommentRedaction(OwnedDomainRecord):
    """Append-only proof that an examiner comment was removed during review."""

    hearing = models.ForeignKey(PanelHearing, on_delete=models.PROTECT,
        related_name='comment_redactions')
    score = models.OneToOneField(PanelScore, on_delete=models.PROTECT,
        related_name='comment_redaction')
    original_comment_digest = models.CharField(max_length=64, editable=False)
    reason = models.TextField(max_length=1000)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='panel_comment_redactions')
    redacted_at = models.DateTimeField(auto_now_add=True)

    objects = PanelCommentRedactionManager()

    class Meta:
        ordering = ('redacted_at', 'pk')

    def clean(self):
        super().clean()
        if self.score_id and (self.score.candidate.hearing_id != self.hearing_id
                              or self.score.criterion.hearing_id != self.hearing_id):
            raise ValidationError({'score': 'The moderated score must belong to this hearing.'})
        if not self.reason or not self.reason.strip():
            raise ValidationError({'reason': 'A reason is required for every comment redaction.'})
        if not re.fullmatch(r'[0-9a-f]{64}', self.original_comment_digest or ''):
            raise ValidationError({'original_comment_digest': 'The comment digest is invalid.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Panel comment redactions are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Panel comment redactions are append-only.')


class PanelEntrantResultLinkQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Panel result links can only be revoked through the audited operation.')

    def delete(self):
        raise ValidationError('Panel result links are retained as revocation history.')


class PanelEntrantResultLinkManager(OwnedManager.from_queryset(PanelEntrantResultLinkQuerySet)):
    pass


class PanelEntrantResultLink(OwnedDomainRecord):
    """Revocable opaque capability showing one entrant only their released results."""

    hearing = models.ForeignKey(PanelHearing, on_delete=models.PROTECT,
        related_name='entrant_result_links')
    candidate = models.ForeignKey(PanelCandidate, on_delete=models.PROTECT,
        related_name='entrant_result_links')
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(auto_now_add=True)

    objects = PanelEntrantResultLinkManager()

    class Meta:
        ordering = ('issued_at', 'pk')

    @staticmethod
    def digest_token(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def clean(self):
        super().clean()
        if self.candidate_id and self.candidate.hearing_id != self.hearing_id:
            raise ValidationError({'candidate': 'The entrant must belong to this hearing.'})
        if self.hearing_id and (self.hearing.status != PanelHearing.Status.RELEASED
                                or not self.hearing.released_at):
            raise ValidationError({'hearing': 'Entrant links are available only after results are released.'})
        if not re.fullmatch(r'[0-9a-f]{64}', self.token_digest or ''):
            raise ValidationError({'token_digest': 'The result-link token digest is invalid.'})

    def save(self, *args, **kwargs):
        if self._state.adding:
            return super().save(*args, **kwargs)
        previous = type(self).objects.get(pk=self.pk)
        if (previous.revoked_at is not None or self.revoked_at is None
                or previous.hearing_id != self.hearing_id
                or previous.candidate_id != self.candidate_id
                or previous.token_digest != self.token_digest
                or previous.organization_id != self.organization_id
                or previous.created_by_id != self.created_by_id):
            raise ValidationError('Panel result links may only be revoked once through the audited operation.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Panel result links are retained as revocation history.')


class PanelLiveDisplayLink(OwnedDomainRecord):
    """Revocable bearer link for aggregate-only live Panel progress."""

    hearing = models.ForeignKey(PanelHearing, on_delete=models.PROTECT,
        related_name='live_display_links')
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-issued_at', 'pk')

    @staticmethod
    def digest_token(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def usable_at(self, at=None):
        return (self.revoked_at is None
                and self.hearing.status != PanelHearing.Status.DRAFT)

    def clean(self):
        super().clean()
        if not re.fullmatch(r'[0-9a-f]{64}', self.token_digest or ''):
            raise ValidationError({'token_digest': 'The live display token is invalid.'})

    def save(self, *args, **kwargs):
        if self._state.adding:
            return super().save(*args, **kwargs)
        previous = type(self).objects.get(pk=self.pk)
        if (previous.revoked_at is not None or self.revoked_at is None
                or previous.hearing_id != self.hearing_id
                or previous.token_digest != self.token_digest
                or previous.organization_id != self.organization_id
                or previous.created_by_id != self.created_by_id):
            raise ValidationError('Live display links may only be revoked once.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Live display links are retained as revocation history.')


class PanelAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Panel audit events are append-only.')

    def delete(self):
        raise ValidationError('Panel audit events are append-only.')


class PanelAuditManager(OwnedManager.from_queryset(PanelAuditQuerySet)):
    pass


class PanelAuditEvent(OwnedDomainRecord):
    class Action(models.TextChoices):
        HEARING_CREATED = 'hearing_created', 'Hearing created'
        HEARING_OPENED = 'hearing_opened', 'Hearing opened'
        CANDIDATE_ADDED = 'candidate_added', 'Candidate added'
        CANDIDATE_LINKED = 'candidate_linked', 'Candidate linked to Classroom group'
        CANDIDATE_ACTIVATED = 'candidate_activated', 'Candidate activated for scoring'
        CANDIDATE_DEACTIVATED = 'candidate_deactivated', 'Candidate scoring ended'
        CRITERION_ADDED = 'criterion_added', 'Criterion added'
        PANEL_ADDED = 'panel_added', 'Scoring panel added'
        PANEL_UPDATED = 'panel_updated', 'Scoring panel updated'
        RESULTS_COMPILED = 'results_compiled', 'Results compiled'
        RESULTS_EXPORTED = 'results_exported', 'Results exported'
        RESULT_ADJUSTED = 'result_adjusted', 'Result adjusted'
        INVITATION_ISSUED = 'invitation_issued', 'Invitation issued'
        INVITATION_RESENT = 'invitation_resent', 'Invitation resent'
        INVITATION_REVOKED = 'invitation_revoked', 'Invitation revoked'
        INVITATION_DECLINED = 'invitation_declined', 'Invitation declined'
        LIVE_DISPLAY_ISSUED = 'live_display_issued', 'Live display link issued'
        LIVE_DISPLAY_REVOKED = 'live_display_revoked', 'Live display link revoked'
        DEVICE_BOUND = 'device_bound', 'Examiner invitation bound to a browser'
        CONFLICT_DECLARED = 'conflict_declared', 'Conflict declared'
        SCORE_ABSTAINED = 'score_abstained', 'Criterion score abstained'
        SCORE_SUBMITTED = 'score_submitted', 'Score submitted'
        REVIEW_SUBMITTED = 'review_submitted', 'Written review submitted'
        VOTE_SUBMITTED = 'vote_submitted', 'Private vote submitted'
        DECISION_RECORDED = 'decision_recorded', 'Chair outcome recorded'
        COMMENT_RELEASED = 'comment_released', 'Examiner comment approved for entrant release'
        RESULTS_SIGNED_OFF = 'results_signed_off', 'Results signed off'
        RESULTS_RELEASED = 'results_released', 'Results released'
        RESULT_LINK_ISSUED = 'result_link_issued', 'Entrant result link issued'
        RESULT_LINK_REVOKED = 'result_link_revoked', 'Entrant result link revoked'
        COMMENT_REDACTED = 'comment_redacted', 'Examiner comment redacted'

    hearing = models.ForeignKey(PanelHearing, on_delete=models.PROTECT, related_name='audit_events')
    invitation = models.ForeignKey(PanelInvitation, null=True, blank=True,
        on_delete=models.PROTECT, related_name='audit_events')
    candidate = models.ForeignKey(PanelCandidate, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='audit_events')
    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='panel_audit_events')
    action = models.CharField(max_length=24, choices=Action.choices)
    reason = models.TextField(max_length=1000)

    objects = PanelAuditManager()

    class Meta:
        ordering = ('created_at', 'pk')

    def clean(self):
        super().clean()
        if self.invitation_id and self.invitation.hearing_id != self.hearing_id:
            raise ValidationError({'invitation': 'The invitation must belong to this hearing.'})
        if self.candidate_id and self.candidate.hearing_id != self.hearing_id:
            raise ValidationError({'candidate': 'The candidate must belong to this hearing.'})
        if not self.reason or not self.reason.strip():
            raise ValidationError({'reason': 'An audit reason is required.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Panel audit events are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Panel audit events are append-only.')


class ShowcaseEvent(OwnedDomainRecord):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        OPEN = 'open', 'Open for ratings'
        CLOSED = 'closed', 'Closed'
        PUBLISHED = 'published', 'Results published'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=160)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    includes_minors = models.BooleanField(default=False)
    consent_confirmed_at = models.DateTimeField(null=True, blank=True)
    minimum_judges_per_exhibit = models.PositiveSmallIntegerField(default=1,
        validators=[MinValueValidator(0), MaxValueValidator(50)],
        help_text='Set 0 for an audience-only event; otherwise require this many judge scorecards per exhibit.')
    public_results = models.BooleanField(default=False)
    workflow_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    branding = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return self.name

    @property
    def brand_accent_color(self):
        accent = self.branding.get('accent') if isinstance(self.branding, dict) else None
        return SHOWCASE_BRAND_ACCENTS.get(accent, SHOWCASE_BRAND_ACCENTS['blue'])

    @property
    def brand_tagline(self):
        tagline = self.branding.get('tagline', '') if isinstance(self.branding, dict) else ''
        return tagline if isinstance(tagline, str) else ''

    def clean(self):
        super().clean()
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({'ends_at': 'Event end must be after its start.'})
        if not isinstance(self.branding, dict):
            raise ValidationError({'branding': 'Event branding must be a structured object.'})
        if set(self.branding) - {'accent', 'tagline'}:
            raise ValidationError({'branding': 'Event branding contains unsupported settings.'})
        accent = self.branding.get('accent', 'blue')
        if not isinstance(accent, str) or accent not in SHOWCASE_BRAND_ACCENTS:
            raise ValidationError({'branding': 'Choose a supported event accent color.'})
        tagline = self.branding.get('tagline', '')
        if not isinstance(tagline, str) or len(tagline) > 160:
            raise ValidationError({'branding': 'Event tagline must be 160 characters or fewer.'})
        if self.status == self.Status.OPEN and self.includes_minors and not self.consent_confirmed_at:
            raise ValidationError({'consent_confirmed_at': 'Record required minor-participant consent before opening.'})


class ShowcaseEventAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Showcase lifecycle audit events are append-only.')

    def delete(self):
        raise ValidationError('Showcase lifecycle audit events are append-only.')


class ShowcaseEventAuditManager(OwnedManager.from_queryset(ShowcaseEventAuditQuerySet)):
    pass


class ShowcaseEventAudit(OwnedDomainRecord):
    event = models.ForeignKey(ShowcaseEvent, on_delete=models.PROTECT, related_name='audit_events')
    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='showcase_event_audit_events')
    from_status = models.CharField(max_length=12, choices=ShowcaseEvent.Status.choices)
    to_status = models.CharField(max_length=12, choices=ShowcaseEvent.Status.choices)
    reason = models.CharField(max_length=500)

    objects = ShowcaseEventAuditManager()

    class Meta:
        ordering = ('created_at', 'pk')

    def clean(self):
        super().clean()
        if not self.reason or not self.reason.strip():
            raise ValidationError({'reason': 'A lifecycle reason is required.'})
        if self.from_status == self.to_status:
            raise ValidationError({'to_status': 'A lifecycle action must change event status.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Showcase lifecycle audit events are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Showcase lifecycle audit events are append-only.')


class ShowcaseConsentAuditQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Showcase consent audit events are append-only.')

    def delete(self):
        raise ValidationError('Showcase consent audit events are append-only.')


class ShowcaseConsentAuditManager(OwnedManager.from_queryset(ShowcaseConsentAuditQuerySet)):
    pass


class ShowcaseConsentAudit(OwnedDomainRecord):
    """Owner attestation that required minor-participant consent was collected."""

    event = models.ForeignKey(ShowcaseEvent, on_delete=models.PROTECT,
        related_name='consent_audit_events')
    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='showcase_consent_audit_events')
    reason = models.CharField(max_length=500)
    confirmed_at = models.DateTimeField(auto_now_add=True)

    objects = ShowcaseConsentAuditManager()

    class Meta:
        ordering = ('confirmed_at', 'pk')

    def clean(self):
        super().clean()
        if not self.reason or not self.reason.strip():
            raise ValidationError({'reason': 'A consent confirmation statement is required.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Showcase consent audit events are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Showcase consent audit events are append-only.')


class ShowcaseCertificate(OwnedDomainRecord):
    """Verifiable, privacy-preserving record of a published exhibit award."""

    class AwardType(models.TextChoices):
        CATEGORY = 'category', 'Category award'
        AUDIENCE = 'audience', 'Audience favorite'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    event = models.ForeignKey(ShowcaseEvent, on_delete=models.PROTECT,
        related_name='certificates')
    exhibit = models.ForeignKey('ShowcaseExhibit', on_delete=models.PROTECT,
        related_name='certificates')
    category = models.ForeignKey('ShowcaseCategory', null=True, blank=True,
        on_delete=models.PROTECT, related_name='certificates')
    award_type = models.CharField(max_length=12, choices=AwardType.choices)
    award_title = models.CharField(max_length=160)
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('event_id', 'award_type', 'pk')
        constraints = [models.UniqueConstraint(
            fields=('event', 'exhibit', 'award_type'), name='showcase_award_certificate_once')]

    def clean(self):
        super().clean()
        if self.exhibit_id and self.event_id != self.exhibit.event_id:
            raise ValidationError({'exhibit': 'The awarded exhibit must belong to this event.'})
        if self.category_id:
            if self.category.event_id != self.event_id:
                raise ValidationError({'category': 'The award category must belong to this event.'})
            if self.exhibit.category_id != self.category_id:
                raise ValidationError({'category': 'The award category must match the exhibit category.'})
        if self.award_type == self.AwardType.CATEGORY and not self.category_id:
            raise ValidationError({'category': 'A category award must identify its category.'})
        if self.award_type == self.AwardType.AUDIENCE and self.category_id:
            raise ValidationError({'category': 'An audience award cannot be tied to one category.'})
        if not self.award_title or not self.award_title.strip():
            raise ValidationError({'award_title': 'A certificate needs an award title.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Issued certificates are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Issued certificates are immutable.')


class ShowcaseCategory(OwnedDomainRecord):
    event = models.ForeignKey(ShowcaseEvent, on_delete=models.CASCADE, related_name='categories')
    name = models.CharField(max_length=100)
    award_title = models.CharField(max_length=100, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('order', 'pk')
        constraints = [models.UniqueConstraint(fields=('event', 'name'), name='showcase_category_name_unique')]

    def __str__(self):
        return self.name


class ShowcaseExhibit(OwnedDomainRecord):
    event = models.ForeignKey(ShowcaseEvent, on_delete=models.CASCADE, related_name='exhibits')
    category = models.ForeignKey(ShowcaseCategory, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='exhibits')
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True, max_length=2000)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('order', 'pk')

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.category_id and self.category.event_id != self.event_id:
            raise ValidationError({'category': 'The category must belong to this showcase event.'})


class ShowcaseJudgeAssignment(OwnedDomainRecord):
    """Owner-managed judge-to-exhibit assignment; labels are private workspace data."""

    event = models.ForeignKey(ShowcaseEvent, on_delete=models.CASCADE, related_name='judge_assignments')
    exhibit = models.ForeignKey(ShowcaseExhibit, on_delete=models.CASCADE, related_name='judge_assignments')
    judge_label = models.CharField(max_length=160)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ('exhibit__order', 'judge_label', 'pk')
        constraints = [models.UniqueConstraint(
            models.F('event'), models.F('exhibit'), Lower('judge_label'),
            condition=Q(active=True), name='showcase_active_judge_assignment_unique')]

    def clean(self):
        super().clean()
        if self.event_id and self.exhibit_id and self.event_id != self.exhibit.event_id:
            raise ValidationError({'exhibit': 'The exhibit must belong to this showcase event.'})
        if not self.judge_label or not self.judge_label.strip():
            raise ValidationError({'judge_label': 'A judge name or assignment label is required.'})
        if self.event_id and self.event.status in (
                ShowcaseEvent.Status.CLOSED, ShowcaseEvent.Status.PUBLISHED):
            raise ValidationError({'event': 'Judge assignments are locked after the event closes.'})


class ShowcaseJudgeInvitation(OwnedDomainRecord):
    """Digest-only private link for an invited judge in one Showcase event."""

    event = models.ForeignKey(ShowcaseEvent, on_delete=models.CASCADE,
        related_name='judge_invitations')
    email = models.EmailField(max_length=254)
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('email', 'pk')

    @staticmethod
    def digest_token(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def clean(self):
        super().clean()
        if not isinstance(self.email, str) or not self.email.strip():
            raise ValidationError({'email': 'Enter a judge email address.'})
        if self.expires_at and self.expires_at <= timezone.now():
            raise ValidationError({'expires_at': 'Judge invitation expiry must be in the future.'})
        if self.token_digest and len(self.token_digest) != 64:
            raise ValidationError({'token_digest': 'The judge invitation token digest is invalid.'})

    def usable_at(self, at=None):
        at = at or timezone.now()
        return (self.revoked_at is None and self.expires_at > at
                and self.event.status in (ShowcaseEvent.Status.DRAFT,
                    ShowcaseEvent.Status.OPEN, ShowcaseEvent.Status.CLOSED))


class ShowcaseJudgeExhibitAssignment(OwnedDomainRecord):
    """An invited judge's assignment to one exhibit in the event."""

    event = models.ForeignKey(ShowcaseEvent, on_delete=models.CASCADE,
        related_name='invited_judge_assignments')
    invitation = models.ForeignKey(ShowcaseJudgeInvitation, on_delete=models.CASCADE,
        related_name='exhibit_assignments')
    exhibit = models.ForeignKey(ShowcaseExhibit, on_delete=models.CASCADE,
        related_name='invited_judge_assignments')
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ('exhibit__order', 'invitation__email', 'pk')
        constraints = [models.UniqueConstraint(fields=('invitation', 'exhibit'),
            name='showcase_invitation_exhibit_once')]

    def clean(self):
        super().clean()
        if self.invitation_id and self.invitation.event_id != self.event_id:
            raise ValidationError({'invitation': 'The judge invitation must belong to this event.'})
        if self.exhibit_id and self.exhibit.event_id != self.event_id:
            raise ValidationError({'exhibit': 'The assigned exhibit must belong to this event.'})
        if self.event_id and self.event.status == ShowcaseEvent.Status.PUBLISHED:
            raise ValidationError({'event': 'Judge assignments are locked after results are published.'})


class ShowcaseJudgeCriterion(OwnedDomainRecord):
    """Weighted criterion and scale used by invited Showcase judges."""

    class Scale(models.TextChoices):
        POINTS = 'points', 'Points'
        STARS = 'stars', 'Five stars'
        PASS_FAIL = 'pass_fail', 'Pass or fail'
        CHECKLIST = 'checklist', 'Checklist'

    event = models.ForeignKey(ShowcaseEvent, on_delete=models.CASCADE,
        related_name='judge_criteria')
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, max_length=1000)
    weight = models.DecimalField(max_digits=5, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    scale = models.CharField(max_length=12, choices=Scale.choices, default=Scale.POINTS)
    max_points = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('100.00'),
        validators=[MinValueValidator(Decimal('0.01')), MaxValueValidator(1000)])
    checklist_items = models.JSONField(default=list, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('order', 'pk')
        constraints = [models.UniqueConstraint(fields=('event', 'name'),
            name='showcase_judge_criterion_name_unique')]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.event_id and self.event.status != ShowcaseEvent.Status.DRAFT:
            raise ValidationError({'event': 'Judge criteria are locked after the event opens.'})
        if self.scale == self.Scale.CHECKLIST:
            if (not isinstance(self.checklist_items, list) or not self.checklist_items
                    or len(self.checklist_items) > 20
                    or any(not isinstance(item, str) or not item.strip() or len(item) > 240
                           for item in self.checklist_items)):
                raise ValidationError({'checklist_items': 'A checklist needs 1 to 20 non-empty items.'})
        elif self.checklist_items:
            raise ValidationError({'checklist_items': 'Checklist items are allowed only for checklist criteria.'})


class ShowcaseJudgeEvaluationQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Submitted Showcase judge evaluations are immutable.')

    def delete(self):
        raise ValidationError('Submitted Showcase judge evaluations are immutable.')


class ShowcaseJudgeEvaluationManager(OwnedManager.from_queryset(ShowcaseJudgeEvaluationQuerySet)):
    pass


class ShowcaseJudgeEvaluation(OwnedDomainRecord):
    """Immutable submitted result for one judge and exhibit."""

    assignment = models.OneToOneField(ShowcaseJudgeExhibitAssignment,
        on_delete=models.PROTECT, related_name='evaluation')
    raw_scores = models.JSONField()
    score = models.DecimalField(max_digits=6, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('submitted_at', 'pk')

    objects = ShowcaseJudgeEvaluationManager()

    def clean(self):
        super().clean()
        if not isinstance(self.raw_scores, dict):
            raise ValidationError({'raw_scores': 'Judge scores must be a criterion mapping.'})
        if self.assignment_id:
            expected = {str(pk) for pk in ShowcaseJudgeCriterion.objects.filter(
                event=self.assignment.event).values_list('pk', flat=True)}
            if not expected or set(self.raw_scores) != expected:
                raise ValidationError({'raw_scores': 'Score every configured criterion exactly once.'})
            from .rules import showcase_judge_score
            criteria = list(ShowcaseJudgeCriterion.objects.filter(
                event=self.assignment.event).order_by('order', 'pk'))
            if self.score != showcase_judge_score(criteria, self.raw_scores):
                raise ValidationError({'score': 'The normalized score must match the submitted criteria.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Submitted Showcase judge evaluations are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Submitted Showcase judge evaluations are immutable.')


class ShowcaseJudgeConflictQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Showcase judge conflict declarations are append-only.')

    def delete(self):
        raise ValidationError('Showcase judge conflict declarations are append-only.')


class ShowcaseJudgeConflictManager(OwnedManager.from_queryset(ShowcaseJudgeConflictQuerySet)):
    pass


class ShowcaseJudgeConflict(OwnedDomainRecord):
    """Immutable conflict declaration for one invited judge and exhibit."""

    assignment = models.OneToOneField(ShowcaseJudgeExhibitAssignment,
        on_delete=models.PROTECT, related_name='conflict_declaration')
    reason = models.CharField(max_length=500)
    declared_at = models.DateTimeField(auto_now_add=True)

    objects = ShowcaseJudgeConflictManager()

    class Meta:
        ordering = ('declared_at', 'pk')

    def clean(self):
        super().clean()
        if not self.reason or not self.reason.strip():
            raise ValidationError({'reason': 'A conflict declaration reason is required.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Showcase judge conflict declarations are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Showcase judge conflict declarations are append-only.')


class ShowcaseRating(OwnedDomainRecord):
    event = models.ForeignKey(ShowcaseEvent, on_delete=models.CASCADE, related_name='ratings')
    exhibit = models.ForeignKey(ShowcaseExhibit, on_delete=models.CASCADE, related_name='ratings')
    device_digest = models.CharField(max_length=64)
    stars = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.CharField(max_length=500, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('event', 'exhibit', 'device_digest'), name='showcase_device_rating_once'),
            models.CheckConstraint(condition=Q(stars__gte=1, stars__lte=5), name='showcase_rating_stars_range'),
        ]

    @staticmethod
    def digest_device_key(key, event):
        try:
            payload = signing.TimestampSigner(salt='livegrade.showcase.device.v1').unsign_object(
                key, max_age=SHOWCASE_DEVICE_TOKEN_MAX_AGE)
        except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError) as exc:
            raise ValidationError('Invalid or expired showcase device token.') from exc
        if (not isinstance(payload, dict)
                or payload.get('event') != str(event.public_id)
                or not re.fullmatch(r'[0-9a-f-]{36}', str(payload.get('event', '')))):
            raise ValidationError('Invalid showcase device token.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', str(payload.get('device', ''))):
            raise ValidationError('Invalid showcase device token.')
        return hashlib.sha256(key.encode('utf-8')).hexdigest()

    @staticmethod
    def issue_device_token(event):
        return signing.TimestampSigner(salt='livegrade.showcase.device.v1').sign_object({
            'event': str(event.public_id), 'device': secrets.token_urlsafe(32)})

    def clean(self):
        super().clean()
        if self.event_id and self.exhibit_id and self.event_id != self.exhibit.event_id:
            raise ValidationError({'exhibit': 'The exhibit must belong to this event.'})
        if self.event_id and self.device_digest and len(self.device_digest) != 64:
            raise ValidationError({'device_digest': 'The device token digest is invalid.'})
        if self.event_id and self.event.status != ShowcaseEvent.Status.OPEN:
            raise ValidationError({'event': 'Ratings are accepted only while the event is open.'})


class TrainingSeries(OwnedDomainRecord):
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True, max_length=2000)
    cadence_note = models.CharField(max_length=120, blank=True,
        help_text='Optional planning note, for example “weekly on Thursdays”.')
    active = models.BooleanField(default=True)
    feedback_minimum_responses = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(2), MaxValueValidator(50)],
        help_text='Feedback remains hidden until this many anonymous responses are received.')

    class Meta:
        ordering = ('name', 'pk')

    def __str__(self):
        return self.name


class TrainingCompetency(OwnedDomainRecord):
    series = models.ForeignKey(TrainingSeries, on_delete=models.CASCADE, related_name='competencies')
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, max_length=1000)
    passing_score = models.DecimalField(max_digits=5, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('order', 'pk')
        constraints = [models.UniqueConstraint(fields=('series', 'name'), name='training_competency_name_unique')]

    def __str__(self):
        return self.name


class TrainingParticipant(OwnedDomainRecord):
    """Minimal reusable series roster; no email or learner account is stored."""

    series = models.ForeignKey(TrainingSeries, on_delete=models.CASCADE, related_name='participants')
    display_name = models.CharField(max_length=150)
    reference = models.CharField(max_length=40, blank=True)
    group_name = models.CharField(max_length=100)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('order', 'pk')
        constraints = [models.UniqueConstraint(fields=('series', 'reference'), condition=~Q(reference=''),
            name='training_participant_reference_unique')]

    def __str__(self):
        return self.display_name


class TrainingSeriesSession(OwnedDomainRecord):
    series = models.ForeignKey(TrainingSeries, on_delete=models.CASCADE, related_name='series_sessions')
    assessment_session = models.OneToOneField('assessments.AssessmentSession', on_delete=models.PROTECT,
        related_name='training_series_link')
    feedback_public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    sequence = models.PositiveIntegerField()
    scheduled_for = models.DateTimeField(null=True, blank=True)
    workflow_snapshot = models.JSONField(default=dict, blank=True, editable=False)

    class Meta:
        ordering = ('sequence',)
        constraints = [models.UniqueConstraint(fields=('series', 'sequence'), name='training_series_sequence_unique')]

    def __str__(self):
        return str(self.assessment_session)

    def clean(self):
        super().clean()
        if self.assessment_session_id:
            if self.assessment_session.service != 'team':
                raise ValidationError({'assessment_session': 'Training series can link only Team and Training sessions.'})
            if self.assessment_session.organization_id != self.organization_id:
                raise ValidationError({'assessment_session': 'The session and series must belong to the same organization.'})
            if self.assessment_session.created_by_id != self.created_by_id:
                raise ValidationError({'assessment_session': 'The session and series must have the same owner.'})


class TrainingLearnerReportLink(OwnedDomainRecord):
    """Owner-released, revocable snapshot shared privately with one learner."""
    series_session = models.ForeignKey(TrainingSeriesSession, on_delete=models.PROTECT,
        related_name='learner_report_links')
    student = models.ForeignKey('assessments.Student', on_delete=models.PROTECT,
        related_name='training_report_links')
    token_digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    snapshot = models.JSONField()


class TrainingFeedbackQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Anonymous training feedback is immutable.')

    def delete(self):
        raise ValidationError('Anonymous training feedback is immutable.')

    def delete_for_series_removal(self):
        return super().delete()


class TrainingFeedbackManager(OwnedManager.from_queryset(TrainingFeedbackQuerySet)):
    pass


class TrainingFeedback(OwnedDomainRecord):
    """One anonymous numeric response per signed browser token and demo."""

    FEEDBACK_TOKEN_MAX_AGE = 7 * 24 * 60 * 60
    series_session = models.ForeignKey(TrainingSeriesSession, on_delete=models.PROTECT,
        related_name='anonymous_feedback')
    device_digest = models.CharField(max_length=64)
    score = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    submitted_at = models.DateTimeField(auto_now_add=True)

    objects = TrainingFeedbackManager()

    class Meta:
        ordering = ('submitted_at', 'pk')
        constraints = [
            models.UniqueConstraint(fields=('series_session', 'device_digest'),
                name='training_feedback_once_per_device'),
            models.CheckConstraint(condition=Q(score__gte=1, score__lte=5),
                name='training_feedback_score_range'),
        ]

    @staticmethod
    def issue_device_token(series_session):
        return signing.TimestampSigner(salt='livegrade.team.feedback.device.v1').sign_object({
            'series_session': str(series_session.feedback_public_id),
            'device': secrets.token_urlsafe(32),
        })

    @classmethod
    def digest_device_token(cls, token, series_session):
        try:
            payload = signing.TimestampSigner(salt='livegrade.team.feedback.device.v1').unsign_object(
                token, max_age=cls.FEEDBACK_TOKEN_MAX_AGE)
        except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError) as exc:
            raise ValidationError('The anonymous feedback link is invalid or expired.') from exc
        if (not isinstance(payload, dict)
                or payload.get('series_session') != str(series_session.feedback_public_id)
                or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', str(payload.get('device', '')))):
            raise ValidationError('The anonymous feedback link is invalid or expired.')
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def clean(self):
        models.Model.clean(self)
        if self.created_by_id is not None:
            raise ValidationError({'created_by': 'Anonymous feedback cannot be linked to an account.'})
        if self.series_session_id:
            if self.organization_id != self.series_session.organization_id:
                raise ValidationError({'series_session': 'The feedback and demo must belong to the same organization.'})
            if self.series_session.series.feedback_minimum_responses is None:
                raise ValidationError({'series_session': 'The owner must set a feedback privacy threshold first.'})
            if self.series_session.assessment_session.status != 'closed':
                raise ValidationError({'series_session': 'Feedback is accepted after the demo session closes.'})
        if len(self.device_digest) != 64:
            raise ValidationError({'device_digest': 'The anonymous device token digest is invalid.'})


    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Anonymous training feedback is immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Anonymous training feedback is immutable.')


class TrainingSessionCompetency(OwnedDomainRecord):
    """Immutable copy of a series criterion for one demo session."""

    series_session = models.ForeignKey(TrainingSeriesSession, on_delete=models.CASCADE,
        related_name='competency_criteria')
    source_competency = models.ForeignKey(TrainingCompetency, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='session_copies')
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, max_length=1000)
    passing_score = models.DecimalField(max_digits=5, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('order', 'pk')
        constraints = [models.UniqueConstraint(fields=('series_session', 'name'),
            name='training_session_competency_name_unique')]

    def clean(self):
        super().clean()
        if (self.source_competency_id and self.series_session_id
                and self.source_competency.series_id != self.series_session.series_id):
            raise ValidationError({'source_competency': 'The source criterion must belong to this series.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Session competency criteria are immutable snapshots.')
        return super().save(*args, **kwargs)


class TrainingCompetencyResultQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Competency decisions are append-only; record a correction revision.')

    def delete(self):
        raise ValidationError('Competency decisions are append-only.')

    def delete_for_series_removal(self):
        return super().delete()


class TrainingCompetencyResultManager(OwnedManager.from_queryset(TrainingCompetencyResultQuerySet)):
    pass


class TrainingCompetencyResult(OwnedDomainRecord):
    """Traceable, append-only competency outcome for a learner in a demo."""

    criterion = models.ForeignKey(TrainingSessionCompetency, on_delete=models.PROTECT,
        related_name='results')
    student = models.ForeignKey('assessments.Student', on_delete=models.PROTECT,
        related_name='training_competency_results')
    score = models.DecimalField(max_digits=5, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    evidence = models.TextField(max_length=1000)
    correction_reason = models.CharField(max_length=500, blank=True)
    revision = models.PositiveIntegerField(default=1)
    supersedes = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT,
        related_name='corrections')
    assessed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='training_competency_decisions')
    assessed_at = models.DateTimeField(auto_now_add=True)

    objects = TrainingCompetencyResultManager()

    class Meta:
        ordering = ('criterion_id', 'student_id', 'revision')
        constraints = [models.UniqueConstraint(fields=('criterion', 'student', 'revision'),
            name='training_competency_result_revision_unique')]

    @property
    def passed(self):
        return self.score >= self.criterion.passing_score

    def clean(self):
        super().clean()
        session_id = self.criterion.series_session.assessment_session_id if self.criterion_id else None
        if self.student_id and session_id != self.student.assessment_session_id:
            raise ValidationError({'student': 'The learner must belong to this Team session.'})
        if not self.evidence or not self.evidence.strip():
            raise ValidationError({'evidence': 'Record the evidence for this competency decision.'})
        if self.supersedes_id:
            previous = self.supersedes
            if (previous.criterion_id != self.criterion_id or previous.student_id != self.student_id
                    or previous.revision + 1 != self.revision):
                raise ValidationError({'supersedes': 'A correction must supersede the latest same-learner decision.'})
            if not self.correction_reason or not self.correction_reason.strip():
                raise ValidationError({'correction_reason': 'A reason is required to correct a competency decision.'})
        elif self.revision != 1:
            raise ValidationError({'revision': 'The first competency decision must be revision one.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Competency decisions are append-only; record a correction revision.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Competency decisions are append-only.')


class TrainingCertificate(OwnedDomainRecord):
    """Immutable public verification record for one passing Team outcome revision."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    result = models.OneToOneField(TrainingCompetencyResult, on_delete=models.PROTECT,
        related_name='certificate')
    learner_name = models.CharField(max_length=150)
    learner_reference = models.CharField(max_length=40, blank=True)
    series_name = models.CharField(max_length=160)
    session_name = models.CharField(max_length=160)
    competency_name = models.CharField(max_length=120)
    score = models.DecimalField(max_digits=5, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    passing_score = models.DecimalField(max_digits=5, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    issued_at = models.DateTimeField(auto_now_add=True)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='team_training_certificates_issued')

    class Meta:
        ordering = ('-issued_at', 'pk')

    def clean(self):
        super().clean()
        if self.result_id:
            result = self.result
            session_link = result.criterion.series_session
            if result.score < result.criterion.passing_score:
                raise ValidationError({'result': 'A certificate requires a passing competency result.'})
            session_link.assessment_session.refresh_from_db(fields=('status',))
            if session_link.assessment_session.status != 'closed':
                raise ValidationError({'result': 'Certificates can be issued after the Team demo closes.'})
            expected = (result.student.full_name, result.student.student_id,
                        session_link.series.name, session_link.assessment_session.name,
                        result.criterion.name, result.score, result.criterion.passing_score)
            actual = (self.learner_name, self.learner_reference, self.series_name,
                      self.session_name, self.competency_name, self.score, self.passing_score)
            if actual != expected:
                raise ValidationError('Certificate details must snapshot the linked passing result.')

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Issued Team certificates are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Issued Team certificates are immutable.')


class Competition(OwnedDomainRecord):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        ACTIVE = 'active', 'In progress'
        COMPLETE = 'complete', 'Complete'
        REVEALED = 'revealed', 'Leaderboard revealed'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=160)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    audience_weight = models.DecimalField(max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    minimum_judges_per_entry = models.PositiveSmallIntegerField(default=1,
        validators=[MinValueValidator(1), MaxValueValidator(50)])
    minimum_audience_ballots_per_track = models.PositiveIntegerField(default=1,
        validators=[MinValueValidator(1), MaxValueValidator(10000)])
    workflow_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    revealed_at = models.DateTimeField(null=True, blank=True)
    public_leaderboard_enabled = models.BooleanField(default=False,
        help_text='Public results remain available only while this owner-controlled display is enabled.')

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return self.name

    @property
    def effective_audience_weight(self):
        """Return the published weight frozen at event start, or the draft value."""
        snapshot = self.workflow_snapshot if isinstance(self.workflow_snapshot, dict) else {}
        try:
            return Decimal(str(snapshot.get('audience_weight', self.audience_weight)))
        except (ArithmeticError, TypeError, ValueError):
            return self.audience_weight

    @property
    def judge_weight(self):
        return Decimal('100') - self.effective_audience_weight

    @property
    def effective_minimum_judges(self):
        snapshot = self.workflow_snapshot if isinstance(self.workflow_snapshot, dict) else {}
        return int(snapshot.get('minimum_judges_per_entry', self.minimum_judges_per_entry))

    @property
    def effective_minimum_audience_ballots(self):
        snapshot = self.workflow_snapshot if isinstance(self.workflow_snapshot, dict) else {}
        return int(snapshot.get('minimum_audience_ballots_per_track', self.minimum_audience_ballots_per_track))

    def clean(self):
        super().clean()
        if self.audience_weight > 100:
            raise ValidationError({'audience_weight': 'Audience weighting cannot exceed 100%.'})
        if self.status == self.Status.REVEALED and not self.revealed_at:
            raise ValidationError({'revealed_at': 'A revealed leaderboard needs an authorized reveal timestamp.'})

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values_list(
                'workflow_snapshot', flat=True).first()
            update_fields = kwargs.get('update_fields')
            snapshot_requested = update_fields is None or 'workflow_snapshot' in update_fields
            if previous and previous != self.workflow_snapshot and snapshot_requested:
                raise ValidationError('Competition scoring rules are frozen when the event starts.')
            if (not previous and self.workflow_snapshot and self.status != self.Status.ACTIVE
                    and snapshot_requested):
                raise ValidationError('Competition scoring rules can be frozen only when the event starts.')
        return super().save(*args, **kwargs)


class CompetitionTransitionAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Competition lifecycle audit events are append-only.')

    def delete(self):
        raise ValidationError('Competition lifecycle audit events are append-only.')


class CompetitionTransitionAuditManager(OwnedManager.from_queryset(
        CompetitionTransitionAuditQuerySet)):
    pass


class CompetitionTransitionAudit(OwnedDomainRecord):
    """Append-only organizer record of Competition lifecycle transitions."""

    competition = models.ForeignKey(Competition, on_delete=models.PROTECT,
        related_name='transition_audit')
    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='competition_transition_audit')
    from_status = models.CharField(max_length=12, choices=Competition.Status.choices)
    to_status = models.CharField(max_length=12, choices=Competition.Status.choices)
    reason = models.CharField(max_length=300)
    objects = CompetitionTransitionAuditManager()

    class Meta:
        ordering = ('created_at', 'pk')

    def clean(self):
        super().clean()
        if self.from_status == self.to_status:
            raise ValidationError({'to_status': 'A lifecycle action must change Competition status.'})
        if not self.reason or not self.reason.strip():
            raise ValidationError({'reason': 'A lifecycle reason is required.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Competition lifecycle audit events are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Competition lifecycle audit events are append-only.')


class CompetitionLeaderboardAudit(OwnedDomainRecord):
    """Append-only record of enabling or hiding a revealed public leaderboard."""

    competition = models.ForeignKey(Competition, on_delete=models.PROTECT,
        related_name='leaderboard_audit')
    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='competition_leaderboard_audit')
    enabled = models.BooleanField()
    reason = models.CharField(max_length=200)

    class Meta:
        ordering = ('created_at', 'pk')

    def clean(self):
        super().clean()
        if self.competition_id and self.competition.status != Competition.Status.REVEALED:
            raise ValidationError({'competition': 'Public display controls apply only after results are revealed.'})
        if not self.reason or not self.reason.strip():
            raise ValidationError({'reason': 'A display-control reason is required.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Competition leaderboard audit events are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Competition leaderboard audit events are append-only.')


class CompetitionWeightedResultAudit(OwnedDomainRecord):
    """Immutable record of the chair's weighted-score close and its coverage."""

    round = models.OneToOneField('CompetitionRound', on_delete=models.PROTECT,
        related_name='weighted_result_audit')
    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='competition_weighted_result_audits')
    judge_weight = models.DecimalField(max_digits=5, decimal_places=2)
    audience_weight = models.DecimalField(max_digits=5, decimal_places=2)
    audience_ballot_count = models.PositiveIntegerField()
    method = models.CharField(max_length=64, default='complete_ranking_borda_0_100_v1')

    class Meta:
        ordering = ('created_at', 'pk')

    def clean(self):
        super().clean()
        if not self.round_id:
            return
        if self.round.audience_locked_at is None:
            raise ValidationError({'round': 'Weighted-result audits require closed audience voting.'})
        competition = self.round.competition
        if (self.audience_weight != competition.effective_audience_weight
                or self.judge_weight != competition.judge_weight):
            raise ValidationError({'audience_weight': 'Audit weights must match the frozen event policy.'})
        if self.audience_ballot_count < competition.effective_minimum_audience_ballots:
            raise ValidationError({'audience_ballot_count': 'The recorded audience count is below the configured floor.'})
        if self.method != 'complete_ranking_borda_0_100_v1':
            raise ValidationError({'method': 'Unknown weighted audience scoring method.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Weighted-result audit events are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Weighted-result audit events are append-only.')


class CompetitionTrack(OwnedDomainRecord):
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name='tracks')
    name = models.CharField(max_length=100)
    advancement_rule = models.CharField(max_length=12, choices=(('top_n', 'Top N'), ('threshold', 'Threshold')), default='top_n')
    advancement_value = models.DecimalField(max_digits=7, decimal_places=2, default=1,
        validators=[MinValueValidator(0)])

    class Meta:
        constraints = [models.UniqueConstraint(fields=('competition', 'name'), name='competition_track_name_unique')]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.advancement_rule == 'top_n' and (
                self.advancement_value < 1 or self.advancement_value != self.advancement_value.to_integral_value()):
            raise ValidationError({'advancement_value': 'Top-N advancement must be a positive whole number.'})
        if self.advancement_rule == 'threshold' and self.advancement_value > 100:
            raise ValidationError({'advancement_value': 'A score threshold cannot exceed 100.'})


class CompetitionCriterion(OwnedDomainRecord):
    """One organizer-defined judging dimension in a competition track."""

    track = models.ForeignKey(CompetitionTrack, on_delete=models.CASCADE,
        related_name='criteria')
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, max_length=1000)
    weight = models.DecimalField(max_digits=5, decimal_places=2, default=100,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('order', 'pk')
        constraints = [models.UniqueConstraint(fields=('track', 'name'),
            name='competition_criterion_name_unique')]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if not self.name or not self.name.strip():
            raise ValidationError({'name': 'Enter a criterion name.'})
        if self.weight <= 0:
            raise ValidationError({'weight': 'A judging criterion must have a positive weight.'})


class CompetitionEntry(OwnedDomainRecord):
    track = models.ForeignKey(CompetitionTrack, on_delete=models.CASCADE, related_name='entries')
    name = models.CharField(max_length=160)
    reference = models.CharField(max_length=80, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('order', 'pk')
        constraints = [models.UniqueConstraint(fields=('track', 'reference'), condition=~Q(reference=''),
            name='competition_entry_reference_unique')]

    def __str__(self):
        return self.name


class CompetitionRound(OwnedDomainRecord):
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name='rounds')
    name = models.CharField(max_length=100)
    order = models.PositiveIntegerField()
    advancement_locked_at = models.DateTimeField(null=True, blank=True)
    audience_locked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('order', 'pk')
        constraints = [models.UniqueConstraint(fields=('competition', 'order'), name='competition_round_order_unique')]

    def __str__(self):
        return self.name


class CompetitionAudienceJoinCode(models.Model):
    """Indexed public routing code; lifecycle checks remain on the target event."""

    code = models.CharField(max_length=8, unique=True)
    round = models.ForeignKey(CompetitionRound, on_delete=models.CASCADE, related_name='join_codes')
    track = models.ForeignKey(CompetitionTrack, on_delete=models.CASCADE, related_name='join_codes')

    class Meta:
        constraints = [models.UniqueConstraint(fields=('round', 'track'),
                       name='competition_join_round_track_unique')]


class CompetitionRoundEntry(OwnedDomainRecord):
    """An entry in a specific competition round, with its finalized score."""

    round = models.ForeignKey(CompetitionRound, on_delete=models.CASCADE, related_name='round_entries')
    entry = models.ForeignKey(CompetitionEntry, on_delete=models.PROTECT, related_name='round_entries')
    track = models.ForeignKey(CompetitionTrack, on_delete=models.PROTECT, related_name='round_entries')
    score = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    judge_score = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    audience_score = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    score_breakdown = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ('entry__track_id', 'entry__order', 'entry_id')
        constraints = [models.UniqueConstraint(fields=('round', 'entry'),
            name='competition_round_entry_unique')]

    def clean(self):
        super().clean()
        if self.entry_id and self.track_id and self.entry.track_id != self.track_id:
            raise ValidationError({'track': 'The round row must use the entry track.'})
        if (self.round_id and self.entry_id
                and self.round.competition_id != self.entry.track.competition_id):
            raise ValidationError({'entry': 'The entry must belong to this competition.'})
        if self.audience_score is not None and self.score is None:
            raise ValidationError({'audience_score': 'An audience score requires a finalized result.'})
        if self.judge_score is not None and self.score is None and self.round_id:
            competition = self.round.competition
            if competition.effective_audience_weight == 0:
                raise ValidationError({'score': 'Judge-only results must be finalized in the score field.'})


class CompetitionAudienceBallotQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Competition audience ballots are immutable.')

    def delete(self):
        raise ValidationError('Competition audience ballots are immutable.')


class CompetitionAudienceBallotManager(OwnedManager.from_queryset(CompetitionAudienceBallotQuerySet)):
    pass


class CompetitionAudienceBallot(OwnedDomainRecord):
    """Anonymous ranked audience ballot for one track in one active round."""

    DEVICE_TOKEN_MAX_AGE = 7 * 24 * 60 * 60
    competition = models.ForeignKey(Competition, on_delete=models.PROTECT,
        related_name='audience_ballots')
    round = models.ForeignKey(CompetitionRound, on_delete=models.PROTECT,
        related_name='audience_ballots')
    track = models.ForeignKey(CompetitionTrack, on_delete=models.PROTECT,
        related_name='audience_ballots')
    device_digest = models.CharField(max_length=64)
    ranking = models.JSONField()
    submitted_at = models.DateTimeField(auto_now_add=True)

    objects = CompetitionAudienceBallotManager()

    class Meta:
        ordering = ('submitted_at', 'pk')
        constraints = [
            models.UniqueConstraint(fields=('round', 'track', 'device_digest'),
                name='competition_audience_ballot_once'),
        ]

    @staticmethod
    def issue_device_token(competition, round_obj, track):
        return signing.TimestampSigner(salt='livegrade.competition.audience.device.v1').sign_object({
            'competition': str(competition.public_id),
            'round': round_obj.pk,
            'track': track.pk,
            'device': secrets.token_urlsafe(32),
        })

    @classmethod
    def digest_device_token(cls, token, competition, round_obj, track):
        try:
            payload = signing.TimestampSigner(salt='livegrade.competition.audience.device.v1').unsign_object(
                token, max_age=cls.DEVICE_TOKEN_MAX_AGE)
        except (signing.BadSignature, signing.SignatureExpired, TypeError, ValueError) as exc:
            raise ValidationError('The audience ballot token is invalid or expired.') from exc
        if (not isinstance(payload, dict)
                or payload.get('competition') != str(competition.public_id)
                or payload.get('round') != round_obj.pk
                or payload.get('track') != track.pk
                or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', str(payload.get('device', '')))):
            raise ValidationError('The audience ballot token is invalid or expired.')
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def clean(self):
        models.Model.clean(self)
        if self.created_by_id is not None:
            raise ValidationError({'created_by': 'Audience ballots cannot be linked to an account.'})
        if self.competition_id:
            if self.organization_id != self.competition.organization_id:
                raise ValidationError({'competition': 'The ballot must belong to this organization.'})
            if self.competition.status != Competition.Status.ACTIVE:
                raise ValidationError({'competition': 'Audience ballots are accepted only during an active competition.'})
        if self.round_id and self.round.competition_id != self.competition_id:
            raise ValidationError({'round': 'The ballot round must belong to this competition.'})
        if self.track_id and self.track.competition_id != self.competition_id:
            raise ValidationError({'track': 'The ballot track must belong to this competition.'})
        if self.round_id and self.track_id:
            latest_round_id = (CompetitionRound.objects.filter(competition_id=self.competition_id)
                .order_by('-order').values_list('pk', flat=True).first())
            if latest_round_id != self.round_id:
                raise ValidationError({'round': 'Audience ballots are accepted only for the latest round.'})
        if not isinstance(self.ranking, list) or not self.ranking:
            raise ValidationError({'ranking': 'Rank at least one entry.'})
        if (any(type(entry_id) is not int for entry_id in self.ranking)
                or len(self.ranking) != len(set(self.ranking))):
            raise ValidationError({'ranking': 'Ranked entries must be unique integer identifiers.'})
        allowed_ids = set(CompetitionRoundEntry.objects.filter(
            round_id=self.round_id, track_id=self.track_id).values_list('entry_id', flat=True))
        if not set(self.ranking) <= allowed_ids:
            raise ValidationError({'ranking': 'Every ranked entry must be entered in this round and track.'})
        if len(self.device_digest) != 64:
            raise ValidationError({'device_digest': 'The audience device token digest is invalid.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Competition audience ballots are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Competition audience ballots are immutable.')


class CompetitionJudgeInvitation(OwnedDomainRecord):
    """Bearer invitation authorizing one judge to score one current track."""

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE,
        related_name='judge_invitations')
    round = models.ForeignKey(CompetitionRound, on_delete=models.CASCADE,
        related_name='judge_invitations')
    track = models.ForeignKey(CompetitionTrack, on_delete=models.PROTECT,
        related_name='judge_invitations')
    email = models.EmailField(max_length=254)
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    @staticmethod
    def digest_token(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def clean(self):
        super().clean()
        if self.competition_id and self.round_id and self.round.competition_id != self.competition_id:
            raise ValidationError({'round': 'The judge round must belong to this competition.'})
        if self.competition_id and self.track_id and self.track.competition_id != self.competition_id:
            raise ValidationError({'track': 'The judge track must belong to this competition.'})
        if self.round_id and self.track_id and not CompetitionRoundEntry.objects.filter(
                round_id=self.round_id, track_id=self.track_id).exists():
            raise ValidationError({'track': 'The judge track needs entries in this round.'})
        if self.expires_at and self.expires_at <= timezone.now():
            raise ValidationError({'expires_at': 'Judge invitation expiry must be in the future.'})

    def usable_at(self, at=None):
        at = at or timezone.now()
        return (self.revoked_at is None and self.submitted_at is None
                and self.expires_at > at)


class CompetitionJudgeAbstention(OwnedDomainRecord):
    """A reasoned decision to exclude one judge from one entry's numeric result."""
    invitation = models.ForeignKey(CompetitionJudgeInvitation, on_delete=models.PROTECT,
        related_name='abstentions')
    round_entry = models.ForeignKey(CompetitionRoundEntry, on_delete=models.PROTECT,
        related_name='judge_abstentions')
    kind = models.CharField(max_length=10, choices=(('conflict', 'Conflict of interest'),
                                                 ('abstain', 'Unable to assess')))
    reason = models.CharField(max_length=1000)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('invitation', 'round_entry'),
                        name='competition_judge_entry_abstain_once')]

    def clean(self):
        super().clean()
        if self.invitation_id and self.round_entry_id:
            if (self.invitation.round_id != self.round_entry.round_id
                    or self.invitation.track_id != self.round_entry.track_id):
                raise ValidationError('The entry must belong to this judge’s assigned round and track.')
        if not self.reason.strip():
            raise ValidationError({'reason': 'Give a reason for not scoring this entry.'})


class CompetitionJudgeScoreQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Competition judge scores are immutable.')

    def delete(self):
        raise ValidationError('Competition judge scores are immutable.')


class CompetitionJudgeScoreManager(OwnedManager.from_queryset(CompetitionJudgeScoreQuerySet)):
    pass


class CompetitionJudgeScore(OwnedDomainRecord):
    """One private judge score for an entry in the invitation's track/round."""

    invitation = models.ForeignKey(CompetitionJudgeInvitation, on_delete=models.PROTECT,
        related_name='scores')
    round_entry = models.ForeignKey(CompetitionRoundEntry, on_delete=models.PROTECT,
        related_name='judge_scores')
    criterion = models.ForeignKey(CompetitionCriterion, null=True, blank=True,
        on_delete=models.PROTECT, related_name='judge_scores')
    score = models.DecimalField(max_digits=5, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    submitted_at = models.DateTimeField(auto_now_add=True)

    objects = CompetitionJudgeScoreManager()

    class Meta:
        ordering = ('round_entry_id', 'submitted_at', 'pk')
        constraints = [models.UniqueConstraint(fields=('invitation', 'round_entry', 'criterion'),
            name='competition_judge_score_once')]

    def clean(self):
        super().clean()
        if self.invitation_id and self.round_entry_id and (
                self.invitation.round_id != self.round_entry.round_id
                or self.invitation.track_id != self.round_entry.track_id):
            raise ValidationError({'round_entry': 'The score entry must match the invited track and round.'})
        if self.criterion_id and self.round_entry_id and self.criterion.track_id != self.round_entry.track_id:
            raise ValidationError({'criterion': 'The criterion must belong to the invited competition track.'})
        if self.invitation_id and self.organization_id != self.invitation.organization_id:
            raise ValidationError({'invitation': 'Judge score and invitation must share an organization.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Competition judge scores are immutable.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Competition judge scores are immutable.')


class CompetitionJudgeAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Competition judge audit records are append-only.')

    def delete(self):
        raise ValidationError('Competition judge audit records are append-only.')


class CompetitionJudgeAuditManager(OwnedManager.from_queryset(CompetitionJudgeAuditQuerySet)):
    pass


class CompetitionJudgeAudit(OwnedDomainRecord):
    class Action(models.TextChoices):
        INVITED = 'invited', 'Judge invited'
        SUBMITTED = 'submitted', 'Scorecard submitted'
        REVOKED = 'revoked', 'Invitation revoked'

    invitation = models.ForeignKey(CompetitionJudgeInvitation, on_delete=models.PROTECT,
        related_name='audit_events')
    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='competition_judge_audit')
    action = models.CharField(max_length=12, choices=Action.choices)
    objects = CompetitionJudgeAuditManager()

    class Meta:
        ordering = ('created_at', 'pk')

    def clean(self):
        super().clean()
        if self.invitation_id and self.organization_id != self.invitation.organization_id:
            raise ValidationError({'invitation': 'Audit and invitation must share an organization.'})
        if self.invitation_id and self.created_by_id != self.invitation.created_by_id:
            raise ValidationError({'invitation': 'Audit and invitation must share an owner.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Competition judge audit records are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Competition judge audit records are append-only.')


class CompetitionAdvancementDecisionQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Competition advancement decisions are append-only.')

    def delete(self):
        raise ValidationError('Competition advancement decisions are append-only.')


class CompetitionAdvancementDecisionManager(OwnedManager.from_queryset(
        CompetitionAdvancementDecisionQuerySet)):
    pass


class CompetitionAdvancementDecision(OwnedDomainRecord):
    """Immutable per-track record of the rule and entrants selected."""

    source_round = models.ForeignKey(CompetitionRound, on_delete=models.PROTECT,
        related_name='advancement_decisions')
    next_round = models.ForeignKey(CompetitionRound, on_delete=models.PROTECT,
        related_name='incoming_advancement_decisions')
    track = models.ForeignKey(CompetitionTrack, on_delete=models.PROTECT,
        related_name='advancement_decisions')
    rule = models.CharField(max_length=12, choices=(('top_n', 'Top N'), ('threshold', 'Threshold')))
    rule_value = models.DecimalField(max_digits=7, decimal_places=2)
    tie_break_order = models.JSONField(default=list, blank=True)
    head_judge_decision = models.JSONField(default=list, blank=True)
    head_judge_reason = models.CharField(max_length=500, blank=True)
    selected_entry_ids = models.JSONField(default=list, blank=True)

    objects = CompetitionAdvancementDecisionManager()

    class Meta:
        ordering = ('track_id', 'pk')
        constraints = [models.UniqueConstraint(fields=('source_round', 'track'),
            name='competition_advancement_once_per_track')]

    def clean(self):
        super().clean()
        if self.source_round_id and self.next_round_id:
            if self.source_round.competition_id != self.next_round.competition_id:
                raise ValidationError({'next_round': 'Both rounds must belong to the same competition.'})
        if self.source_round_id and self.track_id:
            if self.source_round.competition_id != self.track.competition_id:
                raise ValidationError({'track': 'The track must belong to this competition.'})
        if not isinstance(self.tie_break_order, list) or not isinstance(self.head_judge_decision, list):
            raise ValidationError('Advancement tie decisions must be lists of entry identifiers.')
        if not isinstance(self.selected_entry_ids, list):
            raise ValidationError({'selected_entry_ids': 'Selected entries must be a list.'})
        if self.head_judge_decision and not self.head_judge_reason.strip():
            raise ValidationError({'head_judge_reason': 'Record why the head judge made this advancement decision.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Competition advancement decisions are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Competition advancement decisions are append-only.')


class CompetitionLeaderboardTieDecisionQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Competition leaderboard tie decisions are append-only.')

    def delete(self):
        raise ValidationError('Competition leaderboard tie decisions are append-only.')


class CompetitionLeaderboardTieDecisionManager(OwnedManager.from_queryset(
        CompetitionLeaderboardTieDecisionQuerySet)):
    pass


class CompetitionLeaderboardTieDecision(OwnedDomainRecord):
    """Immutable head-judge ordering for one tied score on the final round."""

    round = models.ForeignKey(CompetitionRound, on_delete=models.PROTECT,
        related_name='leaderboard_tie_decisions')
    track = models.ForeignKey(CompetitionTrack, on_delete=models.PROTECT,
        related_name='leaderboard_tie_decisions')
    tied_score = models.DecimalField(max_digits=7, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    ordered_entry_ids = models.JSONField()
    reason = models.CharField(max_length=500)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='competition_final_tie_decisions')

    objects = CompetitionLeaderboardTieDecisionManager()

    class Meta:
        ordering = ('track_id', '-tied_score', 'pk')
        constraints = [models.UniqueConstraint(fields=('round', 'track', 'tied_score'),
            name='competition_final_tie_once')]

    def clean(self):
        super().clean()
        if self.round_id and self.track_id and self.round.competition_id != self.track.competition_id:
            raise ValidationError({'track': 'The tie track must belong to this competition.'})
        if not isinstance(self.ordered_entry_ids, list) or any(
                type(entry_id) is not int for entry_id in self.ordered_entry_ids):
            raise ValidationError({'ordered_entry_ids': 'Head-judge order must be a list of entry identifiers.'})
        if len(self.ordered_entry_ids) < 2 or len(self.ordered_entry_ids) != len(set(self.ordered_entry_ids)):
            raise ValidationError({'ordered_entry_ids': 'A tie decision must order at least two unique entries.'})
        tied_entry_ids = set(CompetitionRoundEntry.objects.filter(
            round_id=self.round_id, track_id=self.track_id, score=self.tied_score)
            .values_list('entry_id', flat=True))
        if len(tied_entry_ids) < 2 or set(self.ordered_entry_ids) != tied_entry_ids:
            raise ValidationError({'ordered_entry_ids': 'The decision must order every entry in this exact final-score tie.'})
        if (self.round_id and Competition.objects.filter(
                pk=self.round.competition_id).exclude(status=Competition.Status.COMPLETE).exists()):
            raise ValidationError({'round': 'Final tie decisions are recorded after scoring is completed.'})
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValidationError({'reason': 'A reason is required for every final tie decision.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Competition leaderboard tie decisions are append-only.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Competition leaderboard tie decisions are append-only.')


class CompetitionLiveState(OwnedDomainRecord):
    """Runtime stage telemetry and projector state for an active competition arena."""

    competition = models.OneToOneField(Competition, on_delete=models.CASCADE,
        related_name='live_state')
    active_round = models.ForeignKey(CompetitionRound, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='active_in_live_states')
    active_entry = models.ForeignKey(CompetitionEntry, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='active_in_live_states')
    pitch_seconds_remaining = models.PositiveIntegerField(default=180)
    timer_paused = models.BooleanField(default=False)
    audience_voting_open = models.BooleanField(default=True)
    projector_mode = models.CharField(max_length=32, default='stage')
    broadcast_banner = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ('pk',)

    def __str__(self):
        return f'{self.competition.name} Live State'


class CombinedAssessmentQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Combined assessment policy changes must use validated model saves.')

    def delete(self):
        if self.exclude(status=CombinedPresentationAssessment.Status.DRAFT).exists():
            raise ValidationError('Published combined assessments cannot be bulk deleted.')
        return super().delete()


class CombinedPresentationAssessment(OwnedDomainRecord):
    """Policy link joining Classroom peer assessment to a numeric Panel source.

    The source services remain systems of record for submissions and workflow.
    This model owns their relationship and the declared course result policy.
    """

    class ResultPolicy(models.TextChoices):
        SEPARATE = 'separate', 'Report peer and Panel results separately'
        WEIGHTED = 'weighted', 'Calculate a weighted numeric mark'

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        ACTIVE = 'active', 'Active'
        CLOSED = 'closed', 'Closed'

    name = models.CharField(max_length=160)
    classroom_session = models.OneToOneField(
        'assessments.AssessmentSession', on_delete=models.PROTECT,
        related_name='combined_assessment')
    panel_hearing = models.OneToOneField(
        PanelHearing, on_delete=models.PROTECT, related_name='combined_assessment')
    result_policy = models.CharField(max_length=12, choices=ResultPolicy.choices,
        default=ResultPolicy.SEPARATE)
    peer_weight = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('50'))
    panel_weight = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('50'))
    moderation_threshold = models.DecimalField(max_digits=5, decimal_places=2,
        null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='For weighted results, require a reasoned review when source scores differ by more than this many points.')
    minimum_peer_ratings = models.PositiveSmallIntegerField(default=1,
        validators=[MinValueValidator(1), MaxValueValidator(500)])
    minimum_panel_raters = models.PositiveSmallIntegerField(default=1,
        validators=[MinValueValidator(1), MaxValueValidator(50)])
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return self.name

    objects = OwnedManager.from_queryset(CombinedAssessmentQuerySet)()

    def save(self, *args, **kwargs):
        if self.pk:
            prior = type(self).objects.filter(pk=self.pk).first()
            if prior and prior.status != self.Status.DRAFT:
                protected = ('classroom_session_id', 'panel_hearing_id', 'result_policy',
                    'peer_weight', 'panel_weight', 'moderation_threshold',
                    'minimum_peer_ratings', 'minimum_panel_raters')
                if any(getattr(prior, field) != getattr(self, field) for field in protected):
                    raise ValidationError('Published result policy is locked; create a new assessment revision.')
                if self.status == self.Status.DRAFT:
                    raise ValidationError('A published combined assessment cannot return to draft.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != self.Status.DRAFT:
            raise ValidationError('Published combined assessments cannot be deleted.')
        return super().delete(*args, **kwargs)

    def clean(self):
        super().clean()
        if not (self.classroom_session_id and self.panel_hearing_id):
            return
        if self.classroom_session.organization_id != self.organization_id:
            raise ValidationError({'classroom_session': 'Choose a session in this organization.'})
        if self.classroom_session.created_by_id != self.created_by_id:
            raise ValidationError({'classroom_session': 'The linked sources must have the same owner.'})
        if self.classroom_session.service != 'classroom':
            raise ValidationError({'classroom_session': 'Only Classroom sessions can be linked.'})
        if self.result_policy == self.ResultPolicy.WEIGHTED:
            if self.peer_weight + self.panel_weight != Decimal('100'):
                raise ValidationError('Peer and Panel weights must total 100%.')
            if self.moderation_threshold is None:
                raise ValidationError({'moderation_threshold': 'Set the score-difference threshold for moderation.'})
            if self.panel_hearing.workflow not in (
                    PanelHearing.Workflow.SCORED, PanelHearing.Workflow.HYBRID):
                raise ValidationError({'result_policy':
                    'Weighted marks require a numeric Panel scoring workflow.'})
        if self.pk and self.status != self.Status.DRAFT:
            prior = type(self).objects.filter(pk=self.pk).first()
            protected = ('classroom_session_id', 'panel_hearing_id', 'result_policy',
                'peer_weight', 'panel_weight', 'moderation_threshold',
                'minimum_peer_ratings', 'minimum_panel_raters')
            if prior and prior.status != self.Status.DRAFT and any(
                    getattr(prior, field) != getattr(self, field) for field in protected):
                raise ValidationError('Published result policy is locked; create a new assessment revision.')


class CombinedMappingQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Presentation mappings must use validated model saves.')

    def delete(self):
        if self.exclude(assessment__status=CombinedPresentationAssessment.Status.DRAFT).exists():
            raise ValidationError('Mappings for published assessments cannot be deleted.')
        return super().delete()


class CombinedPresentationMapping(OwnedDomainRecord):
    """Explicit mapping; names are never used as score keys."""

    assessment = models.ForeignKey(CombinedPresentationAssessment,
        on_delete=models.CASCADE, related_name='mappings')
    group = models.OneToOneField('assessments.PresentationGroup',
        on_delete=models.PROTECT, related_name='combined_assessment_mapping')
    candidate = models.OneToOneField(PanelCandidate, on_delete=models.PROTECT,
        related_name='combined_assessment_mapping')
    group_member_ids_snapshot = models.JSONField(default=list, blank=True,
        help_text='Classroom student IDs captured when this entry mapping is confirmed.')

    class Meta:
        ordering = ('group__order', 'pk')
        constraints = [
            models.UniqueConstraint(fields=('assessment', 'group'), name='combined_assessment_group_once'),
            models.UniqueConstraint(fields=('assessment', 'candidate'), name='combined_assessment_candidate_once'),
        ]

    objects = OwnedManager.from_queryset(CombinedMappingQuerySet)()

    def save(self, *args, **kwargs):
        if self.pk and self.assessment.status != CombinedPresentationAssessment.Status.DRAFT:
            raise ValidationError('Mappings are locked after publishing.')
        if self._state.adding and not self.group_member_ids_snapshot and self.group_id:
            self.group_member_ids_snapshot = sorted(self.group.memberships.values_list('student_id', flat=True))
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.assessment.status != CombinedPresentationAssessment.Status.DRAFT:
            raise ValidationError('Mappings are locked after publishing.')
        return super().delete(*args, **kwargs)

    def clean(self):
        super().clean()
        if not (self.assessment_id and self.group_id and self.candidate_id):
            return
        if self.group.assessment_session_id != self.assessment.classroom_session_id:
            raise ValidationError({'group': 'The group must belong to the linked Classroom session.'})
        if self.candidate.hearing_id != self.assessment.panel_hearing_id:
            raise ValidationError({'candidate': 'The candidate must belong to the linked Panel hearing.'})
        if self.group.is_practice:
            raise ValidationError({'group': 'Practice groups cannot be combined assessment entries.'})
        if self.candidate.source_group_id and self.candidate.source_group_id != self.group_id:
            raise ValidationError({'candidate': 'This Panel entry is linked to a different Classroom group.'})


class CombinedModerationQuerySet(OwnedQuerySet):
    def update(self, **kwargs):
        raise ValidationError('Combined moderation decisions are append-only.')

    def delete(self):
        raise ValidationError('Combined moderation decisions are append-only.')


class CombinedModerationDecision(OwnedDomainRecord):
    """Append-only moderator disposition for a flagged score difference."""

    class Decision(models.TextChoices):
        APPROVE = 'approve', 'Accept the weighted result with explanation'
        HOLD = 'hold', 'Hold the result for source correction or further review'

    mapping = models.ForeignKey(CombinedPresentationMapping, on_delete=models.PROTECT,
        related_name='moderation_decisions')
    decision = models.CharField(max_length=10, choices=Decision.choices)
    reason = models.TextField(max_length=1200)
    peer_score_at_review = models.DecimalField(max_digits=9, decimal_places=6)
    panel_score_at_review = models.DecimalField(max_digits=9, decimal_places=6)
    revision = models.PositiveSmallIntegerField(default=1)
    supersedes = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT,
        related_name='corrections')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='combined_moderation_reviews')

    class Meta:
        ordering = ('mapping_id', 'revision')
        constraints = [models.UniqueConstraint(fields=('mapping', 'revision'),
            name='combined_moderation_revision_unique')]

    objects = OwnedManager.from_queryset(CombinedModerationQuerySet)()

    def clean(self):
        super().clean()
        if not self.reason or not self.reason.strip():
            raise ValidationError({'reason': 'A reason is required for moderation.'})
        if self.supersedes_id:
            if self.supersedes.mapping_id != self.mapping_id or self.supersedes.revision + 1 != self.revision:
                raise ValidationError({'supersedes': 'A correction must supersede the previous review.'})
        elif self.revision != 1:
            raise ValidationError({'revision': 'The first review revision must be one.'})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Combined moderation decisions are append-only.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Combined moderation decisions are append-only.')


class CoAssessmentSession(OwnedDomainRecord):
    """Aggregate root for a unified co-assessment event."""

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        ACTIVE = 'active', 'Live on Stage'
        REVIEW = 'review', 'In Moderation'
        SIGNED_OFF = 'signed_off', 'Signed Off'
        RELEASED = 'released', 'Results Released'

    name = models.CharField(max_length=160)
    description = models.CharField(max_length=500, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)
    panel_weight = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('70.00'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))])
    peer_weight = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('30.00'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))])
    moderation_threshold = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('15.00'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
        help_text='Score discrepancy between panel and peer marks requiring chair review.')
    minimum_panel_evaluators = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(50)])
    minimum_peer_evaluators = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(500)])
    presentation_duration_seconds = models.PositiveIntegerField(default=600)
    qa_duration_seconds = models.PositiveIntegerField(default=300)
    public_join_code = models.CharField(max_length=16, unique=True, editable=False)
    allow_anonymous_peer_comments = models.BooleanField(default=True)
    signed_off_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if (self.panel_weight + self.peer_weight) != Decimal('100.00'):
            raise ValidationError({'peer_weight': 'Panel and Peer weights must total exactly 100%.'})

    def save(self, *args, **kwargs):
        if not self.public_join_code:
            import secrets
            self.public_join_code = secrets.token_hex(4).upper()
        self.full_clean()
        return super().save(*args, **kwargs)

    def rubric_total_weight(self):
        """Calculate the sum of all criterion weights."""
        return sum((c.weight for c in self.criteria.all()), Decimal('0'))

    def is_rubric_balanced(self):
        """Check whether the criterion weights sum to 100 percent."""
        return self.rubric_total_weight() == Decimal('100.00')

    def group_criteria(self):
        """Return criteria scoped to the overall team presentation."""
        return self.criteria.filter(scope=CoAssessmentCriterion.Scope.GROUP)

    def individual_criteria(self):
        """Return criteria scoped to individual student members."""
        return self.criteria.filter(scope=CoAssessmentCriterion.Scope.INDIVIDUAL)

    def group_weight_total(self):
        """Sum of weights across all group-scoped criteria."""
        return sum((c.weight for c in self.group_criteria()), Decimal('0'))

    def individual_weight_total(self):
        """Sum of weights across all individual-scoped criteria."""
        return sum((c.weight for c in self.individual_criteria()), Decimal('0'))


class CoAssessmentCriterion(OwnedDomainRecord):
    """Shared rubric criterion scored by both panel and peer evaluators."""

    class Scope(models.TextChoices):
        GROUP = 'group', 'Group / project'
        INDIVIDUAL = 'individual', 'Individual student'

    session = models.ForeignKey(
        CoAssessmentSession, on_delete=models.CASCADE, related_name='criteria')
    name = models.CharField(max_length=140)
    description = models.TextField(blank=True, max_length=1000)
    scope = models.CharField(
        max_length=12, choices=Scope.choices, default=Scope.GROUP, db_index=True)
    weight = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('25.00'),
        validators=[MinValueValidator(Decimal('0.01')), MaxValueValidator(Decimal('100'))])
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('order', 'pk')
        constraints = [
            models.UniqueConstraint(
                fields=('session', 'name'),
                name='coassessment_criterion_name_unique'),
        ]

    def __str__(self):
        return f'{self.name} ({self.get_scope_display()}, {self.weight}%)'


class CoAssessmentLevelDescriptor(OwnedDomainRecord):
    """Score anchor with descriptive performance guidance."""

    criterion = models.ForeignKey(
        CoAssessmentCriterion, on_delete=models.CASCADE, related_name='descriptors')
    score = models.DecimalField(max_digits=5, decimal_places=2)
    label = models.CharField(max_length=60)
    guidance = models.CharField(max_length=MAX_LEVEL_DESCRIPTION)

    class Meta:
        ordering = ('score', 'pk')


class CoAssessmentGroup(OwnedDomainRecord):
    """Presenting team in the scheduled lineup."""

    session = models.ForeignKey(
        CoAssessmentSession, on_delete=models.CASCADE, related_name='groups')
    name = models.CharField(max_length=140)
    topic = models.CharField(max_length=255, blank=True)
    location = models.CharField(max_length=120, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('order', 'pk')

    def __str__(self):
        return self.name


class CoAssessmentParticipant(OwnedDomainRecord):
    """Student roster member who can evaluate presentations and present."""

    session = models.ForeignKey(
        CoAssessmentSession, on_delete=models.CASCADE, related_name='participants')
    group = models.ForeignKey(
        CoAssessmentGroup, null=True, blank=True, on_delete=models.SET_NULL, related_name='members')
    full_name = models.CharField(max_length=150)
    student_id = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)

    class Meta:
        ordering = ('full_name',)
        constraints = [
            models.UniqueConstraint(
                fields=('session', 'student_id'),
                condition=~models.Q(student_id=''),
                name='coassessment_participant_student_id_unique'),
        ]

    def __str__(self):
        return self.full_name or self.student_id


class CoAssessmentExaminer(OwnedDomainRecord):
    """Panel examiner with a secure token digest."""

    session = models.ForeignKey(
        CoAssessmentSession, on_delete=models.CASCADE, related_name='examiners')
    email = models.EmailField()
    display_name = models.CharField(max_length=120)
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    invite_token = models.CharField(max_length=64, blank=True, default='')
    is_chair = models.BooleanField(default=False)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('display_name',)

    def __str__(self):
        return f'{self.display_name} ({self.email})'


class CoAssessmentTurn(OwnedDomainRecord):
    """Presentation stage window for a group."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACTIVE = 'active', 'Active on Stage'
        CLOSED = 'closed', 'Completed'

    session = models.ForeignKey(
        CoAssessmentSession, on_delete=models.CASCADE, related_name='turns')
    group = models.ForeignKey(
        CoAssessmentGroup, on_delete=models.CASCADE, related_name='turns')
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    is_paused = models.BooleanField(default=False)
    paused_at = models.DateTimeField(null=True, blank=True)
    total_paused_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('group__order', 'pk')

    def __str__(self):
        return f'{self.group.name} : {self.get_status_display()}'


class CoAssessmentScorecard(OwnedDomainRecord):
    """Evaluation submission from an individual peer or panel examiner."""

    class Tier(models.TextChoices):
        PANEL = 'panel', 'Panel Examiner'
        PEER = 'peer', 'Student Peer'

    turn = models.ForeignKey(
        CoAssessmentTurn, on_delete=models.CASCADE, related_name='scorecards')
    evaluator_tier = models.CharField(max_length=10, choices=Tier.choices)
    peer_evaluator = models.ForeignKey(
        CoAssessmentParticipant, null=True, blank=True, on_delete=models.CASCADE,
        related_name='peer_scorecards')
    examiner_evaluator = models.ForeignKey(
        CoAssessmentExaminer, null=True, blank=True, on_delete=models.CASCADE,
        related_name='examiner_scorecards')
    qualitative_feedback = models.TextField(blank=True, max_length=2000)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('turn', 'peer_evaluator'),
                condition=models.Q(peer_evaluator__isnull=False),
                name='coassessment_one_peer_scorecard_per_turn'),
            models.UniqueConstraint(
                fields=('turn', 'examiner_evaluator'),
                condition=models.Q(examiner_evaluator__isnull=False),
                name='coassessment_one_examiner_scorecard_per_turn'),
        ]

    def clean(self):
        super().clean()
        if self.evaluator_tier == self.Tier.PEER:
            if not self.peer_evaluator_id:
                raise ValidationError({'peer_evaluator': 'Peer scorecard must link to a valid participant.'})
            if self.peer_evaluator.group_id == self.turn.group_id:
                raise ValidationError({'peer_evaluator': 'Students cannot evaluate their own team.'})
        elif self.evaluator_tier == self.Tier.PANEL:
            if not self.examiner_evaluator_id:
                raise ValidationError({'examiner_evaluator': 'Panel scorecard must link to an invited examiner.'})


class CoAssessmentScoreValue(models.Model):
    """Individual criterion score within a scorecard."""

    scorecard = models.ForeignKey(
        CoAssessmentScorecard, on_delete=models.CASCADE, related_name='values')
    criterion = models.ForeignKey(
        CoAssessmentCriterion, on_delete=models.PROTECT, related_name='values')
    target_participant = models.ForeignKey(
        'CoAssessmentParticipant', null=True, blank=True, on_delete=models.CASCADE,
        related_name='received_scores')
    score = models.DecimalField(
        max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))])

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('scorecard', 'criterion'),
                condition=models.Q(target_participant__isnull=True),
                name='coassessment_unique_group_score_per_card'),
            models.UniqueConstraint(
                fields=('scorecard', 'criterion', 'target_participant'),
                condition=models.Q(target_participant__isnull=False),
                name='coassessment_unique_individual_score_per_card'),
        ]

    def clean(self):
        super().clean()
        if self.criterion_id:
            if self.criterion.scope == CoAssessmentCriterion.Scope.GROUP and self.target_participant_id is not None:
                raise ValidationError({'target_participant': 'Group criteria must not have a target participant.'})
            if self.criterion.scope == CoAssessmentCriterion.Scope.INDIVIDUAL and self.target_participant_id is None:
                raise ValidationError({'target_participant': 'Individual criteria require a target participant.'})


class CoAssessmentModeration(OwnedDomainRecord):
    """Chair moderation record for a turn."""

    class Action(models.TextChoices):
        APPROVE_COMBINED = 'approve', 'Approve combined mark'
        OVERRIDE_PANEL = 'override_panel', 'Override with panel score only'
        ADJUSTED = 'adjusted', 'Apply calibrated score'

    turn = models.ForeignKey(
        CoAssessmentTurn, on_delete=models.CASCADE, related_name='moderations')
    action = models.CharField(max_length=20, choices=Action.choices)
    panel_score_snapshot = models.DecimalField(max_digits=5, decimal_places=2)
    peer_score_snapshot = models.DecimalField(max_digits=5, decimal_places=2)
    final_score = models.DecimalField(max_digits=5, decimal_places=2)
    justification = models.TextField(max_length=1500)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='coassessment_moderations')

    class Meta:
        ordering = ('-created_at',)

