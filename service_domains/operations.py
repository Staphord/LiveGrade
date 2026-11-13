"""Safe, transaction-aware operations for the planned service use cases.

Views must scope each operation to the active service, organization, and owner.
Each service remains closed until its complete actor journey and release
evidence exist.
"""

import hashlib
import hmac
import json
import math
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from django.core.exceptions import PermissionDenied, ValidationError
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone
from django.utils.crypto import salted_hmac

from assessments.duplication import SESSION_SETTINGS, values
from assessments.models import AssessmentSession, GroupMembership, PresentationGroup, Student
from assessments.session_templates import (StarterTemplate, blueprint,
                                           save_draft_from_template, starters_for)
from assessments.models import SessionTemplate

from .models import (
    CombinedPresentationAssessment, CombinedPresentationMapping,
    PanelAuditEvent, PanelAbstention, PanelCandidate, PanelCommentRedaction, PanelConflict,
    PanelCriterion, PanelCriterionLevel, PanelHearing, PanelScoringPanel,
    PanelInvitation, PanelInvitationAttempt, PanelScore, PanelResultSnapshot, PanelResultAdjustment,
    PanelEntrantResultLink, PanelLiveDisplayLink, PanelDecision, PanelVote, PanelWrittenReview,
    ShowcaseCertificate, ShowcaseEvent, ShowcaseEventAudit, ShowcaseConsentAudit,
    ShowcaseExhibit,
    ShowcaseJudgeAssignment, ShowcaseJudgeInvitation, ShowcaseJudgeExhibitAssignment,
    ShowcaseJudgeCriterion, ShowcaseJudgeEvaluation, ShowcaseJudgeConflict, ShowcaseRating,
    TrainingCompetency, TrainingCompetencyResult, TrainingFeedback, TrainingParticipant, TrainingSeries,
    TrainingSeriesSession, TrainingSessionCompetency, TrainingCertificate,
    Competition, CompetitionAdvancementDecision, CompetitionAudienceBallot, CompetitionCriterion, CompetitionEntry,
    CompetitionLeaderboardAudit,
    CompetitionLeaderboardTieDecision, CompetitionRound, CompetitionRoundEntry,
    CompetitionTrack, CompetitionTransitionAudit,
    CompetitionJudgeInvitation, CompetitionJudgeScore,
    CompetitionJudgeAudit, CompetitionJudgeAbstention,
    CompetitionWeightedResultAudit, CompetitionAudienceJoinCode,
    CompetitionLiveState,
)
from .realtime import schedule_competition_leaderboard_update
from .rules import (advance_entries, aggregate_anonymous_feedback, aggregate_scores,
                   ranked_choice_result, showcase_judge_score)


MAX_SHOWCASE_JUDGE_ASSIGNMENTS = 5000
COMPETITION_JOIN_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
PANEL_DEVICE_COOKIE_SALT = 'livegrade.panel.examiner.device.v1'
PANEL_INVITATION_QUEUE_STALE_AFTER = timedelta(minutes=5)
PANEL_INVITATION_SENDING_STALE_AFTER = timedelta(minutes=15)


def competition_audience_join_code(competition, round_obj, track):
    """Derive a printable, round-scoped audience code without storing a bearer secret."""
    message = f'{competition.public_id}:{round_obj.pk}:{track.pk}'
    digest = salted_hmac('livegrade.competition.audience.join.v1', message,
        algorithm='sha256').digest()
    bits = int.from_bytes(digest[:5], 'big')
    chars = []
    for shift in range(35, -1, -5):
        chars.append(COMPETITION_JOIN_CODE_ALPHABET[(bits >> shift) & 31])
    code = ''.join(chars)
    from .models import CompetitionAudienceJoinCode
    for attempt in range(8):
        try:
            with transaction.atomic():
                routing, _ = CompetitionAudienceJoinCode.objects.get_or_create(
                    round=round_obj, track=track, defaults={'code': code})
            return routing.code
        except IntegrityError:
            code = ''.join(secrets.choice(COMPETITION_JOIN_CODE_ALPHABET) for _ in range(8))
    raise ValidationError('Unable to issue a unique audience code. Please retry.')


def owned_rows(model, organization, user):
    """Fail-closed queryset for the active organization and creator."""
    return model.objects.for_organization(organization).owned_by(user)


def _record_panel_audit(hearing, action, reason, *, invitation=None, candidate=None, actor_user=None):
    event = PanelAuditEvent(hearing=hearing, invitation=invitation, candidate=candidate,
        actor_user=actor_user, action=action, reason=reason,
        organization_id=hearing.organization_id, created_by=hearing.created_by)
    event.full_clean()
    event.save(force_insert=True)
    return event


def record_panel_invitation_attempt(invitation, action, outcome, reason):
    attempt = PanelInvitationAttempt(invitation=invitation, action=action,
        outcome=outcome, reason=reason, organization_id=invitation.organization_id,
        created_by=invitation.created_by)
    attempt.full_clean()
    attempt.save(force_insert=True)
    return attempt


@transaction.atomic
def open_panel_hearing(hearing, *, organization, user):
    """Open a fully configured draft hearing and record the owner action."""
    locked = (PanelHearing.objects.for_organization(organization).owned_by(user)
              .select_for_update().filter(pk=hearing.pk).first())
    if locked is None:
        raise PermissionDenied
    if locked.status != PanelHearing.Status.DRAFT:
        raise ValidationError('Only a draft hearing can be opened.')
    scoring_required = locked.workflow in (PanelHearing.Workflow.SCORED, PanelHearing.Workflow.HYBRID)
    voting_required = locked.workflow in (PanelHearing.Workflow.VOTE, PanelHearing.Workflow.HYBRID,
                                           PanelHearing.Workflow.REVIEW_VOTE)
    review_required = locked.workflow in (PanelHearing.Workflow.REVIEW,
                                           PanelHearing.Workflow.REVIEW_VOTE)
    if not locked.candidates.exists() or (scoring_required and not locked.criteria.exists()):
        raise ValidationError('Add at least one candidate and the required scoring criteria before opening.')
    panels = list(PanelScoringPanel.objects.filter(hearing=locked).order_by('pk'))
    if not panels:
        panels = [create_default_panel(locked)]
    if scoring_required and sum((panel.share for panel in panels), Decimal('0')) != Decimal('100.00'):
        raise ValidationError('Panel shares must total exactly 100% before the hearing can open.')
    if scoring_required and any(not panel.criteria.exists() for panel in panels):
        raise ValidationError('Assign at least one criterion to every panel before opening.')
    if scoring_required:
        unassigned = [criterion.name for criterion in locked.criteria.all()
            if not any(panel.share > 0 and panel.criteria.filter(pk=criterion.pk).exists()
                       for panel in panels)]
        if unassigned:
            raise ValidationError('Assign every scored criterion to a panel with a positive share: '
                + ', '.join(unassigned[:10]))
    for panel in panels if scoring_required or voting_required or review_required else []:
        if scoring_required and panel.share <= 0:
            continue
        accepted_reviewers = PanelInvitation.objects.filter(hearing=locked, panel=panel,
            confirmed_at__isnull=False, accepted_at__isnull=True, declined_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now()).count()
        if accepted_reviewers < panel.minimum_examiners:
            raise ValidationError(
                f'Panel “{panel.name}” has {accepted_reviewers} accepted examiner links; '
                f'{panel.minimum_examiners} are required before opening.')
    stage_profiles = {
        PanelHearing.Workflow.SCORED: ['prepare', 'conflicts', 'independent_scoring', 'moderation', 'signoff', 'release'],
        PanelHearing.Workflow.OUTCOME: ['prepare', 'conflicts', 'chair_outcome', 'signoff', 'release'],
        PanelHearing.Workflow.VOTE: ['prepare', 'conflicts', 'private_vote', 'chair_outcome', 'signoff', 'release'],
        PanelHearing.Workflow.HYBRID: ['prepare', 'conflicts', 'independent_scoring', 'private_vote',
            'chair_outcome', 'moderation', 'signoff', 'release'],
        PanelHearing.Workflow.REVIEW: ['prepare', 'conflicts', 'independent_written_review',
            'chair_outcome', 'signoff', 'release'],
        PanelHearing.Workflow.REVIEW_VOTE: ['prepare', 'conflicts', 'independent_written_review',
            'private_vote', 'chair_outcome', 'signoff', 'release'],
    }
    workflow_snapshot = {
        'version': 1,
        'workflow': locked.workflow,
        'stages': stage_profiles[locked.workflow],
        'minimum_examiners': locked.minimum_examiners,
        'aggregation': locked.aggregation,
        'trim_fraction': str(locked.trim_fraction) if locked.trim_fraction is not None else None,
        'hearing_date': locked.hearing_date.isoformat() if locked.hearing_date else None,
        'venue': locked.venue,
        'candidates': [{'id': candidate.pk, 'name': candidate.display_name,
                        'reference': candidate.reference, 'order': candidate.order,
                        'scheduled_time': candidate.scheduled_time.isoformat()
                            if candidate.scheduled_time else None}
                       for candidate in locked.candidates.order_by('order', 'pk')],
        'criteria': [{'id': criterion.pk, 'name': criterion.name,
                      'description': criterion.description,
                      'order': criterion.order,
                      'levels': [{'score': str(level.score), 'description': level.description}
                          for level in criterion.levels.order_by('score', 'pk')],
                      'panels': list(criterion.panels.values_list('name', flat=True))}
                     for criterion in locked.criteria.order_by('order', 'pk')],
        'panels': [{'name': panel.name, 'share': str(panel.share),
                    'minimum_examiners': panel.minimum_examiners,
                    'aggregation': panel.aggregation,
                    'trim_fraction': str(panel.trim_fraction)
                        if panel.trim_fraction is not None else None,
                    'criteria': list(panel.criteria.values_list('name', flat=True))}
                   for panel in panels],
    }
    canonical_snapshot = json.dumps(workflow_snapshot, sort_keys=True, separators=(',', ':'))
    workflow_snapshot['sha256'] = hashlib.sha256(canonical_snapshot.encode('utf-8')).hexdigest()
    locked.active_candidate = None
    now = timezone.now()
    workflow_snapshot['chamber_state'] = {
        'active_phase': 'presentation',
        'phase_duration_seconds': 900,
        'phase_seconds_remaining': 900,
        'phase_started_at': now.isoformat(),
        'elapsed_before_pause': 0,
        'timer_paused': False,
        'timer_paused_at': None,
        'display_mode': 'in_session',
    }
    locked.workflow_snapshot = workflow_snapshot
    locked.status = PanelHearing.Status.OPEN
    locked.save(update_fields=('status', 'active_candidate', 'workflow_snapshot', 'updated_at'))
    _record_panel_audit(locked, PanelAuditEvent.Action.HEARING_OPENED,
        f'Hearing opened using the {locked.get_workflow_display()} workflow; '
        f'configuration SHA-256 {locked.workflow_snapshot["sha256"]}.', actor_user=user)
    return locked


@transaction.atomic
def activate_panel_candidate(hearing, candidate, *, organization, user):
    """Select the single candidate examiners may score at this moment."""
    locked = (PanelHearing.objects.for_organization(organization).owned_by(user)
              .select_for_update().filter(pk=hearing.pk).first())
    if locked is None:
        raise PermissionDenied
    if locked.status != PanelHearing.Status.OPEN:
        raise ValidationError('Candidates can only be activated while scoring is open.')
    candidate = PanelCandidate.objects.filter(pk=candidate.pk, hearing=locked).first()
    if candidate is None:
        raise PermissionDenied
    if locked.active_candidate_id == candidate.pk:
        return locked
    locked.active_candidate = candidate
    locked.save(update_fields=('active_candidate', 'updated_at'))
    _record_panel_audit(locked, PanelAuditEvent.Action.CANDIDATE_ACTIVATED,
        'Chair activated candidate for examiner scoring.', candidate=candidate, actor_user=user)
    return locked


@transaction.atomic
def deactivate_panel_candidate(hearing, *, organization, user):
    """Pause scoring between candidates while keeping the hearing open."""
    locked = (PanelHearing.objects.for_organization(organization).owned_by(user)
              .select_for_update().filter(pk=hearing.pk).first())
    if locked is None:
        raise PermissionDenied
    if locked.status != PanelHearing.Status.OPEN:
        raise ValidationError('Candidate scoring can only be ended while the hearing is open.')
    candidate = locked.active_candidate
    if candidate is None:
        return locked
    locked.active_candidate = None
    locked.save(update_fields=('active_candidate', 'updated_at'))
    _record_panel_audit(locked, PanelAuditEvent.Action.CANDIDATE_DEACTIVATED,
        'Chair ended scoring for active candidate.', candidate=candidate, actor_user=user)
    return locked


def create_default_panel(hearing):
    """Create a legacy-equivalent 100% invited panel for a hearing."""
    panel = PanelScoringPanel(
        hearing=hearing, organization_id=hearing.organization_id, created_by=hearing.created_by,
        name='Default panel', share=Decimal('100.00'), aggregation=hearing.aggregation,
        minimum_examiners=hearing.minimum_examiners, trim_fraction=hearing.trim_fraction)
    panel.full_clean()
    panel.save(force_insert=True)
    for criterion in hearing.criteria.all():
        criterion.panels.add(panel)
    return panel


@transaction.atomic
def issue_panel_invitation(hearing, email, expires_at, *, organization, user, panel=None):
    """Issue an examiner bearer token; persist only its SHA-256 digest."""
    hearing = (PanelHearing.objects.for_organization(organization).owned_by(user)
               .select_for_update().filter(pk=hearing.pk).first())
    if hearing is None:
        raise PermissionDenied
    if hearing.status not in (PanelHearing.Status.DRAFT, PanelHearing.Status.OPEN):
        raise ValidationError('This hearing is not accepting examiner invitations.')
    _ensure_panel_invitation_address_available(hearing, email)
    if panel is None:
        panel = PanelScoringPanel.objects.filter(hearing=hearing).order_by('pk').first()
        if panel is None:
            panel = create_default_panel(hearing)
    else:
        panel = PanelScoringPanel.objects.filter(hearing=hearing, pk=panel.pk).first()
        if panel is None:
            raise PermissionDenied
    raw_token = secrets.token_urlsafe(32)
    invitation = PanelInvitation(
        hearing=hearing, panel=panel, organization_id=hearing.organization_id, created_by=user,
        email=email, token_digest=PanelInvitation.digest_token(raw_token), expires_at=expires_at)
    invitation.full_clean()
    invitation.save()
    _record_panel_audit(hearing, PanelAuditEvent.Action.INVITATION_ISSUED,
        'Examiner invitation issued.', invitation=invitation, actor_user=user)
    return invitation, raw_token


@transaction.atomic
def queue_panel_invitation(hearing, email, expires_at, *, organization, user, panel=None):
    """Create an invitation whose bearer token will be generated by the mail worker."""
    locked = (PanelHearing.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=hearing.pk).first())
    if locked is None:
        raise PermissionDenied
    if locked.status not in (PanelHearing.Status.DRAFT, PanelHearing.Status.OPEN):
        raise ValidationError('This hearing is not accepting examiner invitations.')
    _ensure_panel_invitation_address_available(locked, email)
    if panel is None:
        panel = PanelScoringPanel.objects.filter(hearing=locked).order_by('pk').first()
        if panel is None:
            panel = create_default_panel(locked)
    else:
        panel = PanelScoringPanel.objects.filter(hearing=locked, pk=panel.pk).first()
        if panel is None:
            raise PermissionDenied
    invitation = PanelInvitation(
        hearing=locked, panel=panel, organization_id=locked.organization_id,
        created_by=user, email=email, token_digest=None, expires_at=expires_at,
        delivery_status=PanelInvitation.DeliveryStatus.PENDING)
    invitation.full_clean()
    invitation.save(force_insert=True)
    _record_panel_audit(locked, PanelAuditEvent.Action.INVITATION_ISSUED,
        'Examiner invitation queued for email delivery.', invitation=invitation, actor_user=user)
    return invitation


@transaction.atomic
def apply_panel_invitation_import(hearing, parsed, expires_at, *, organization, user, panel=None):
    """Atomically queue a fully validated examiner list and audit each invitation."""
    locked = (PanelHearing.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=hearing.pk, status=PanelHearing.Status.DRAFT).first())
    if locked is None:
        raise PermissionDenied
    if parsed.get('fatal') or parsed.get('errors') or not parsed.get('rows'):
        raise ValidationError('Fix all spreadsheet errors before applying the examiner import.')
    if isinstance(expires_at, str):
        try:
            expires_at = timezone.datetime.fromisoformat(expires_at)
        except (TypeError, ValueError):
            raise ValidationError('The examiner invitation expiry is invalid.')
        if timezone.is_naive(expires_at):
            expires_at = timezone.make_aware(expires_at, timezone.get_current_timezone())
    if panel is None:
        panel = PanelScoringPanel.objects.filter(hearing=locked).order_by('pk').first()
        if panel is None:
            panel = create_default_panel(locked)
    else:
        panel = PanelScoringPanel.objects.filter(hearing=locked, pk=panel.pk).first()
        if panel is None:
            raise PermissionDenied
    emails = [item['email'] for item in parsed['rows']]
    normalized = [email.casefold() for email in emails]
    if len(set(normalized)) != len(normalized):
        raise ValidationError('Examiner email addresses must be unique in the spreadsheet.')
    active = {email.casefold() for email in PanelInvitation.objects.filter(hearing=locked,
        revoked_at__isnull=True, declined_at__isnull=True, accepted_at__isnull=True,
        expires_at__gt=timezone.now()
        ).values_list('email', flat=True)}
    if any(email in active for email in normalized):
        raise ValidationError('An active invitation already exists for an examiner in this spreadsheet.')
    if expires_at <= timezone.now():
        raise ValidationError('Invitation expiry must be in the future.')
    invitations = []
    for email in emails:
        invitation = PanelInvitation(hearing=locked, panel=panel,
            organization_id=locked.organization_id, created_by=user, email=email,
            token_digest=None, expires_at=expires_at,
            delivery_status=PanelInvitation.DeliveryStatus.PENDING)
        invitation.full_clean()
        invitation.save(force_insert=True)
        _record_panel_audit(locked, PanelAuditEvent.Action.INVITATION_ISSUED,
            'Examiner invitation imported and queued for email delivery.',
            invitation=invitation, actor_user=user)
        invitations.append(invitation)
    return invitations


@transaction.atomic
def request_panel_invitation_resend(hearing, invitation, *, organization, user):
    """Rotate the outstanding capability and queue one audited delivery attempt."""
    locked_hearing = (PanelHearing.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=hearing.pk).first())
    if locked_hearing is None:
        raise PermissionDenied
    locked = (PanelInvitation.objects.select_for_update()
        .filter(pk=invitation.pk, hearing=locked_hearing).first())
    if locked is None:
        raise PermissionDenied
    if locked_hearing.status not in (PanelHearing.Status.DRAFT, PanelHearing.Status.OPEN):
        raise ValidationError('This hearing is not accepting examiner invitations.')
    if (locked.revoked_at or locked.declined_at or locked.accepted_at or locked.confirmed_at
            or locked.expires_at <= timezone.now()):
        raise ValidationError('Only an active, unexpired invitation can be resent.')
    if not panel_invitation_resend_available(locked):
        if locked.delivery_status == PanelInvitation.DeliveryStatus.SENDING:
            raise ValidationError('This invitation already has a delivery attempt in progress.')
        raise ValidationError('This invitation is already queued for delivery.')
    locked.token_digest = None
    locked.delivery_status = PanelInvitation.DeliveryStatus.PENDING
    locked.delivery_error = ''
    locked.save(update_fields=('token_digest', 'delivery_status', 'delivery_error', 'updated_at'))
    _record_panel_audit(locked_hearing, PanelAuditEvent.Action.INVITATION_RESENT,
        'Owner requested a fresh examiner invitation email.', invitation=locked, actor_user=user)
    return locked


def panel_invitation_resend_available(invitation, *, at=None):
    """Whether a live invitation has a failed or stale delivery that can be retried."""
    now = at or timezone.now()
    if (invitation.revoked_at or invitation.declined_at or invitation.accepted_at or invitation.confirmed_at
            or invitation.expires_at <= now
            or invitation.hearing.status not in (PanelHearing.Status.DRAFT, PanelHearing.Status.OPEN)):
        return False
    if invitation.delivery_status == PanelInvitation.DeliveryStatus.SENDING:
        return invitation.updated_at <= now - PANEL_INVITATION_SENDING_STALE_AFTER
    if invitation.delivery_status == PanelInvitation.DeliveryStatus.PENDING:
        return invitation.updated_at <= now - PANEL_INVITATION_QUEUE_STALE_AFTER
    return invitation.delivery_status in (
        PanelInvitation.DeliveryStatus.FAILED, PanelInvitation.DeliveryStatus.SENT,
        PanelInvitation.DeliveryStatus.UNKNOWN)


@transaction.atomic
def mark_panel_invitation_enqueue_failed(invitation_id):
    invitation = PanelInvitation.objects.select_for_update().filter(pk=invitation_id).first()
    if invitation is None or invitation.delivery_status != PanelInvitation.DeliveryStatus.PENDING:
        return False
    invitation.delivery_status = PanelInvitation.DeliveryStatus.FAILED
    invitation.delivery_error = 'queue_unavailable'
    invitation.save(update_fields=('delivery_status', 'delivery_error', 'updated_at'))
    return True


def resolve_panel_invitation(raw_token, *, at=None):
    """Return an invitation only while its bearer token is valid."""
    if not raw_token or len(raw_token) > 256:
        return None
    digest = PanelInvitation.digest_token(raw_token)
    invitation = PanelInvitation.objects.select_related('hearing').filter(token_digest=digest).first()
    if invitation is None or not invitation.usable_at(at):
        return None
    return invitation


def _ensure_panel_invitation_address_available(hearing, email, *, at=None):
    """Prevent a second live invite to the same address for one hearing."""
    now = at or timezone.now()
    address = email.strip()
    if PanelInvitation.objects.filter(hearing=hearing, email__iexact=address,
            revoked_at__isnull=True, declined_at__isnull=True, accepted_at__isnull=True,
            expires_at__gt=now).exists():
        raise ValidationError('An active invitation already exists for this examiner. Resend it from the invitation list.')


@transaction.atomic
def decline_panel_invitation(invitation, *, at=None):
    """Record that the invited examiner declined and permanently close the link."""
    now = at or timezone.now()
    locked = (PanelInvitation.objects.select_for_update().select_related('hearing')
        .filter(pk=invitation.pk).first())
    if locked is None or not locked.usable_at(now) or locked.device_digest:
        raise ValidationError('This invitation can no longer be declined.')
    locked.declined_at = now
    locked.save(update_fields=('declined_at', 'updated_at'))
    _record_panel_audit(locked.hearing, PanelAuditEvent.Action.INVITATION_DECLINED,
        'Examiner declined the invitation.', invitation=locked)
    return locked


@transaction.atomic
def issue_panel_live_display_link(hearing, *, organization, user):
    """Create a fresh, revocable public link showing aggregate live progress only."""
    locked = (PanelHearing.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=hearing.pk).first())
    if locked is None:
        raise PermissionDenied
    if locked.status != PanelHearing.Status.OPEN:
        raise ValidationError('Open the assessment before creating a live display link.')
    now = timezone.now()
    for old_link in PanelLiveDisplayLink.objects.select_for_update().filter(
            hearing=locked, revoked_at__isnull=True):
        old_link.revoked_at = now
        old_link.save(update_fields=('revoked_at', 'updated_at'))
    token = secrets.token_urlsafe(32)
    link = PanelLiveDisplayLink(hearing=locked,
        organization_id=locked.organization_id, created_by=user,
        token_digest=PanelLiveDisplayLink.digest_token(token))
    link.full_clean()
    link.save(force_insert=True)
    _record_panel_audit(locked, PanelAuditEvent.Action.LIVE_DISPLAY_ISSUED,
        'A public aggregate live display link was issued.', actor_user=user)
    return link, token


@transaction.atomic
def revoke_panel_live_display_link(hearing, link, *, organization, user):
    """Revoke a public live display link while retaining its audit history."""
    locked_hearing = (PanelHearing.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=hearing.pk).first())
    if locked_hearing is None:
        raise PermissionDenied
    locked_link = (PanelLiveDisplayLink.objects.select_for_update()
        .filter(pk=link.pk, hearing=locked_hearing).first())
    if locked_link is None or locked_link.revoked_at is not None:
        raise ValidationError('This live display link is already inactive.')
    locked_link.revoked_at = timezone.now()
    locked_link.save(update_fields=('revoked_at', 'updated_at'))
    _record_panel_audit(locked_hearing, PanelAuditEvent.Action.LIVE_DISPLAY_REVOKED,
        'A public aggregate live display link was revoked.', actor_user=user)
    return locked_link


def resolve_panel_live_display_link(raw_token):
    """Resolve a public display bearer without exposing its stored token."""
    if not raw_token or len(raw_token) > 256:
        return None
    digest = PanelLiveDisplayLink.digest_token(raw_token)
    link = (PanelLiveDisplayLink.objects.select_related('hearing')
        .filter(Q(token_digest=digest) | Q(token_digest=raw_token)).first())
    return link if link and link.usable_at() else None


@transaction.atomic
def issue_panel_device_cookie(invitation):
    """Prepare a signed browser token without claiming the invitation on GET."""
    return signing.dumps({
        'invitation': invitation.pk,
        'nonce': secrets.token_urlsafe(32),
    }, salt=PANEL_DEVICE_COOKIE_SALT)


def valid_panel_device_cookie(invitation, cookie_token):
    if not cookie_token or len(cookie_token) > 512:
        return False
    try:
        payload = signing.loads(cookie_token, salt=PANEL_DEVICE_COOKIE_SALT)
    except (signing.BadSignature, TypeError, ValueError):
        return False
    return (isinstance(payload, dict) and payload.get('invitation') == invitation.pk
        and isinstance(payload.get('nonce'), str) and len(payload['nonce']) >= 32)


@transaction.atomic
def bind_panel_invitation_device(invitation, cookie_token, *, at=None):
    """Bind one invitation on explicit POST; only its signed-cookie digest persists."""
    now = at or timezone.now()
    locked = PanelInvitation.objects.select_for_update().select_related('hearing').filter(
        pk=invitation.pk).first()
    if locked is None or not locked.usable_at(now) or not valid_panel_device_cookie(
            locked, cookie_token):
        return None, False, False
    digest = hashlib.sha256(cookie_token.encode('utf-8')).hexdigest()
    if locked.device_digest:
        if hmac.compare_digest(locked.device_digest, digest):
            return cookie_token, True, False
        return None, False, False
    locked.device_digest = digest
    if locked.confirmed_at is None:
        locked.confirmed_at = now
    locked.save(update_fields=('device_digest', 'confirmed_at', 'updated_at'))
    _record_panel_audit(locked.hearing, PanelAuditEvent.Action.DEVICE_BOUND,
        'Examiner invitation bound to its first browser.', invitation=locked)
    return cookie_token, True, True


def _lock_panel_invitation(raw_token, at=None):
    """Lock hearing then invitation in one order for all examiner operations."""
    if not raw_token or len(raw_token) > 256:
        raise PermissionDenied
    target = PanelInvitation.objects.filter(
        token_digest=PanelInvitation.digest_token(raw_token)).values_list('pk', 'hearing_id').first()
    if target is None:
        raise PermissionDenied
    invitation_id, hearing_id = target
    hearing = PanelHearing.objects.select_for_update().filter(pk=hearing_id).first()
    if hearing is None:
        raise PermissionDenied
    invitation = (PanelInvitation.objects.select_for_update()
                  .filter(pk=invitation_id, hearing_id=hearing.pk).first())
    if invitation is None or not invitation.usable_at(at):
        raise PermissionDenied
    return hearing, invitation


@transaction.atomic
def declare_panel_conflict(raw_token, candidate, reason, *, at=None):
    """Record an invited examiner's reasoned recusal from a candidate."""
    hearing, invitation = _lock_panel_invitation(raw_token, at)
    if hearing.status not in (PanelHearing.Status.DRAFT, PanelHearing.Status.OPEN):
        raise ValidationError('This hearing is not accepting conflict declarations.')
    if not PanelCandidate.objects.filter(pk=candidate.pk, hearing_id=invitation.hearing_id).exists():
        raise PermissionDenied
    if (PanelScore.objects.filter(invitation=invitation, candidate=candidate).exists()
            or PanelAbstention.objects.filter(invitation=invitation, candidate=candidate).exists()
            or PanelVote.objects.filter(invitation=invitation, candidate=candidate).exists()
            or PanelWrittenReview.objects.filter(invitation=invitation, candidate=candidate).exists()):
        raise ValidationError('A conflict must be declared before scoring this candidate.')
    conflict = PanelConflict(invitation=invitation, candidate=candidate,
        organization_id=invitation.organization_id, created_by=invitation.created_by, reason=reason)
    conflict.full_clean()
    conflict.save(force_insert=True)
    record_panel_invitation_attempt(invitation, PanelInvitationAttempt.Action.CONFLICT,
        PanelInvitationAttempt.Outcome.ACCEPTED, PanelInvitationAttempt.Reason.SUBMITTED)
    _record_panel_audit(hearing, PanelAuditEvent.Action.CONFLICT_DECLARED, reason,
        invitation=invitation, candidate=candidate)
    return conflict


@transaction.atomic
def submit_panel_score(raw_token, candidate, criterion, score, comment='', *, at=None):
    """Accept one score per invited examiner/candidate/criterion, enforcing recusal."""
    hearing, invitation = _lock_panel_invitation(raw_token, at)
    if not PanelCandidate.objects.filter(pk=candidate.pk, hearing_id=invitation.hearing_id).exists():
        raise PermissionDenied
    if not PanelCriterion.objects.filter(pk=criterion.pk, hearing_id=invitation.hearing_id).exists():
        raise PermissionDenied
    if invitation.panel_id and not invitation.panel.criteria.filter(pk=criterion.pk).exists():
        raise PermissionDenied('This criterion is not assigned to the invited panel.')
    if hearing.status != PanelHearing.Status.OPEN:
        raise ValidationError('This hearing is not accepting scores.')
    if hearing.workflow not in (PanelHearing.Workflow.SCORED, PanelHearing.Workflow.HYBRID):
        raise ValidationError('This workflow does not collect numeric scores.')
    if hearing.active_candidate_id != candidate.pk:
        raise ValidationError('The chair has not activated this candidate for scoring.')
    if PanelConflict.objects.filter(invitation=invitation, candidate=candidate).exists():
        raise PermissionDenied('This examiner is recused from the candidate.')
    if PanelAbstention.objects.filter(invitation=invitation, candidate=candidate,
                                      criterion=criterion).exists():
        raise ValidationError('This examiner has already abstained from this criterion.')
    panel_score = PanelScore(invitation=invitation, candidate=candidate, criterion=criterion,
        organization_id=invitation.organization_id, created_by=invitation.created_by,
        score=score, comment=comment)
    panel_score.full_clean()
    panel_score.save(force_insert=True)
    record_panel_invitation_attempt(invitation, PanelInvitationAttempt.Action.SCORE,
        PanelInvitationAttempt.Outcome.ACCEPTED, PanelInvitationAttempt.Reason.SUBMITTED)
    _record_panel_audit(hearing, PanelAuditEvent.Action.SCORE_SUBMITTED,
        'Criterion score submitted.', invitation=invitation, candidate=candidate)
    return panel_score


@transaction.atomic
def abstain_panel_score(raw_token, candidate, criterion, *, reason='', at=None):
    """Record one criterion abstention for an invited examiner, without a score."""
    hearing, invitation = _lock_panel_invitation(raw_token, at)
    if not PanelCandidate.objects.filter(pk=candidate.pk, hearing_id=invitation.hearing_id).exists():
        raise PermissionDenied
    if not PanelCriterion.objects.filter(pk=criterion.pk, hearing_id=invitation.hearing_id).exists():
        raise PermissionDenied
    if hearing.status != PanelHearing.Status.OPEN:
        raise ValidationError('This hearing is not accepting abstentions.')
    if hearing.workflow not in (PanelHearing.Workflow.SCORED, PanelHearing.Workflow.HYBRID):
        raise ValidationError('This workflow does not collect numeric scores.')
    if hearing.active_candidate_id != candidate.pk:
        raise ValidationError('The chair has not activated this candidate for scoring.')
    if PanelConflict.objects.filter(invitation=invitation, candidate=candidate).exists():
        raise PermissionDenied('This examiner is recused from the candidate.')
    if PanelScore.objects.filter(invitation=invitation, candidate=candidate, criterion=criterion).exists():
        raise ValidationError('A submitted score cannot be changed to an abstention.')
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
        raise ValidationError('Give a reason for skipping this criterion (up to 1000 characters).')
    abstention = PanelAbstention(invitation=invitation, candidate=candidate, criterion=criterion,
        reason=reason.strip(),
        organization_id=invitation.organization_id, created_by=invitation.created_by)
    abstention.full_clean()
    abstention.save(force_insert=True)
    record_panel_invitation_attempt(invitation, PanelInvitationAttempt.Action.ABSTAIN,
        PanelInvitationAttempt.Outcome.ACCEPTED, PanelInvitationAttempt.Reason.SUBMITTED)
    _record_panel_audit(hearing, PanelAuditEvent.Action.SCORE_ABSTAINED,
        'Examiner abstained from scoring this criterion.',
        invitation=invitation, candidate=candidate)
    return abstention


@transaction.atomic
def submit_panel_written_review(raw_token, candidate, report, revision_reason='', *, at=None):
    """Save one immutable independent report without exposing it to peer reviewers."""
    hearing, invitation = _lock_panel_invitation(raw_token, at)
    if hearing.workflow not in (PanelHearing.Workflow.REVIEW, PanelHearing.Workflow.REVIEW_VOTE):
        raise ValidationError('This workflow does not collect written reviewer reports.')
    if hearing.status != PanelHearing.Status.OPEN or hearing.active_candidate_id != candidate.pk:
        raise ValidationError('The chair has not activated this candidate for review.')
    if not PanelCandidate.objects.filter(pk=candidate.pk, hearing=hearing).exists():
        raise PermissionDenied
    if PanelConflict.objects.filter(invitation=invitation, candidate=candidate).exists():
        raise PermissionDenied('This examiner is recused from the candidate.')
    previous = PanelWrittenReview.objects.filter(invitation=invitation,
        candidate=candidate).order_by('-revision').first()
    revision = previous.revision + 1 if previous else 1
    review = PanelWrittenReview(hearing=hearing, invitation=invitation, candidate=candidate,
        report=report.strip(), revision=revision, revision_reason=revision_reason.strip(),
        organization_id=hearing.organization_id,
        created_by=invitation.created_by)
    review.full_clean()
    review.save(force_insert=True)
    _record_panel_audit(hearing, PanelAuditEvent.Action.REVIEW_SUBMITTED,
        'Independent written reviewer report submitted.', invitation=invitation, candidate=candidate)
    return review


@transaction.atomic
def submit_panel_vote(raw_token, candidate, choice, *, at=None):
    """Save one private ballot; the organizer sees the aggregate after quorum."""
    hearing, invitation = _lock_panel_invitation(raw_token, at)
    if hearing.workflow not in (PanelHearing.Workflow.VOTE, PanelHearing.Workflow.HYBRID,
                                PanelHearing.Workflow.REVIEW_VOTE):
        raise ValidationError('This workflow does not collect panel votes.')
    if hearing.status != PanelHearing.Status.OPEN or hearing.active_candidate_id != candidate.pk:
        raise ValidationError('The chair has not activated this candidate for voting.')
    if not PanelCandidate.objects.filter(pk=candidate.pk, hearing=hearing).exists():
        raise PermissionDenied
    if PanelConflict.objects.filter(invitation=invitation, candidate=candidate).exists():
        raise PermissionDenied('This examiner is recused from the candidate.')
    vote = PanelVote(hearing=hearing, invitation=invitation, candidate=candidate,
        choice=choice, organization_id=hearing.organization_id,
        created_by=invitation.created_by)
    vote.full_clean()
    vote.save(force_insert=True)
    _record_panel_audit(hearing, PanelAuditEvent.Action.VOTE_SUBMITTED,
        'Private examiner ballot submitted.', invitation=invitation, candidate=candidate)
    return vote


@transaction.atomic
def record_panel_decision(hearing, candidate, outcome, rationale, conditions='', dissent='',
                          *, organization, user):
    """Append a chair's reasoned decision after the panel's discussion and review."""
    locked = PanelHearing.objects.for_organization(organization).owned_by(user).select_for_update().filter(
        pk=hearing.pk, status=PanelHearing.Status.REVIEW).first()
    if locked is None:
        raise PermissionDenied
    candidate = PanelCandidate.objects.filter(pk=candidate.pk, hearing=locked).first()
    if candidate is None:
        raise PermissionDenied
    previous = PanelDecision.objects.filter(hearing=locked, candidate=candidate).order_by('-revision').first()
    decision = PanelDecision(hearing=locked, candidate=candidate, outcome=outcome,
        rationale=rationale.strip(), conditions=conditions.strip(), dissent=dissent.strip(),
        revision=(previous.revision + 1 if previous else 1), decided_by=user,
        organization_id=locked.organization_id, created_by=user)
    decision.full_clean()
    decision.save(force_insert=True)
    _record_panel_audit(locked, PanelAuditEvent.Action.DECISION_RECORDED,
        f'Chair recorded candidate outcome: {decision.get_outcome_display()}.',
        candidate=candidate, actor_user=user)
    return decision


@transaction.atomic
def revoke_panel_invitation(hearing, invitation, reason, *, organization, user, at=None):
    """Revoke a bearer invitation and record the owner action exactly once."""
    locked_hearing = (PanelHearing.objects.for_organization(organization).owned_by(user)
                      .select_for_update().filter(pk=hearing.pk).first())
    if locked_hearing is None:
        raise PermissionDenied
    locked_invitation = (PanelInvitation.objects.select_for_update()
                         .filter(pk=invitation.pk, hearing=locked_hearing).first())
    if locked_invitation is None:
        raise PermissionDenied
    if locked_invitation.revoked_at is not None:
        raise ValidationError('This invitation has already been revoked.')
    if not reason or not reason.strip():
        raise ValidationError('A revocation reason is required.')
    locked_invitation.revoked_at = at or timezone.now()
    locked_invitation.save(update_fields=('revoked_at', 'updated_at'))
    _record_panel_audit(locked_hearing, PanelAuditEvent.Action.INVITATION_REVOKED,
        reason, invitation=locked_invitation, actor_user=user)
    return locked_invitation


def delete_panel_hearings(hearings, using='default'):
    """Atomically delete panel hearings and cascade all dependent records."""
    if not hearings:
        return 0
    hearing_ids = [h.pk if hasattr(h, 'pk') else int(h) for h in hearings]
    if (CombinedPresentationAssessment.objects.filter(panel_hearing_id__in=hearing_ids).exists()
            or CombinedPresentationMapping.objects.filter(candidate__hearing_id__in=hearing_ids).exists()):
        raise ValidationError('One or more selected assessments are linked to a combined assessment and cannot be deleted.')

    with transaction.atomic(using=using):
        PanelHearing.objects.filter(pk__in=hearing_ids).update(active_candidate=None)

        PanelCommentRedaction.objects.filter(hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelResultAdjustment.objects.filter(snapshot__hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelResultSnapshot.objects.filter(hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelEntrantResultLink.objects.filter(hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelLiveDisplayLink.objects.filter(hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelAuditEvent.objects.filter(hearing_id__in=hearing_ids)._raw_delete(using=using)

        PanelScore.objects.filter(candidate__hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelConflict.objects.filter(invitation__hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelAbstention.objects.filter(invitation__hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelWrittenReview.objects.filter(hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelDecision.objects.filter(hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelVote.objects.filter(hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelInvitationAttempt.objects.filter(invitation__hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelInvitation.objects.filter(hearing_id__in=hearing_ids)._raw_delete(using=using)

        PanelCriterionLevel.objects.filter(criterion__hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelScoringPanel.criteria.through.objects.filter(panelscoringpanel__hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelScoringPanel.objects.filter(hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelCriterion.objects.filter(hearing_id__in=hearing_ids)._raw_delete(using=using)
        PanelCandidate.objects.filter(hearing_id__in=hearing_ids)._raw_delete(using=using)

        count = PanelHearing.objects.filter(pk__in=hearing_ids)._raw_delete(using=using)
        return count


def delete_showcase_events(events, using='default'):
    """Atomically delete showcase events and cascade all dependent records."""
    if not events:
        return 0
    event_ids = [e.pk if hasattr(e, 'pk') else int(e) for e in events]
    with transaction.atomic(using=using):
        ShowcaseJudgeEvaluation.objects.filter(assignment__event_id__in=event_ids)._raw_delete(using=using)
        ShowcaseJudgeConflict.objects.filter(assignment__event_id__in=event_ids)._raw_delete(using=using)
        ShowcaseCertificate.objects.filter(event_id__in=event_ids)._raw_delete(using=using)
        ShowcaseConsentAudit.objects.filter(event_id__in=event_ids)._raw_delete(using=using)
        ShowcaseEventAudit.objects.filter(event_id__in=event_ids)._raw_delete(using=using)
        count, _ = ShowcaseEvent.objects.filter(pk__in=event_ids).delete()
        return len(event_ids)


def delete_competitions(competitions, using='default'):
    """Atomically delete competitions and cascade all dependent records."""
    if not competitions:
        return 0
    competition_ids = [c.pk if hasattr(c, 'pk') else int(c) for c in competitions]
    with transaction.atomic(using=using):
        CompetitionLeaderboardAudit.objects.filter(competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionTransitionAudit.objects.filter(competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionAudienceBallot.objects.filter(competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionLeaderboardTieDecision.objects.filter(round__competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionAdvancementDecision.objects.filter(source_round__competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionJudgeAudit.objects.filter(invitation__competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionJudgeScore.objects.filter(invitation__competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionJudgeAbstention.objects.filter(invitation__competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionJudgeInvitation.objects.filter(competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionWeightedResultAudit.objects.filter(round__competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionAudienceJoinCode.objects.filter(round__competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionRoundEntry.objects.filter(round__competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionRound.objects.filter(competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionEntry.objects.filter(track__competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionCriterion.objects.filter(track__competition_id__in=competition_ids)._raw_delete(using=using)
        CompetitionTrack.objects.filter(competition_id__in=competition_ids)._raw_delete(using=using)
        count = Competition.objects.filter(pk__in=competition_ids)._raw_delete(using=using)
        return count


SHOWCASE_RATING_NOT_ON_STAGE = 'Ratings are open for an exhibit only while it is on stage.'


@transaction.atomic
def submit_showcase_rating(event, exhibit, device_key, stars):
    """Store one privacy-preserving visitor rating per event/exhibit/device.

    Public submissions do not lock the shared event row: the database's unique
    event/exhibit/device constraint is the authority for duplicate races. An
    in-flight request that passes the open-window check before the organizer
    closes the event may finish writing, like any request already accepted by
    the event when it was open.
    """
    current_event = ShowcaseEvent.objects.filter(pk=event.pk).first()
    if current_event is None:
        raise ValidationError('This showcase is not currently accepting ratings.')
    current_exhibit = ShowcaseExhibit.objects.filter(
        pk=exhibit.pk, event_id=current_event.pk).first()
    if current_exhibit is None:
        raise ValidationError('The exhibit is not part of this showcase.')
    if current_event.status != ShowcaseEvent.Status.OPEN:
        raise ValidationError('This showcase is not currently accepting ratings.')
    if current_event.includes_minors and not current_event.consent_confirmed_at:
        raise ValidationError('Required minor-participant consent has not been confirmed.')
    if not showcase_exhibit_on_stage(current_event, current_exhibit.pk):
        raise ValidationError(SHOWCASE_RATING_NOT_ON_STAGE)
    digest = ShowcaseRating.digest_device_key(device_key, current_event)
    rating = ShowcaseRating(event=current_event, exhibit=current_exhibit, device_digest=digest,
                            organization_id=current_event.organization_id, created_by=current_event.created_by,
                            stars=stars)
    # The database constraint is the concurrency-safe duplicate check. Avoid
    # Django's separate uniqueness query on this high-volume public write path.
    rating.full_clean(validate_unique=False, validate_constraints=False)
    try:
        rating.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError('This device has already rated this exhibit.') from exc
    return rating


@transaction.atomic
def replace_showcase_judge_assignments(event, assignments, *, organization, user,
                                      minimum_judges=None):
    """Replace the event's assignment plan atomically, retaining old rows inactive."""
    locked_event = (ShowcaseEvent.objects.for_organization(organization).owned_by(user)
                    .select_for_update().filter(pk=event.pk).first())
    if locked_event is None:
        raise PermissionDenied
    if locked_event.status not in (ShowcaseEvent.Status.DRAFT, ShowcaseEvent.Status.OPEN):
        raise ValidationError('Judge assignments are locked after the event closes.')
    if minimum_judges is not None and (
            isinstance(minimum_judges, bool) or not isinstance(minimum_judges, int)
            or not 1 <= minimum_judges <= 50):
        raise ValidationError('Minimum judge coverage must be a whole number from 1 to 50.')
    if not isinstance(assignments, dict):
        raise ValidationError('Judge assignments must be grouped by exhibit.')

    exhibits = {item.pk: item for item in ShowcaseExhibit.objects
                .filter(event=locked_event).order_by('pk')}
    normalized = {}
    assignment_count = 0
    for exhibit_id, labels in assignments.items():
        if isinstance(exhibit_id, bool):
            raise ValidationError('An assignment references an unknown exhibit.')
        try:
            exhibit_id = int(exhibit_id)
        except (TypeError, ValueError) as exc:
            raise ValidationError('An assignment references an unknown exhibit.') from exc
        if exhibit_id not in exhibits:
            raise PermissionDenied
        if isinstance(labels, (str, bytes)) or not isinstance(labels, (list, tuple, set)):
            raise ValidationError('Each exhibit assignment must be a list of judge labels.')
        unique_labels = {}
        for label in labels:
            if not isinstance(label, str):
                raise ValidationError('Judge labels must be text.')
            label = ' '.join(label.split())
            if not label or len(label) > 160:
                raise ValidationError('Judge labels must contain 1 to 160 characters.')
            unique_labels.setdefault(label.casefold(), label)
        normalized[exhibit_id] = list(unique_labels.values())
        assignment_count += len(unique_labels)
    if assignment_count > MAX_SHOWCASE_JUDGE_ASSIGNMENTS:
        raise ValidationError(f'An event cannot have more than {MAX_SHOWCASE_JUDGE_ASSIGNMENTS} active judge assignments.')

    if minimum_judges is not None:
        locked_event.minimum_judges_per_exhibit = minimum_judges
        locked_event.save(update_fields=('minimum_judges_per_exhibit', 'updated_at'))

    ShowcaseJudgeAssignment.objects.filter(event=locked_event, active=True).update(active=False)
    created = []
    for exhibit_id, labels in normalized.items():
        exhibit = exhibits[exhibit_id]
        for label in labels:
            assignment = ShowcaseJudgeAssignment(
                event=locked_event, exhibit=exhibit, judge_label=label,
                organization_id=locked_event.organization_id, created_by=locked_event.created_by)
            assignment.full_clean()
            assignment.save(force_insert=True)
            created.append(assignment)
    return created


@transaction.atomic
def issue_showcase_judge_invitation(event, email, expires_at, *, organization, user):
    """Issue a per-event judge link; keep only a digest of its bearer token."""
    locked_event = (ShowcaseEvent.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=event.pk,
            status__in=(ShowcaseEvent.Status.DRAFT, ShowcaseEvent.Status.OPEN,
                ShowcaseEvent.Status.CLOSED)).first())
    if locked_event is None:
        raise PermissionDenied
    if not isinstance(email, str) or not email.strip() or len(email.strip()) > 254:
        raise ValidationError('Enter a valid judge email address.')
    if ShowcaseJudgeInvitation.objects.filter(event=locked_event,
            email__iexact=email.strip(), revoked_at__isnull=True,
            expires_at__gt=timezone.now()).exists():
        raise ValidationError('An active judge invitation already exists for this email address.')
    raw_token = secrets.token_urlsafe(32)
    invitation = ShowcaseJudgeInvitation(
        event=locked_event, email=email.strip(),
        token_digest=ShowcaseJudgeInvitation.digest_token(raw_token),
        expires_at=expires_at, organization_id=locked_event.organization_id,
        created_by=user)
    invitation.full_clean()
    invitation.save(force_insert=True)
    return invitation, raw_token


def resolve_showcase_judge_invitation(raw_token, *, at=None):
    """Resolve a judge bearer link only while the invitation is usable."""
    if not raw_token or len(raw_token) > 256:
        return None
    invitation = (ShowcaseJudgeInvitation.objects.select_related('event')
        .filter(token_digest=ShowcaseJudgeInvitation.digest_token(raw_token)).first())
    return invitation if invitation and invitation.usable_at(at) else None


@transaction.atomic
def revoke_showcase_judge_invitation(event, invitation_id, *, organization, user, at=None):
    """Revoke an event-scoped judge link and deactivate its assignments."""
    locked_event = (ShowcaseEvent.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=event.pk).first())
    if locked_event is None:
        raise PermissionDenied
    invitation = (ShowcaseJudgeInvitation.objects.for_organization(organization)
        .owned_by(user).select_for_update()
        .filter(pk=invitation_id, event=locked_event).first())
    if invitation is None:
        raise PermissionDenied
    if invitation.revoked_at is not None:
        raise ValidationError('This Showcase judge invitation is already revoked.')
    invitation.revoked_at = at or timezone.now()
    invitation.save(update_fields=('revoked_at', 'updated_at'))
    ShowcaseJudgeExhibitAssignment.objects.filter(
        invitation=invitation, active=True).update(active=False)
    return invitation


@transaction.atomic
def replace_showcase_invited_judge_assignments(event, assignments, *, organization, user):
    """Replace invited-judge exhibit assignments atomically within one event."""
    locked_event = (ShowcaseEvent.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=event.pk,
            status__in=(ShowcaseEvent.Status.DRAFT, ShowcaseEvent.Status.OPEN,
                ShowcaseEvent.Status.CLOSED)).first())
    if locked_event is None:
        raise PermissionDenied
    if not isinstance(assignments, dict):
        raise ValidationError('Judge assignments must map invitation IDs to exhibit IDs.')
    invitations = {item.pk: item for item in ShowcaseJudgeInvitation.objects
        .for_organization(organization).owned_by(user)
        .filter(event=locked_event, revoked_at__isnull=True).select_for_update()}
    exhibits = {item.pk: item for item in ShowcaseExhibit.objects
        .filter(event=locked_event).order_by('pk')}
    normalized = {}
    total = 0
    for invitation_id, exhibit_ids in assignments.items():
        if isinstance(invitation_id, bool):
            raise ValidationError('An assignment references an unknown judge invitation.')
        try:
            invitation_id = int(invitation_id)
        except (TypeError, ValueError) as exc:
            raise ValidationError('An assignment references an unknown judge invitation.') from exc
        invitation = invitations.get(invitation_id)
        if invitation is None:
            raise PermissionDenied
        if not invitation.usable_at():
            raise ValidationError('An assignment references an expired or revoked judge invitation.')
        if isinstance(exhibit_ids, (str, bytes)) or not isinstance(exhibit_ids, (list, tuple, set)):
            raise ValidationError('Each judge must be assigned a list of exhibits.')
        selected = set()
        for exhibit_id in exhibit_ids:
            if isinstance(exhibit_id, bool):
                raise ValidationError('An assignment references an unknown exhibit.')
            try:
                exhibit_id = int(exhibit_id)
            except (TypeError, ValueError) as exc:
                raise ValidationError('An assignment references an unknown exhibit.') from exc
            if exhibit_id not in exhibits:
                raise PermissionDenied
            selected.add(exhibit_id)
        normalized[invitation_id] = selected
        total += len(selected)
    if total > MAX_SHOWCASE_JUDGE_ASSIGNMENTS:
        raise ValidationError(f'An event cannot have more than {MAX_SHOWCASE_JUDGE_ASSIGNMENTS} active judge assignments.')

    ShowcaseJudgeExhibitAssignment.objects.filter(event=locked_event, active=True).update(active=False)
    created = []
    for invitation_id, exhibit_ids in normalized.items():
        invitation = invitations[invitation_id]
        for exhibit_id in exhibit_ids:
            assignment = ShowcaseJudgeExhibitAssignment.objects.filter(
                invitation=invitation, exhibit=exhibits[exhibit_id]).first()
            if assignment is None:
                assignment = ShowcaseJudgeExhibitAssignment(
                    event=locked_event, invitation=invitation,
                    exhibit=exhibits[exhibit_id], active=True,
                    organization_id=locked_event.organization_id,
                    created_by=locked_event.created_by)
                assignment.full_clean()
                assignment.save(force_insert=True)
            else:
                assignment.active = True
                assignment.full_clean()
                assignment.save(update_fields=('active', 'updated_at'))
            created.append(assignment)
    return created


def showcase_rubric_total(event):
    """Sum of the event's judge criterion weights."""
    return event.judge_criteria.aggregate(total=Sum('weight'))['total'] or Decimal('0')


@transaction.atomic
def save_showcase_judge_criterion(event, data, *, organization, user, criterion_id=None):
    """Add one draft judge criterion, or update `criterion_id`.

    The rubric is built up criterion by criterion, so the running total may stay
    under 100 while editing; it can never go over, and opening requires exactly 100.
    """
    locked_event = (ShowcaseEvent.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=event.pk, status=ShowcaseEvent.Status.DRAFT).first())
    if locked_event is None:
        raise PermissionDenied
    criteria = list(locked_event.judge_criteria.all())
    current = None
    if criterion_id is not None:
        current = next((item for item in criteria if item.pk == criterion_id), None)
        if current is None:
            raise PermissionDenied
    others = [item for item in criteria if item is not current]
    if current is None and len(criteria) >= 50:
        raise ValidationError('A rubric can have at most 50 criteria.')
    name = ' '.join(str(data['name']).split())
    if name.casefold() in {item.name.casefold() for item in others}:
        raise ValidationError('Judge criterion names must be unique.')
    remaining = Decimal('100') - sum((item.weight for item in others), Decimal('0'))
    if data['weight'] > remaining:
        raise ValidationError(
            f'This weight is too high: only {remaining.normalize():f}% of the rubric is still unassigned.')
    criterion = current or ShowcaseJudgeCriterion(event=locked_event,
        organization_id=locked_event.organization_id, created_by=locked_event.created_by,
        order=max((item.order for item in criteria), default=-1) + 1)
    criterion.name = name
    criterion.description = data['description']
    criterion.weight = data['weight']
    criterion.scale = data['scale']
    criterion.max_points = data['max_points']
    criterion.checklist_items = data['checklist_items']
    criterion.full_clean()
    criterion.save()
    return criterion


@transaction.atomic
def replace_showcase_judge_criteria(event, criteria, *, organization, user):
    """Replace the draft judge rubric; all configured percentage weights total 100."""
    locked_event = (ShowcaseEvent.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=event.pk, status=ShowcaseEvent.Status.DRAFT).first())
    if locked_event is None:
        raise PermissionDenied
    if isinstance(criteria, (str, bytes)) or not isinstance(criteria, (list, tuple)):
        raise ValidationError('Judge criteria must be a list.')
    if not criteria or len(criteria) > 50:
        raise ValidationError('Configure between 1 and 50 judge criteria.')
    allowed_fields = {'name', 'description', 'weight', 'scale', 'max_points', 'checklist_items'}
    if any(not isinstance(item, dict) or set(item) - allowed_fields for item in criteria):
        raise ValidationError('Judge criterion configuration contains invalid fields.')
    names = [' '.join(str(item.get('name', '')).split()) for item in criteria]
    if any(not name or len(name) > 120 for name in names) or len({name.casefold() for name in names}) != len(names):
        raise ValidationError('Judge criterion names must be unique and contain 1 to 120 characters.')
    try:
        total_weight = sum((Decimal(str(item.get('weight'))) for item in criteria), Decimal('0'))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError('Judge criterion weights must be valid percentages.') from exc
    if not total_weight.is_finite() or total_weight != Decimal('100'):
        raise ValidationError('Judge criterion weights must total exactly 100.')

    ShowcaseJudgeCriterion.objects.filter(event=locked_event).delete()
    created = []
    for order, (item, name) in enumerate(zip(criteria, names)):
        criterion = ShowcaseJudgeCriterion(
            event=locked_event, name=name,
            description=item.get('description', ''), weight=item.get('weight'),
            scale=item.get('scale', ShowcaseJudgeCriterion.Scale.POINTS),
            max_points=item.get('max_points', Decimal('100')),
            checklist_items=item.get('checklist_items', []), order=order,
            organization_id=locked_event.organization_id, created_by=locked_event.created_by)
        criterion.full_clean()
        criterion.save(force_insert=True)
        created.append(criterion)
    return created


SHOWCASE_NOT_ON_STAGE = ('This exhibit is not on stage yet. You can submit its scorecard '
                         'once the organizer puts it on stage.')


@transaction.atomic
def submit_showcase_judge_evaluation(raw_token, exhibit_public_id, raw_scores, *, at=None):
    """Accept one immutable, weighted judge scorecard for an assigned exhibit."""
    invitation = resolve_showcase_judge_invitation(raw_token, at=at)
    if invitation is None:
        raise PermissionDenied
    event = ShowcaseEvent.objects.select_for_update().filter(pk=invitation.event_id).first()
    invitation = (ShowcaseJudgeInvitation.objects.select_for_update()
        .select_related('event').filter(pk=invitation.pk, event=event).first())
    now = at or timezone.now()
    if (event is None or invitation is None or not invitation.usable_at(now)
            or event.status not in (ShowcaseEvent.Status.OPEN, ShowcaseEvent.Status.CLOSED)):
        raise PermissionDenied
    assignment = (ShowcaseJudgeExhibitAssignment.objects.select_for_update()
        .select_related('exhibit').filter(event=event, invitation=invitation,
            exhibit__public_id=exhibit_public_id, active=True).first())
    if assignment is None:
        raise PermissionDenied
    if ShowcaseJudgeConflict.objects.filter(assignment=assignment).exists():
        raise PermissionDenied
    if ShowcaseJudgeEvaluation.objects.filter(assignment=assignment).exists():
        raise ValidationError('This exhibit scorecard was already submitted and is immutable.')
    if (event.status == ShowcaseEvent.Status.OPEN
            and not showcase_exhibit_on_stage(event, assignment.exhibit_id)):
        raise ValidationError(SHOWCASE_NOT_ON_STAGE)
    criteria = list(ShowcaseJudgeCriterion.objects.filter(event=event).order_by('order', 'pk'))
    score = showcase_judge_score(criteria, raw_scores)
    stored_scores = {key: ([str(value) if isinstance(value, Decimal) else value
        for value in values] if isinstance(values, (list, tuple)) else
        str(values) if isinstance(values, Decimal) else values)
        for key, values in raw_scores.items()}
    evaluation = ShowcaseJudgeEvaluation(assignment=assignment, raw_scores=stored_scores,
        score=score, organization_id=assignment.organization_id,
        created_by=assignment.created_by)
    evaluation.full_clean()
    try:
        evaluation.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError('This exhibit scorecard was already submitted and is immutable.') from exc
    return evaluation


@transaction.atomic
def declare_showcase_judge_conflict(raw_token, exhibit_public_id, reason, *, at=None):
    """Record an invited judge's conflict before accepting an exhibit scorecard."""
    invitation = resolve_showcase_judge_invitation(raw_token, at=at)
    if invitation is None:
        raise PermissionDenied
    event = ShowcaseEvent.objects.select_for_update().filter(pk=invitation.event_id).first()
    invitation = (ShowcaseJudgeInvitation.objects.select_for_update()
        .select_related('event').filter(pk=invitation.pk, event=event).first())
    now = at or timezone.now()
    if event is None or invitation is None or not invitation.usable_at(now):
        raise PermissionDenied
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
        raise ValidationError('Enter a conflict reason of 1 to 500 characters.')
    assignment = (ShowcaseJudgeExhibitAssignment.objects.select_for_update()
        .select_related('exhibit').filter(event=event, invitation=invitation,
            exhibit__public_id=exhibit_public_id, active=True).first())
    if assignment is None:
        raise PermissionDenied
    if ShowcaseJudgeEvaluation.objects.filter(assignment=assignment).exists():
        raise ValidationError('A conflict must be declared before submitting this exhibit scorecard.')
    if ShowcaseJudgeConflict.objects.filter(assignment=assignment).exists():
        raise ValidationError('A conflict has already been declared for this exhibit.')
    conflict = ShowcaseJudgeConflict(assignment=assignment, reason=reason.strip(),
        organization_id=assignment.organization_id, created_by=assignment.created_by)
    conflict.full_clean()
    conflict.save(force_insert=True)
    return conflict


def showcase_judge_progress(event, *, organization, user):
    """Return owner-scoped assignment completion without exposing raw criteria scores."""
    scoped_event = (ShowcaseEvent.objects.for_organization(organization).owned_by(user)
        .filter(pk=event.pk).first())
    if scoped_event is None:
        raise PermissionDenied
    assignments = (ShowcaseJudgeExhibitAssignment.objects.for_organization(organization)
        .owned_by(user).filter(event=scoped_event, active=True)
        .select_related('invitation', 'exhibit').order_by('invitation__email', 'exhibit__order', 'pk'))
    assignment_rows = list(assignments)
    submitted_assignment_ids = set(ShowcaseJudgeEvaluation.objects.filter(
        assignment_id__in=[item.pk for item in assignment_rows])
        .values_list('assignment_id', flat=True))
    conflicted_assignment_ids = set(ShowcaseJudgeConflict.objects.filter(
        assignment_id__in=[item.pk for item in assignment_rows])
        .values_list('assignment_id', flat=True))
    rows = []
    for assignment in assignment_rows:
        submitted = assignment.pk in submitted_assignment_ids
        conflict = assignment.pk in conflicted_assignment_ids
        rows.append({'judge': assignment.invitation.email, 'exhibit': assignment.exhibit.name,
            'submitted': submitted, 'conflict_declared': conflict})
    return {'assignments': rows, 'assigned_count': len(rows),
        'submitted_count': sum(row['submitted'] for row in rows),
        'pending_count': sum(not row['submitted'] and not row['conflict_declared'] for row in rows),
        'conflict_count': sum(row['conflict_declared'] for row in rows)}


@transaction.atomic
def confirm_showcase_minor_consent(event, reason, *, organization, user):
    """Record an owner attestation before a minors-involving event can open."""
    locked_event = (ShowcaseEvent.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=event.pk, status=ShowcaseEvent.Status.DRAFT).first())
    if locked_event is None:
        raise PermissionDenied
    if not locked_event.includes_minors:
        raise ValidationError('Consent confirmation applies only to events marked as involving minors.')
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
        raise ValidationError('Enter confirmation details of 1 to 500 characters.')
    if locked_event.consent_confirmed_at:
        raise ValidationError('Consent has already been confirmed for this draft event.')
    locked_event.consent_confirmed_at = timezone.now()
    locked_event.save(update_fields=('consent_confirmed_at', 'updated_at'))
    audit = ShowcaseConsentAudit(event=locked_event, actor_user=user,
        reason=reason.strip(), organization_id=locked_event.organization_id,
        created_by=locked_event.created_by)
    audit.full_clean()
    audit.save(force_insert=True)
    return audit


def showcase_judge_coverage(event, *, minimum_judges, require_submitted=False):
    """Summarize active assignments or submitted scorecards against the threshold."""
    if isinstance(minimum_judges, bool) or not isinstance(minimum_judges, int) or not 0 <= minimum_judges <= 50:
        raise ValidationError('Minimum judge coverage must be a whole number from 0 to 50.')
    if require_submitted:
        submitted_counts = {
            row['assignment__exhibit_id']: row['count']
            for row in ShowcaseJudgeEvaluation.objects.filter(
                assignment__event=event).values('assignment__exhibit_id')
                .annotate(count=Count('pk'))
        }
        assignments = {exhibit_id: ['submitted'] * count
            for exhibit_id, count in submitted_counts.items()}
    else:
        assignments = {}
        assignments_query = ShowcaseJudgeExhibitAssignment.objects.filter(
            event=event, active=True, invitation__revoked_at__isnull=True,
            invitation__expires_at__gt=timezone.now())
        assignments_query = assignments_query.exclude(conflict_declaration__isnull=False)
        for assignment in assignments_query.select_related('invitation').order_by(
                'invitation__email', 'pk'):
            assignments.setdefault(assignment.exhibit_id, []).append(assignment.invitation.email)
    exhibits = []
    for exhibit in event.exhibits.all():
        labels = assignments.get(exhibit.pk, [])
        gap = max(0, minimum_judges - len(labels))
        exhibits.append({
            'exhibit_id': exhibit.pk,
            'exhibit': exhibit.name,
            'assigned_judges': labels,
            'assigned_count': len(labels),
            'required_count': minimum_judges,
            'gap': gap,
            'covered': gap == 0,
        })
    gaps = sum(row['gap'] for row in exhibits)
    return {
        'exhibits': exhibits,
        'required_judges': minimum_judges,
        'exhibit_count': len(exhibits),
        'covered_exhibit_count': sum(row['covered'] for row in exhibits),
        'uncovered_exhibit_count': sum(not row['covered'] for row in exhibits),
        'missing_assignments': gaps,
    }


@transaction.atomic
def transition_showcase_event(event, target_status, *, organization, user, reason):
    """Perform and audit one owner-authorized Showcase lifecycle transition."""
    locked_event = (ShowcaseEvent.objects.for_organization(organization).owned_by(user)
                    .select_for_update().filter(pk=event.pk).first())
    if locked_event is None:
        raise PermissionDenied
    if not isinstance(target_status, str) or target_status not in ShowcaseEvent.Status.values:
        raise ValidationError('Choose a valid Showcase event status.')
    allowed = {
        ShowcaseEvent.Status.DRAFT: {ShowcaseEvent.Status.OPEN},
        ShowcaseEvent.Status.OPEN: {ShowcaseEvent.Status.CLOSED},
        ShowcaseEvent.Status.CLOSED: {ShowcaseEvent.Status.PUBLISHED},
        ShowcaseEvent.Status.PUBLISHED: {ShowcaseEvent.Status.CLOSED},
    }
    if target_status not in allowed.get(locked_event.status, set()):
        raise ValidationError('This Showcase event cannot make that lifecycle transition.')
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
        raise ValidationError('Provide a lifecycle reason of 1 to 500 characters.')
    if (target_status == ShowcaseEvent.Status.OPEN
            and not ShowcaseExhibit.objects.filter(event=locked_event).exists()):
        raise ValidationError('Add at least one exhibit before opening this Showcase event.')
    if (target_status == ShowcaseEvent.Status.OPEN and locked_event.includes_minors
            and locked_event.consent_confirmed_at is None):
        raise ValidationError('Confirm participant consent before opening this Showcase event.')
    if target_status == ShowcaseEvent.Status.OPEN:
        active_assignments = ShowcaseJudgeExhibitAssignment.objects.filter(
            event=locked_event, active=True, invitation__revoked_at__isnull=True,
            invitation__expires_at__gt=timezone.now()).exclude(
                conflict_declaration__isnull=False).exists()
        judge_scoring_required = (locked_event.minimum_judges_per_exhibit > 0
            or active_assignments)
        if judge_scoring_required and not ShowcaseJudgeCriterion.objects.filter(event=locked_event).exists():
            raise ValidationError('Configure judge criteria before opening this Showcase event.')
        rubric_total = showcase_rubric_total(locked_event)
        if rubric_total and rubric_total != 100:
            raise ValidationError(
                f'Judge criterion weights total {rubric_total}%; they must total exactly 100% before opening.')
        coverage = showcase_judge_coverage(locked_event,
            minimum_judges=locked_event.minimum_judges_per_exhibit)
        if coverage['uncovered_exhibit_count']:
            raise ValidationError('Assign enough active invited judges to every exhibit before opening this Showcase event.')
    if (target_status == ShowcaseEvent.Status.PUBLISHED
            and locked_event.minimum_judges_per_exhibit > 0):
        submitted_coverage = showcase_judge_coverage(locked_event,
            minimum_judges=locked_event.minimum_judges_per_exhibit,
            require_submitted=True)
        if submitted_coverage['uncovered_exhibit_count']:
            raise ValidationError('Each exhibit needs the required number of submitted judge scorecards before results can be published.')

    previous_status = locked_event.status
    locked_event.status = target_status
    locked_event.public_results = target_status == ShowcaseEvent.Status.PUBLISHED
    locked_event.full_clean()
    locked_event.save(update_fields=('status', 'public_results', 'updated_at'))

    if target_status == ShowcaseEvent.Status.PUBLISHED:
        issue_showcase_certificates(locked_event)

    audit = ShowcaseEventAudit(event=locked_event, actor_user=user,
        from_status=previous_status, to_status=target_status, reason=reason.strip(),
        organization_id=locked_event.organization_id, created_by=locked_event.created_by)
    audit.full_clean()
    audit.save(force_insert=True)
    return locked_event


def showcase_awards(event, *, allow_preview=False):
    """Calculate judge-based category awards and the separate audience favorite."""
    if not allow_preview and (event.status != ShowcaseEvent.Status.PUBLISHED or not event.public_results):
        raise PermissionDenied
    judge_scores = defaultdict(list)
    for exhibit_id, score in ShowcaseJudgeEvaluation.objects.filter(
            assignment__event=event).values_list('assignment__exhibit_id', 'score'):
        judge_scores[exhibit_id].append(score)
    visitor_ratings = defaultdict(list)
    for exhibit_id, stars in ShowcaseRating.objects.filter(event=event).values_list(
            'exhibit_id', 'stars'):
        visitor_ratings[exhibit_id].append(stars)
    awards = []
    for category in event.categories.all():
        candidates = []
        for exhibit in category.exhibits.all():
            values = judge_scores.get(exhibit.pk, [])
            if values:
                candidates.append((exhibit, aggregate_scores(values, 'mean'), len(values)))
        candidates.sort(key=lambda row: -row[1])
        leaders = [row for row in candidates if row[1] == candidates[0][1]] if candidates else []
        awards.append({'category': category.name,
                       'winner': leaders[0][0] if len(leaders) == 1 else None,
                       'tied': [row[0] for row in leaders] if len(leaders) > 1 else [],
                       'tied_details': [{'exhibit': row[0], 'mean': row[1], 'judge_count': row[2]}
                                        for row in leaders] if len(leaders) > 1 else [],
                       'mean': candidates[0][1] if candidates else None,
                       'judge_count': candidates[0][2] if candidates else 0})
    candidates = []
    for exhibit in event.exhibits.all():
        values = visitor_ratings.get(exhibit.pk, [])
        if values:
            candidates.append((exhibit, aggregate_scores(values, 'mean'), len(values)))
    candidates.sort(key=lambda row: -row[1])
    leaders = [row[0] for row in candidates if row[1] == candidates[0][1]] if candidates else []
    return {'categories': awards,
            'audience_favorite': leaders[0] if len(leaders) == 1 else None,
            'audience_favorite_tied': leaders if len(leaders) > 1 else [],
            'audience_favorite_mean': candidates[0][1] if candidates else None,
            'audience_favorite_rating_count': candidates[0][2] if candidates else 0,
            'audience_favorite_tied_details': [
                {'exhibit': row[0], 'mean': row[1], 'rating_count': row[2]}
                for row in candidates if row[1] == candidates[0][1]
            ] if candidates else []}


def issue_showcase_certificates(event):
    """Issue immutable certificates only for unique winners of published awards."""
    awards = showcase_awards(event)
    issued = []
    for award in awards['categories']:
        winner = award['winner']
        if winner is None:
            continue
        category = event.categories.get(name=award['category'])
        certificate = ShowcaseCertificate.objects.filter(
            event=event, exhibit=winner, award_type=ShowcaseCertificate.AwardType.CATEGORY).first()
        if certificate is None:
            certificate = ShowcaseCertificate(
                event=event, exhibit=winner, category=category,
                award_type=ShowcaseCertificate.AwardType.CATEGORY,
                award_title=category.award_title.strip() or f'{category.name} Award',
                organization_id=event.organization_id, created_by=event.created_by)
            certificate.full_clean()
            certificate.save(force_insert=True)
        issued.append(certificate)

    favorite = awards['audience_favorite']
    if favorite is not None:
        certificate = ShowcaseCertificate.objects.filter(
            event=event, exhibit=favorite, award_type=ShowcaseCertificate.AwardType.AUDIENCE).first()
        if certificate is None:
            certificate = ShowcaseCertificate(
                event=event, exhibit=favorite,
                award_type=ShowcaseCertificate.AwardType.AUDIENCE,
                award_title='Audience Favorite', organization_id=event.organization_id,
                created_by=event.created_by)
            certificate.full_clean()
            certificate.save(force_insert=True)
        issued.append(certificate)
    return issued


def competition_advancement(scores, rule, value, *, tie_break_order=(), head_judge_decision=None):
    """Public adapter for the exact configured advancement calculation."""
    return advance_entries(scores, rule, value, tie_break_order=tie_break_order,
                           head_judge_decision=head_judge_decision)


@transaction.atomic
def create_initial_competition_round(competition, name, *, organization, user):
    """Create round one from every configured entry without opening voting."""
    competition = (Competition.objects.for_organization(organization).owned_by(user)
                   .select_for_update().filter(pk=competition.pk).first())
    if competition is None:
        raise PermissionDenied
    if competition.status != Competition.Status.DRAFT:
        raise ValidationError('The first round can be created only for a draft competition.')
    if CompetitionRound.objects.filter(competition=competition).exists():
        raise ValidationError('The initial round has already been created.')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise ValidationError('Enter a first-round name of 1 to 100 characters.')

    tracks = list(CompetitionTrack.objects.for_organization(organization).owned_by(user)
                  .filter(competition=competition).order_by('pk'))
    if not tracks:
        raise ValidationError('Add at least one track before creating the first round.')
    entries = list(CompetitionEntry.objects.for_organization(organization).owned_by(user)
                   .filter(track__competition=competition).select_related('track')
                   .order_by('track_id', 'order', 'pk'))
    by_track = {track.pk: [] for track in tracks}
    for entry in entries:
        by_track[entry.track_id].append(entry)
    if any(not by_track[track.pk] for track in tracks):
        raise ValidationError('Every competition track needs at least one entry.')

    round_obj = CompetitionRound(competition=competition, name=name.strip(), order=1,
        organization_id=competition.organization_id, created_by=user)
    round_obj.full_clean()
    round_obj.save(force_insert=True)
    for track in tracks:
        for entry in by_track[track.pk]:
            row = CompetitionRoundEntry(round=round_obj, entry=entry, track=track,
                organization_id=competition.organization_id, created_by=user)
            row.full_clean()
            row.save(force_insert=True)
    return round_obj


@transaction.atomic
def start_competition(competition, *, organization, user):
    """Open an owner-scoped draft after its complete initial round is prepared."""
    competition = (Competition.objects.for_organization(organization).owned_by(user)
                   .select_for_update().filter(pk=competition.pk).first())
    if competition is None:
        raise PermissionDenied
    if competition.status != Competition.Status.DRAFT:
        raise ValidationError('Only a draft competition can be started.')
    competition.full_clean()
    initial = (CompetitionRound.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(competition=competition, order=1).first())
    if initial is None:
        raise ValidationError('Create the initial round before starting the competition.')
    tracks = list(CompetitionTrack.objects.for_organization(organization).owned_by(user)
        .filter(competition=competition).order_by('pk'))
    rows = list(CompetitionRoundEntry.objects.for_organization(organization).owned_by(user)
        .filter(round=initial).select_related('track').order_by('track_id', 'pk'))
    present_tracks = {row.track_id for row in rows}
    if not tracks or any(track.pk not in present_tracks for track in tracks):
        raise ValidationError('Every track needs at least one entry in the initial round.')
    criterion_snapshot = {}
    for track in tracks:
        criteria = list(CompetitionCriterion.objects.for_organization(organization)
            .owned_by(user).filter(track=track).order_by('order', 'pk'))
        if not criteria:
            criterion = CompetitionCriterion(track=track, name='Overall score',
                description='Overall score from 0 to 100.', weight=Decimal('100'), order=0,
                organization_id=competition.organization_id, created_by=user)
            criterion.full_clean()
            criterion.save(force_insert=True)
            criteria = [criterion]
        if sum((criterion.weight for criterion in criteria), Decimal('0')) != Decimal('100'):
            raise ValidationError(f'Judging criteria for {track.name} must total exactly 100%.')
        criterion_snapshot[str(track.pk)] = [
            {'id': criterion.pk, 'name': criterion.name, 'weight': str(criterion.weight),
             'description': criterion.description} for criterion in criteria]
    previous = competition.status
    competition.workflow_snapshot = {
        'version': 1,
        'audience_weight': str(competition.audience_weight),
        'judge_weight': str(Decimal('100') - competition.audience_weight),
        'audience_method': ('complete_ranking_borda_0_100_v1'
            if competition.audience_weight > 0 else 'separate_ranked_choice_award'),
        'judge_method': 'arithmetic_mean_0_100_v1',
        'minimum_judges_per_entry': competition.minimum_judges_per_entry,
        'minimum_audience_ballots_per_track': competition.minimum_audience_ballots_per_track,
        'criteria': criterion_snapshot,
        'tie_policy': 'reasoned_head_judge_order_v1',
    }
    competition.status = Competition.Status.ACTIVE
    competition.save(update_fields=('status', 'workflow_snapshot', 'updated_at'))
    event = CompetitionTransitionAudit(competition=competition, actor_user=user,
        from_status=previous, to_status=competition.status,
        reason='Owner started the competition after preparing the initial round.',
        organization_id=competition.organization_id, created_by=user)
    event.full_clean()
    event.save(force_insert=True)
    return competition


def _transition_competition(competition, *, organization, user, expected_status,
                            target_status, reason, at=None):
    competition = (Competition.objects.for_organization(organization).owned_by(user)
                   .select_for_update().filter(pk=competition.pk).first())
    if competition is None:
        raise PermissionDenied
    if competition.status != expected_status:
        raise ValidationError('The competition is not in the required state for this action.')
    latest = (CompetitionRound.objects.for_organization(organization).owned_by(user)
        .filter(competition=competition).order_by('-order').first())
    if latest is None:
        raise ValidationError('Create and open a competition round before finalizing results.')
    rows = list(CompetitionRoundEntry.objects.for_organization(organization).owned_by(user)
        .filter(round=latest).select_related('track', 'entry').order_by('entry_id'))
    if not rows or any(row.score is None for row in rows):
        raise ValidationError('Finalize judge scores for every entry in the latest round first.')
    if competition.effective_audience_weight > 0 and latest.audience_locked_at is None:
        raise ValidationError('Finalize the weighted audience results before completing the competition.')
    if CompetitionJudgeInvitation.objects.filter(round=latest, submitted_at__isnull=True,
            revoked_at__isnull=True, expires_at__gt=at or timezone.now()).exists():
        raise ValidationError('Resolve every active judge invitation before finalizing results.')
    if target_status == Competition.Status.REVEALED:
        ties = _competition_final_score_ties(rows)
        decisions = set(CompetitionLeaderboardTieDecision.objects.filter(round=latest)
            .values_list('track_id', 'tied_score'))
        if any((tie['track_id'], tie['score']) not in decisions for tie in ties):
            raise ValidationError('Record a reasoned head-judge order for every final-score tie before reveal.')
    previous = competition.status
    competition.status = target_status
    if target_status == Competition.Status.REVEALED:
        competition.revealed_at = at or timezone.now()
        competition.public_leaderboard_enabled = True
        competition.save(update_fields=(
            'status', 'revealed_at', 'public_leaderboard_enabled', 'updated_at'))
    else:
        competition.save(update_fields=('status', 'updated_at'))
    event = CompetitionTransitionAudit(competition=competition, actor_user=user,
        from_status=previous, to_status=target_status, reason=reason,
        organization_id=competition.organization_id, created_by=user)
    event.full_clean()
    event.save(force_insert=True)
    return competition


@transaction.atomic
def complete_competition(competition, *, organization, user, at=None):
    """Close scoring after every latest-round judge score is finalized."""
    return _transition_competition(competition, organization=organization, user=user,
        expected_status=Competition.Status.ACTIVE,
        target_status=Competition.Status.COMPLETE,
        reason='Owner completed the competition after finalizing the latest round.', at=at)


def _competition_final_score_ties(rows):
    grouped = {}
    for row in rows:
        # Callers must first enforce the all-final-scores-present invariant.
        grouped.setdefault((row.track_id, row.score), []).append(row)
    ties = []
    for (track_id, score), entries in grouped.items():
        if len(entries) > 1:
            ties.append({'track_id': track_id, 'score': score,
                'track': entries[0].track,
                'entries': sorted(entries, key=lambda row: (row.entry.order, row.entry_id))})
    return sorted(ties, key=lambda tie: (tie['track'].name.casefold(), -tie['score']))


def competition_final_tie_requirements(competition, *, organization, user):
    """Return unresolved final score ties for the correctly scoped owner."""
    scoped = (Competition.objects.for_organization(organization).owned_by(user)
        .filter(pk=competition.pk, status=Competition.Status.COMPLETE).first())
    if scoped is None:
        raise PermissionDenied
    latest = (CompetitionRound.objects.for_organization(organization).owned_by(user)
        .filter(competition=scoped).select_related('competition').order_by('-order').first())
    if latest is None:
        return []
    rows = list(CompetitionRoundEntry.objects.for_organization(organization).owned_by(user)
        .filter(round=latest).select_related('track', 'entry').order_by('track_id', 'entry_id'))
    resolved = set(CompetitionLeaderboardTieDecision.objects.for_organization(organization)
        .owned_by(user).filter(round=latest).values_list('track_id', 'tied_score'))
    return [tie for tie in _competition_final_score_ties(rows)
        if (tie['track_id'], tie['score']) not in resolved]


@transaction.atomic
def resolve_competition_final_ties(competition, decisions, *, organization, user):
    """Record the head judge's ordered result and reason for each final tie."""
    scoped = (Competition.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=competition.pk, status=Competition.Status.COMPLETE).first())
    if scoped is None:
        raise PermissionDenied
    latest = (CompetitionRound.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(competition=scoped).order_by('-order').first())
    if latest is None:
        raise ValidationError('A completed final round is required before resolving ties.')
    rows = list(CompetitionRoundEntry.objects.for_organization(organization).owned_by(user)
        .filter(round=latest).select_related('track', 'entry').select_for_update()
        .order_by('track_id', 'entry_id'))
    if not rows or any(row.score is None for row in rows):
        raise ValidationError('Finalize every entry score before resolving leaderboard ties.')
    ties = _competition_final_score_ties(rows)
    existing = set(CompetitionLeaderboardTieDecision.objects.for_organization(organization)
        .owned_by(user).filter(round=latest).values_list('track_id', 'tied_score'))
    pending = {(tie['track_id'], tie['score']): tie for tie in ties
        if (tie['track_id'], tie['score']) not in existing}
    if not pending:
        raise ValidationError('There are no unresolved final-score ties.')
    if not isinstance(decisions, list) or len(decisions) != len(pending):
        raise ValidationError('Submit one head-judge decision for every unresolved final-score tie.')
    normalized = {}
    for item in decisions:
        if not isinstance(item, dict):
            raise ValidationError('Each tie decision must include a track, score, order, and reason.')
        track_id = item.get('track_id')
        if isinstance(track_id, bool):
            raise ValidationError('A tie decision references an unknown track.')
        try:
            track_id = int(track_id)
            score = Decimal(item.get('score'))
        except (TypeError, ValueError, ArithmeticError):
            raise ValidationError('A tie decision contains an invalid track or score.')
        if not score.is_finite():
            raise ValidationError('A tie decision score must be finite.')
        key = (track_id, score)
        if key not in pending or key in normalized:
            raise ValidationError('A tie decision does not match a unique unresolved final-score tie.')
        entry_ids = item.get('ordered_entry_ids')
        reason = item.get('reason')
        if (not isinstance(entry_ids, list) or any(type(entry_id) is not int for entry_id in entry_ids)
                or not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500):
            raise ValidationError('Provide a unique entry order and a reason of 1 to 500 characters.')
        expected_ids = {row.entry_id for row in pending[key]['entries']}
        if len(entry_ids) != len(expected_ids) or set(entry_ids) != expected_ids:
            raise ValidationError('The head-judge order must include every tied entry exactly once.')
        normalized[key] = (entry_ids, reason.strip())
    recorded = []
    for key, tie in pending.items():
        entry_ids, reason = normalized[key]
        record = CompetitionLeaderboardTieDecision(round=latest, track=tie['track'],
            tied_score=tie['score'], ordered_entry_ids=entry_ids, reason=reason,
            decided_by=user, organization_id=scoped.organization_id, created_by=user)
        record.full_clean()
        record.save(force_insert=True)
        recorded.append(record)
    return recorded


@transaction.atomic
def reveal_competition_leaderboard(competition, *, organization, user, at=None):
    """Publish the frozen, owner-authorized judge-score leaderboard."""
    competition = (Competition.objects.for_organization(organization).owned_by(user)
                   .select_for_update().filter(pk=competition.pk).first())
    if competition is None:
        raise PermissionDenied
    competition = _transition_competition(competition, organization=organization, user=user,
        expected_status=Competition.Status.COMPLETE,
        target_status=Competition.Status.REVEALED,
        reason='Owner revealed the finalized competition leaderboard.', at=at)
    schedule_competition_leaderboard_update(competition.public_id, True)
    return competition


@transaction.atomic
def set_competition_public_leaderboard(competition, enabled, *, organization, user):
    """Enable or hide an already-revealed public leaderboard, with an audit row."""
    if not isinstance(enabled, bool):
        raise ValidationError('Choose whether the public leaderboard is enabled.')
    competition = (Competition.objects.for_organization(organization).owned_by(user)
                   .select_for_update().filter(pk=competition.pk).first())
    if competition is None:
        raise PermissionDenied
    if competition.status != Competition.Status.REVEALED:
        raise ValidationError('Public display controls are available only after results are revealed.')
    if competition.public_leaderboard_enabled == enabled:
        return competition
    competition.public_leaderboard_enabled = enabled
    competition.save(update_fields=('public_leaderboard_enabled', 'updated_at'))
    event = CompetitionLeaderboardAudit(
        competition=competition, actor_user=user, enabled=enabled,
        reason=('Owner enabled the revealed public leaderboard.' if enabled
                else 'Owner paused the revealed public leaderboard.'),
        organization_id=competition.organization_id, created_by=user)
    event.full_clean()
    event.save(force_insert=True)
    schedule_competition_leaderboard_update(competition.public_id, enabled)
    return competition


def _record_competition_judge_audit(invitation, action, actor_user):
    event = CompetitionJudgeAudit(invitation=invitation, actor_user=actor_user,
        action=action, organization_id=invitation.organization_id,
        created_by=invitation.created_by)
    event.full_clean()
    event.save(force_insert=True)
    return event


@transaction.atomic
def issue_competition_judge_invitation(competition, round_obj, track, email, expires_at,
                                      *, organization, user):
    """Issue a digest-only judge link for one track in the current round."""
    competition = (Competition.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=competition.pk).first())
    if competition is None:
        raise PermissionDenied
    if competition.status not in (Competition.Status.DRAFT, Competition.Status.ACTIVE):
        raise ValidationError('Invite judges during setup or while the competition is active.')
    if competition.judge_weight == 0:
        raise ValidationError('Judge invitations are not used for a 100% audience-scored event.')
    scoped_round = (CompetitionRound.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=round_obj.pk, competition=competition).first())
    scoped_track = (CompetitionTrack.objects.for_organization(organization).owned_by(user)
        .filter(pk=track.pk, competition=competition).first())
    latest_round = (CompetitionRound.objects.filter(competition=competition)
        .order_by('-order').values_list('pk', flat=True).first())
    if scoped_round is None or scoped_track is None or scoped_round.pk != latest_round:
        raise PermissionDenied
    if not CompetitionRoundEntry.objects.filter(round=scoped_round, track=scoped_track).exists():
        raise ValidationError('The invited track needs entries in this round.')
    if CompetitionRoundEntry.objects.filter(round=scoped_round, track=scoped_track).filter(
            Q(judge_score__isnull=False) | Q(score__isnull=False)).exists():
        raise ValidationError('Judge invitations are locked after round scoring is finalized.')
    if not isinstance(email, str) or not email.strip() or len(email.strip()) > 254:
        raise ValidationError('Enter a valid judge email address.')
    if CompetitionJudgeInvitation.objects.filter(round=scoped_round, track=scoped_track,
            email__iexact=email.strip(), revoked_at__isnull=True, expires_at__gt=timezone.now()).exists():
        raise ValidationError('This judge already has a current invitation for this track and round.')
    token = secrets.token_urlsafe(32)
    invitation = CompetitionJudgeInvitation(competition=competition, round=scoped_round,
        track=scoped_track, email=email.strip(), token_digest=CompetitionJudgeInvitation.digest_token(token),
        expires_at=expires_at, organization_id=competition.organization_id, created_by=user)
    invitation.full_clean()
    invitation.save(force_insert=True)
    _record_competition_judge_audit(invitation, CompetitionJudgeAudit.Action.INVITED, user)
    return invitation, token


def resolve_competition_judge_invitation(token, *, at=None):
    if not token or len(token) > 256:
        return None
    invitation = (CompetitionJudgeInvitation.objects.select_related(
        'competition', 'round', 'track').filter(
            token_digest=CompetitionJudgeInvitation.digest_token(token)).first())
    at = at or timezone.now()
    if (invitation is None or invitation.revoked_at is not None
            or invitation.expires_at <= at):
        return None
    if invitation.submitted_at is not None:
        return invitation
    if invitation.competition.status not in (Competition.Status.DRAFT, Competition.Status.ACTIVE):
        return None
    latest_round = (CompetitionRound.objects.filter(competition=invitation.competition)
        .order_by('-order').values_list('pk', flat=True).first())
    return invitation if invitation.round_id == latest_round else None


@transaction.atomic
def submit_competition_judge_scores(token, scores, *, at=None):
    """Atomically accept a complete 0–100 scorecard once per invitation."""
    if not token or len(token) > 256 or not isinstance(scores, dict):
        raise ValidationError('The judge scorecard is invalid or expired.')
    digest = CompetitionJudgeInvitation.digest_token(token)
    invitation_target = (CompetitionJudgeInvitation.objects.filter(token_digest=digest)
        .values_list('pk', 'competition_id').first())
    if invitation_target is None:
        raise PermissionDenied
    invitation_id, competition_id = invitation_target
    competition = Competition.objects.select_for_update().filter(pk=competition_id).first()
    if competition is None:
        raise PermissionDenied
    invitation = (CompetitionJudgeInvitation.objects.select_for_update()
        .select_related('round', 'track').filter(pk=invitation_id, competition=competition).first())
    now = at or timezone.now()
    if (invitation is None or invitation.revoked_at is not None
            or invitation.expires_at <= now
            or (competition.status != Competition.Status.ACTIVE
                and invitation.submitted_at is None)):
        raise PermissionDenied
    latest_round = (CompetitionRound.objects.filter(competition=competition)
        .order_by('-order').values_list('pk', flat=True).first())
    if invitation.round_id != latest_round and invitation.submitted_at is None:
        raise PermissionDenied
    entries = list(CompetitionRoundEntry.objects.select_for_update()
        .filter(round=invitation.round, track=invitation.track).order_by('entry_id'))
    criteria = list(CompetitionCriterion.objects.filter(track=invitation.track).order_by('order', 'pk'))
    required_criteria = criteria or [None]
    expected_ids = {row.pk for row in entries}
    try:
        submitted_ids = {int(key) for key in scores}
    except (TypeError, ValueError) as exc:
        raise ValidationError('Score every entry in the assigned track.') from exc
    if not entries or submitted_ids != expected_ids or len(scores) != len(expected_ids):
        raise ValidationError('Score every entry in the assigned track exactly once.')
    from .models import CompetitionJudgeAbstention
    abstentions = {}
    normalized = {}
    for row in entries:
        payload = scores.get(str(row.pk), scores.get(row.pk))
        if isinstance(payload, dict) and 'abstention' in payload:
            if (set(payload) != {'abstention', 'reason'}
                    or payload['abstention'] not in ('conflict', 'abstain')
                    or not isinstance(payload['reason'], str)
                    or not payload['reason'].strip() or len(payload['reason'].strip()) > 1000):
                raise ValidationError('Give a valid decision and reason for not scoring this entry.')
            abstentions[row.pk] = (payload['abstention'], payload['reason'].strip())
            continue
        if len(required_criteria) == 1 and not isinstance(payload, dict):
            values_by_criterion = {required_criteria[0].pk if required_criteria[0] else None: payload}
        elif isinstance(payload, dict):
            try:
                submitted_criteria = {int(key): value for key, value in payload.items()}
            except (TypeError, ValueError) as exc:
                raise ValidationError('Score every judging criterion for every entry.') from exc
            expected_criteria = {criterion.pk for criterion in criteria}
            if set(submitted_criteria) != expected_criteria:
                raise ValidationError('Score every judging criterion for every entry exactly once.')
            values_by_criterion = submitted_criteria
        else:
            raise ValidationError('Score every judging criterion for every entry.')
        if set(values_by_criterion) != {criterion.pk if criterion else None for criterion in required_criteria}:
            raise ValidationError('Score every judging criterion for every entry exactly once.')
        for criterion in required_criteria:
            key = criterion.pk if criterion else None
            score = CompetitionJudgeScore(invitation=invitation, round_entry=row,
                criterion=criterion, score=values_by_criterion[key],
                organization_id=invitation.organization_id, created_by=invitation.created_by)
            score.clean_fields()
            normalized[(row.pk, key)] = score.score
    if invitation.submitted_at:
        previous_scores = {(entry_id, criterion_id): value for entry_id, criterion_id, value
            in CompetitionJudgeScore.objects.filter(invitation=invitation)
                .values_list('round_entry_id', 'criterion_id', 'score')}
        previous_abstentions = {row_id: (kind, reason) for row_id, kind, reason
            in invitation.abstentions.values_list('round_entry_id', 'kind', 'reason')}
        if normalized == previous_scores and abstentions == previous_abstentions:
            return list(CompetitionJudgeScore.objects.filter(
                invitation=invitation).order_by('round_entry_id'))
        raise ValidationError('This scorecard was already submitted and cannot be changed.')
    records = []
    for row in entries:
        if row.pk in abstentions:
            kind, reason = abstentions[row.pk]
            abstention = CompetitionJudgeAbstention(invitation=invitation, round_entry=row,
                kind=kind, reason=reason, organization_id=invitation.organization_id,
                created_by=invitation.created_by)
            abstention.full_clean()
            abstention.save(force_insert=True)
            continue
        for criterion in required_criteria:
            criterion_id = criterion.pk if criterion else None
            score = CompetitionJudgeScore(invitation=invitation, round_entry=row,
                criterion=criterion, score=normalized[(row.pk, criterion_id)],
                organization_id=invitation.organization_id, created_by=invitation.created_by)
            score.full_clean()
            records.append(score)
    CompetitionJudgeScore.objects.bulk_create(records)
    invitation.submitted_at = now
    invitation.save(update_fields=('submitted_at', 'updated_at'))
    _record_competition_judge_audit(invitation, CompetitionJudgeAudit.Action.SUBMITTED,
        None)
    return records


@transaction.atomic
def revoke_competition_judge_invitation(competition, invitation_id, *, organization, user,
                                        at=None):
    try:
        invitation_id = int(invitation_id)
    except (TypeError, ValueError) as exc:
        raise PermissionDenied from exc
    competition = (Competition.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=competition.pk, status__in=(Competition.Status.DRAFT, Competition.Status.ACTIVE)).first())
    if competition is None:
        raise PermissionDenied
    invitation = (CompetitionJudgeInvitation.objects.select_for_update()
        .filter(pk=invitation_id, competition=competition).first())
    if invitation is None:
        raise PermissionDenied
    if invitation.submitted_at:
        raise ValidationError('A submitted scorecard cannot be revoked.')
    if invitation.revoked_at:
        raise ValidationError('This judge invitation is already revoked.')
    invitation.revoked_at = at or timezone.now()
    invitation.save(update_fields=('revoked_at', 'updated_at'))
    _record_competition_judge_audit(invitation, CompetitionJudgeAudit.Action.REVOKED, user)
    return invitation


@transaction.atomic
def compile_competition_judge_scores(competition, round_obj, track, *, organization, user):
    """Freeze per-entry judge means and disclose their number of ratings."""
    scoped_competition = (Competition.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=competition.pk, status=Competition.Status.ACTIVE).first())
    if scoped_competition is None:
        raise PermissionDenied
    scoped_round = (CompetitionRound.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=round_obj.pk, competition=scoped_competition).first())
    scoped_track = (CompetitionTrack.objects.for_organization(organization).owned_by(user)
        .filter(pk=track.pk, competition=scoped_competition).first())
    if scoped_round is None or scoped_track is None:
        raise PermissionDenied
    if CompetitionJudgeInvitation.objects.filter(round=scoped_round, track=scoped_track,
            submitted_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now()).exists():
        raise ValidationError('All active judge invitations must be submitted or revoked before tallying.')
    rows = list(CompetitionRoundEntry.objects.for_organization(organization).owned_by(user)
        .filter(round=scoped_round, track=scoped_track).select_for_update().order_by('entry_id'))
    if not rows:
        raise ValidationError('The track has no entries in this round.')
    criteria = list(CompetitionCriterion.objects.for_organization(organization)
        .owned_by(user).filter(track=scoped_track).order_by('order', 'pk'))
    scoring_dimensions = criteria or [None]
    compiled = []
    for row in rows:
        breakdown = []
        judge_mean = Decimal('0')
        judge_count = 0
        for criterion in scoring_dimensions:
            score_rows = CompetitionJudgeScore.objects.filter(
                invitation__competition=scoped_competition,
                invitation__round=scoped_round, invitation__track=scoped_track,
                round_entry=row, criterion=criterion)
            values = list(score_rows.values_list('score', flat=True))
            if len(values) < scoped_competition.effective_minimum_judges:
                label = criterion.name if criterion else 'overall score'
                raise ValidationError(
                    f'Every entry needs at least {scoped_competition.effective_minimum_judges} '
                    f'judge scores for {label} before tallying.')
            mean = sum(values, Decimal('0')) / Decimal(len(values))
            weight = criterion.weight if criterion else Decimal('100')
            judge_mean += mean * weight / Decimal('100')
            judge_count = max(judge_count, len(values))
            breakdown.append({'criterion': criterion.name if criterion else 'Overall score',
                'weight': str(weight), 'mean': str(mean.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
                'judge_count': len(values)})
        if row.judge_score is not None:
            raise ValidationError('Judge means have already been finalized for this track.')
        row.judge_score = judge_mean.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        row.score_breakdown = breakdown
        row.score = (None if scoped_competition.effective_audience_weight > 0 else row.judge_score)
        row.full_clean()
        row.save(update_fields=('judge_score', 'score', 'score_breakdown', 'updated_at'))
        compiled.append({'entry_id': row.entry_id, 'score': row.judge_score,
                         'judge_count': judge_count})
    return compiled


@transaction.atomic
def finalize_competition_weighted_scores(competition, round_obj, *, organization, user, at=None):
    """Freeze complete-ranking audience scores and combine them with judge means."""
    scoped = (Competition.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=competition.pk, status=Competition.Status.ACTIVE).first())
    if scoped is None:
        raise PermissionDenied
    if not Decimal('0') < scoped.effective_audience_weight <= Decimal('100'):
        raise ValidationError('Choose an audience weight between 1% and 100% to finalize audience-scored results.')
    locked_round = (CompetitionRound.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=round_obj.pk, competition=scoped).first())
    latest_id = CompetitionRound.objects.filter(competition=scoped).order_by('-order').values_list('pk', flat=True).first()
    if locked_round is None or locked_round.pk != latest_id:
        raise PermissionDenied
    if locked_round.audience_locked_at:
        raise ValidationError('Audience results for this round have already been finalized.')
    tracks = list(CompetitionTrack.objects.for_organization(organization).owned_by(user)
        .filter(competition=scoped).order_by('pk'))
    rows = list(CompetitionRoundEntry.objects.for_organization(organization).owned_by(user)
        .filter(round=locked_round).select_related('entry', 'track').select_for_update()
        .order_by('track_id', 'entry_id'))
    if not tracks or not rows:
        raise ValidationError('The current round needs entries in every competition track.')
    if scoped.judge_weight > 0 and any(row.judge_score is None for row in rows):
        raise ValidationError('Finalize judge means for every entry before combining results.')
    if CompetitionJudgeInvitation.objects.filter(round=locked_round, submitted_at__isnull=True,
            revoked_at__isnull=True, expires_at__gt=at or timezone.now()).exists():
        raise ValidationError('Submit or revoke every active judge invitation before combining results.')
    rows_by_track = {}
    for row in rows:
        rows_by_track.setdefault(row.track_id, []).append(row)
    ballots_by_track = {}
    ballots = CompetitionAudienceBallot.objects.for_organization(organization).filter(
        competition=scoped, round=locked_round).order_by('submitted_at', 'pk')
    for ballot in ballots:
        ballots_by_track.setdefault(ballot.track_id, []).append(ballot.ranking)
    audience_weight = scoped.effective_audience_weight / Decimal('100')
    judge_weight = Decimal('1') - audience_weight
    for track in tracks:
        track_rows = rows_by_track.get(track.pk, [])
        track_ballots = ballots_by_track.get(track.pk, [])
        if not track_rows or len(track_ballots) < scoped.effective_minimum_audience_ballots:
            raise ValidationError(
                f'Every track, including {track.name}, needs at least '
                f'{scoped.effective_minimum_audience_ballots} audience ballots.')
        candidate_ids = {row.entry_id for row in track_rows}
        ballot_points = {entry_id: [] for entry_id in candidate_ids}
        for ranking in track_ballots:
            if not isinstance(ranking, list) or len(ranking) != len(candidate_ids) or set(ranking) != candidate_ids:
                raise ValidationError('Weighted audience ballots must rank every entry exactly once.')
            for position, entry_id in enumerate(ranking):
                points = (Decimal('100') if len(candidate_ids) == 1 else
                    Decimal(len(candidate_ids) - position - 1) * Decimal('100') /
                    Decimal(len(candidate_ids) - 1))
                ballot_points[entry_id].append(points)
        for row in track_rows:
            audience_score = (sum(ballot_points[row.entry_id], Decimal('0')) /
                Decimal(len(ballot_points[row.entry_id]))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            row.audience_score = audience_score
            judge_score = row.judge_score or Decimal('0')
            row.score = (judge_score * judge_weight + audience_score * audience_weight).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP)
            row.full_clean()
            row.save(update_fields=('audience_score', 'score', 'updated_at'))
    locked_round.audience_locked_at = at or timezone.now()
    locked_round.save(update_fields=('audience_locked_at', 'updated_at'))
    audit = CompetitionWeightedResultAudit(round=locked_round, actor_user=user,
        judge_weight=scoped.judge_weight, audience_weight=scoped.effective_audience_weight,
        audience_ballot_count=sum(len(track_ballots) for track_ballots in ballots_by_track.values()),
        organization_id=scoped.organization_id, created_by=user)
    audit.full_clean()
    audit.save(force_insert=True)
    return rows


@transaction.atomic
def create_next_competition_round(competition, source_round, name, *, organization, user,
                                  tie_break_orders=None, head_judge_decisions=None,
                                  head_judge_reasons=None, at=None):
    """Advance a completed round atomically and record each track's decision."""
    competition = (Competition.objects.for_organization(organization).owned_by(user)
                   .select_for_update().filter(pk=competition.pk).first())
    if competition is None:
        raise PermissionDenied
    if competition.status != Competition.Status.ACTIVE:
        raise ValidationError('Only an active competition can advance a round.')
    source = (CompetitionRound.objects.for_organization(organization).owned_by(user)
              .select_for_update().filter(pk=source_round.pk, competition=competition).first())
    if source is None:
        raise PermissionDenied
    if source.advancement_locked_at:
        raise ValidationError('This round has already advanced.')
    if competition.effective_audience_weight > 0 and source.audience_locked_at is None:
        raise ValidationError('Finalize and lock weighted audience results before advancing this round.')
    if CompetitionRound.objects.filter(competition=competition, order__gt=source.order).exists():
        raise ValidationError('Only the latest competition round can advance.')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise ValidationError('Enter a next-round name of 1 to 100 characters.')
    for value, label in ((tie_break_orders, 'tie-break orders'),
                         (head_judge_decisions, 'head-judge decisions'),
                         (head_judge_reasons, 'head-judge reasons')):
        if value is not None and not isinstance(value, dict):
            raise ValidationError(f'Competition {label} must be grouped by track.')
    tie_break_orders = tie_break_orders or {}
    head_judge_decisions = head_judge_decisions or {}
    head_judge_reasons = head_judge_reasons or {}

    tracks = list(CompetitionTrack.objects.for_organization(organization).owned_by(user)
                  .filter(competition=competition).select_for_update().order_by('pk'))
    if not tracks:
        raise ValidationError('Add at least one competition track before advancing.')
    by_track = {}
    entries = list(CompetitionRoundEntry.objects.for_organization(organization).owned_by(user)
                   .filter(round=source).select_related('entry__track')
                   .select_for_update().order_by('entry__track_id', 'entry__order', 'entry_id'))
    for row in entries:
        if row.track_id != row.entry.track_id:
            raise ValidationError('A round entry is assigned to the wrong competition track.')
        by_track.setdefault(row.track_id, []).append(row)
    if any(track.pk not in by_track for track in tracks):
        raise ValidationError('Every competition track needs round entries before it can advance.')
    allowed_track_ids = {track.pk for track in tracks}
    for payload in (tie_break_orders, head_judge_decisions, head_judge_reasons):
        try:
            payload_ids = {int(track_id) for track_id in payload}
        except (TypeError, ValueError) as exc:
            raise ValidationError('A tie decision references an unknown track.') from exc
        if not payload_ids <= allowed_track_ids:
            raise ValidationError('A tie decision references an unknown track.')

    selections = {}
    normalized_decisions = {}
    for track in tracks:
        rows = by_track[track.pk]
        if any(row.score is None for row in rows):
            raise ValidationError(f'Every entry in track “{track.name}” needs a score before advancement.')
        scores = {row.entry_id: row.score for row in rows}
        tie_order = tie_break_orders.get(track.pk, tie_break_orders.get(str(track.pk), []))
        head_decision = head_judge_decisions.get(track.pk,
            head_judge_decisions.get(str(track.pk)))
        reason = head_judge_reasons.get(track.pk, head_judge_reasons.get(str(track.pk), ''))
        if tie_order and head_decision:
            raise ValidationError('Choose a configured tie-break order or a head-judge decision, not both.')
        selected = competition_advancement(scores, track.advancement_rule,
            track.advancement_value, tie_break_order=tie_order,
            head_judge_decision=head_decision)
        if head_decision and (not isinstance(reason, str) or not reason.strip()):
            raise ValidationError('A head-judge advancement decision needs a reason.')
        if not head_decision and reason:
            raise ValidationError('A head-judge reason requires a head-judge decision.')
        selections[track.pk] = selected
        normalized_decisions[track.pk] = (list(tie_order), list(head_decision or []), reason.strip())

    selected_ids = {entry_id for ids in selections.values() for entry_id in ids}
    if not selected_ids:
        raise ValidationError('The configured rules advanced no entries; no next round was created.')
    next_order = source.order + 1
    next_round = CompetitionRound(competition=competition, name=name.strip(), order=next_order,
        organization_id=competition.organization_id, created_by=user)
    next_round.full_clean()
    next_round.save(force_insert=True)

    for track in tracks:
        selected = selections[track.pk]
        tie_order, head_decision, reason = normalized_decisions[track.pk]
        decision = CompetitionAdvancementDecision(
            source_round=source, next_round=next_round, track=track,
            rule=track.advancement_rule, rule_value=track.advancement_value,
            tie_break_order=tie_order, head_judge_decision=head_decision,
            head_judge_reason=reason, selected_entry_ids=selected,
            organization_id=competition.organization_id, created_by=user)
        decision.full_clean()
        decision.save(force_insert=True)
        for entry_id in selected:
            row = CompetitionRoundEntry(round=next_round, entry_id=entry_id, track=track,
                organization_id=competition.organization_id, created_by=user)
            row.full_clean()
            row.save(force_insert=True)

    source.advancement_locked_at = at or timezone.now()
    source.save(update_fields=('advancement_locked_at', 'updated_at'))
    return next_round, selections


def competition_ranked_choice(ballots, candidate_ids, tie_break_order=()):
    """Public adapter for auditable instant-runoff counting."""
    return ranked_choice_result(ballots, candidate_ids, tie_break_order)


@transaction.atomic
def submit_competition_audience_ballot(competition_public_id, round_id, track_id,
                                      device_token, ranking):
    """Persist one anonymous ranked-choice ballot for the current round/track."""
    competition = (Competition.objects.select_for_update()
        .filter(public_id=competition_public_id).first())
    if competition is None:
        raise ValidationError('This competition ballot is unavailable.')
    if competition.status != Competition.Status.ACTIVE:
        raise ValidationError('Audience voting is only open during an active competition.')
    round_obj = (CompetitionRound.objects.select_for_update()
        .filter(pk=round_id, competition=competition).first())
    track = CompetitionTrack.objects.filter(pk=track_id, competition=competition).first()
    if round_obj is None or track is None:
        raise ValidationError('This competition ballot is unavailable.')
    candidate_count = CompetitionRoundEntry.objects.filter(
        round=round_obj, track=track).count()
    if competition.effective_audience_weight == 0 and candidate_count < 2:
        raise ValidationError('Audience ranked-choice voting requires at least two entries in this track.')
    if round_obj.audience_locked_at is not None:
        raise ValidationError('Audience voting is closed for this round.')
    latest_round_id = (CompetitionRound.objects.filter(competition=competition)
        .order_by('-order').values_list('pk', flat=True).first())
    if latest_round_id != round_obj.pk:
        raise ValidationError('Audience voting is only open for the latest competition round.')
    digest = CompetitionAudienceBallot.digest_device_token(
        device_token, competition, round_obj, track)
    ballot = CompetitionAudienceBallot(
        competition=competition, round=round_obj, track=track,
        device_digest=digest, ranking=ranking,
        organization_id=competition.organization_id)
    ballot.full_clean()
    if competition.effective_audience_weight > 0:
        if not isinstance(ranking, list) or len(ranking) != candidate_count:
            raise ValidationError('This event uses weighted audience scoring; rank every entry to submit a ballot.')
    try:
        ballot.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError('This browser has already voted in this round and track.') from exc
    return ballot


def competition_audience_ranked_choice(competition, round_obj, track, *, organization, user,
                                      tie_break_order=()):
    """Count stored ballots after checking the organizer's complete ownership scope."""
    locked_competition = (Competition.objects.for_organization(organization).owned_by(user)
        .filter(pk=competition.pk).first())
    if locked_competition is None:
        raise PermissionDenied
    scoped_round = (CompetitionRound.objects.for_organization(organization).owned_by(user)
        .filter(pk=round_obj.pk, competition=locked_competition).first())
    scoped_track = (CompetitionTrack.objects.for_organization(organization).owned_by(user)
        .filter(pk=track.pk, competition=locked_competition).first())
    if scoped_round is None or scoped_track is None:
        raise PermissionDenied
    candidate_ids = list(CompetitionRoundEntry.objects.for_organization(organization)
        .owned_by(user).filter(round=scoped_round, track=scoped_track)
        .order_by('entry__order', 'entry_id').values_list('entry_id', flat=True))
    ballots = list(CompetitionAudienceBallot.objects.for_organization(organization)
        .filter(competition=locked_competition, round=scoped_round, track=scoped_track,
                round__competition__created_by=user)
        .order_by('submitted_at', 'pk').values_list('ranking', flat=True))
    return competition_ranked_choice(ballots, candidate_ids, tie_break_order)


def panel_aggregate(scores, method='mean', trim_fraction=None):
    """Use the configured Panel rule without binary floating point."""
    return aggregate_scores(scores, method, trim_fraction)


@transaction.atomic
def create_next_training_session(series, template, name, *, organization, user,
                                 identify_by=None, scheduled_for=None):
    """Create a Team session with a saved rubric and the series roster in one transaction."""
    scope = TrainingSeries.objects.for_organization(organization).owned_by(user)
    locked_series = scope.select_for_update().filter(pk=series.pk, active=True).first()
    if locked_series is None:
        raise PermissionDenied
    if isinstance(template, StarterTemplate):
        if template not in starters_for('team'):
            raise ValidationError('Choose a Team starter rubric.')
        locked_template = template
    else:
        template_scope = (SessionTemplate.objects.for_organization(organization)
                          .for_service('team').owned_by(user).select_for_update())
        locked_template = template_scope.filter(pk=getattr(template, 'pk', None)).first()
        if locked_template is None:
            raise ValidationError('Choose a Team template owned by you in this organization.')
    participants = list(TrainingParticipant.objects.for_organization(organization).owned_by(user)
                        .filter(series=locked_series).select_for_update().order_by('order', 'pk'))
    if not participants:
        raise ValidationError('Add at least one series participant before creating a session.')
    session_settings, rubric_rows = blueprint(locked_template)
    if identify_by:
        if identify_by not in (AssessmentSession.IdentifyBy.FULL_NAME,
                               AssessmentSession.IdentifyBy.STUDENT_ID):
            raise ValidationError('Choose full name or Student ID for learner access.')
        session_settings['identify_by'] = identify_by
    if session_settings['identify_by'] == AssessmentSession.IdentifyBy.STUDENT_ID and any(
            not participant.reference for participant in participants):
        raise ValidationError('Every series participant needs an identifier for this template.')
    if not rubric_rows:
        raise ValidationError('Add at least one criterion to the Team template before creating a session.')

    session = AssessmentSession(
        name=name, organization_id=locked_series.organization_id, service='team', created_by=user,
        **session_settings)
    save_draft_from_template(session, locked_template)

    groups = {}
    for participant in participants:
        if participant.group_name not in groups:
            group = PresentationGroup(assessment_session=session,
                name=participant.group_name, order=len(groups))
            group.full_clean()
            group.save()
            groups[participant.group_name] = group
        learner = Student(assessment_session=session, full_name=participant.display_name,
            student_id=(participant.reference if session.identify_by == AssessmentSession.IdentifyBy.STUDENT_ID else ''))
        learner.full_clean()
        learner.save()
        membership = GroupMembership(group=groups[participant.group_name], student=learner)
        membership.full_clean()
        membership.save()

    latest = (TrainingSeriesSession.objects.for_organization(organization).owned_by(user)
              .filter(series=locked_series).order_by('-sequence').values_list('sequence', flat=True).first() or 0)
    link = TrainingSeriesSession(series=locked_series, assessment_session=session,
        organization_id=locked_series.organization_id, created_by=user, sequence=latest + 1,
        scheduled_for=scheduled_for)
    link.full_clean()
    link.save()
    competencies = (TrainingCompetency.objects.for_organization(organization).owned_by(user)
                    .filter(series=locked_series).select_for_update().order_by('order', 'pk'))
    for competency in competencies:
        snapshot = TrainingSessionCompetency(
            series_session=link, source_competency=competency,
            name=competency.name, description=competency.description,
            passing_score=competency.passing_score, order=competency.order,
            organization_id=locked_series.organization_id, created_by=user)
        snapshot.full_clean()
        snapshot.save(force_insert=True)
    return session


def training_series_competency_trends(series, *, organization, user):
    """Build learner and group mean trends from the latest decision per demo."""
    scoped_series = TrainingSeries.objects.for_organization(organization).owned_by(user).filter(
        pk=series.pk, active=True).first()
    if scoped_series is None:
        raise PermissionDenied
    links = list(TrainingSeriesSession.objects.for_organization(organization).owned_by(user)
        .filter(series=scoped_series).select_related('assessment_session').order_by('sequence', 'pk'))
    if len(links) < 3:
        return {'learners': [], 'teams': []}
    links_by_id = {link.pk: link for link in links}

    def normalized_group_name(name):
        return ' '.join(name.split()).casefold()

    def learner_key(student):
        if student.student_id and student.student_id.strip():
            return ('id', student.student_id.strip().casefold())
        if student.full_name and student.full_name.strip():
            return ('name', ' '.join(student.full_name.split()).casefold())
        return None

    # Names are a fallback for full-name Team templates. Ambiguous duplicate
    # names in one session are omitted instead of merged into one learner.
    identities_by_session = {}
    learners = {}
    team_groups_by_session = {}
    team_members_by_session = {}
    for link in links:
        students = list(link.assessment_session.students.all())
        counts = {}
        for student in students:
            key = learner_key(student)
            if key:
                counts[key] = counts.get(key, 0) + 1
        identities_by_session[link.pk] = {
            student.pk: learner_key(student) for student in students
            if learner_key(student) and counts[learner_key(student)] == 1
        }
        for student in students:
            key = learner_key(student)
            if key and counts[key] == 1:
                learners.setdefault(key, student.full_name)

        groups = list(PresentationGroup.objects.filter(
            assessment_session=link.assessment_session, is_practice=False))
        team_name_counts = {}
        for group in groups:
            normalized = normalized_group_name(group.name)
            if normalized:
                team_name_counts[normalized] = team_name_counts.get(normalized, 0) + 1
        team_groups_by_session[link.pk] = {
            normalized_group_name(group.name): group.name
            for group in groups
            if normalized_group_name(group.name)
            and team_name_counts[normalized_group_name(group.name)] == 1
        }
        member_rows = list(GroupMembership.objects.filter(group__in=groups)
            .select_related('student', 'group').order_by('student_id', 'pk'))
        member_counts = {}
        for membership in member_rows:
            member_counts[membership.student_id] = member_counts.get(membership.student_id, 0) + 1
        team_members_by_session[link.pk] = {
            membership.student_id: normalized_group_name(membership.group.name)
            for membership in member_rows
            if member_counts[membership.student_id] == 1
            and normalized_group_name(membership.group.name) in team_groups_by_session[link.pk]
        }

    latest_results = {}
    result_rows = (TrainingCompetencyResult.objects.for_organization(organization).owned_by(user)
        .filter(criterion__series_session__in=links)
        .select_related('criterion', 'criterion__series_session', 'student')
        .order_by('criterion__series_session__sequence', 'criterion__order', 'student_id', '-revision'))
    for result in result_rows:
        link = result.criterion.series_session
        competency = (('source', result.criterion.source_competency_id)
                      if result.criterion.source_competency_id
                      else ('name', ' '.join(result.criterion.name.split()).casefold()))
        latest_results.setdefault((competency, result.student_id, link.pk), result)

    groups = {}
    team_scores = {}
    for (competency, student_id, link_id), result in latest_results.items():
        session_link = links_by_id[link_id]
        learner = identities_by_session.get(link_id, {}).get(student_id)
        if learner is not None:
            groups.setdefault((competency, learner), []).append((session_link, result))
        team_key = team_members_by_session.get(link_id, {}).get(student_id)
        if team_key is not None:
            team_scores.setdefault((competency, team_key, link_id), []).append(result)

    trends = []
    for (competency, learner), values in groups.items():
        values.sort(key=lambda item: (item[0].sequence, item[0].pk))
        if len(values) < 3:
            continue
        trends.append({
            'learner': learners[learner],
            'competency': values[0][1].criterion.name,
            'sessions': [(link.assessment_session.name, result.score, result.passed)
                         for link, result in values],
            'first_score': values[0][1].score,
            'latest_score': values[-1][1].score,
            'change': values[-1][1].score - values[0][1].score,
        })
    teams = {}
    for (competency, team_key, link_id), results in team_scores.items():
        team_values = teams.setdefault((competency, team_key), [])
        team_values.append((links_by_id[link_id],
                            aggregate_scores([result.score for result in results], 'mean'),
                            len(results), results[0].criterion.name))
    team_trends = []
    for (competency, team_key), values in teams.items():
        values.sort(key=lambda item: (item[0].sequence, item[0].pk))
        if len(values) < 3:
            continue
        team_trends.append({
            'team': team_groups_by_session[values[0][0].pk][team_key],
            'competency': values[0][3],
            'sessions': [(link.assessment_session.name, score, learner_count)
                         for link, score, learner_count, _ in values],
            'first_score': values[0][1], 'latest_score': values[-1][1],
            'change': values[-1][1] - values[0][1],
        })
    return {
        'learners': sorted(trends,
            key=lambda item: (item['learner'].casefold(), item['competency'].casefold())),
        'teams': sorted(team_trends,
            key=lambda item: (item['team'].casefold(), item['competency'].casefold())),
    }


def training_series_competency_export(series, *, organization, user):
    """Return only each learner's latest traceable outcome per demo criterion."""
    scoped_series = TrainingSeries.objects.for_organization(organization).owned_by(user).filter(
        pk=series.pk, active=True).first()
    if scoped_series is None:
        raise PermissionDenied
    links = list(TrainingSeriesSession.objects.for_organization(organization).owned_by(user)
        .filter(series=scoped_series).select_related('assessment_session')
        .order_by('sequence', 'pk'))
    if not links:
        return []
    latest = {}
    results = (TrainingCompetencyResult.objects.for_organization(organization).owned_by(user)
        .filter(criterion__series_session__in=links)
        .select_related('criterion', 'criterion__series_session',
                        'criterion__series_session__assessment_session', 'student')
        .order_by('criterion__series_session__sequence', 'criterion__order',
                  'student__full_name', 'student_id', '-revision'))
    for result in results:
        link = result.criterion.series_session
        if result.student.assessment_session_id != link.assessment_session_id:
            continue
        latest.setdefault((result.criterion_id, result.student_id), result)
    rows = []
    for result in latest.values():
        link = result.criterion.series_session
        rows.append({
            'series': scoped_series.name,
            'sequence': link.sequence,
            'session': link.assessment_session.name,
            'learner_reference': result.student.student_id,
            'learner_name': result.student.full_name,
            'competency': result.criterion.name,
            'score': result.score,
            'passing_score': result.criterion.passing_score,
            'passed': result.passed,
            'revision': result.revision,
            'assessed_at': result.assessed_at,
        })
    return rows


@transaction.atomic
def record_training_competency_result(session, criterion, student, score, evidence,
                                      *, organization, user, correction_reason=''):
    """Record an assessor decision or append a reasoned correction revision."""
    locked_session = (AssessmentSession.objects.for_organization(organization)
                      .for_service('team').owned_by(user).select_for_update()
                      .filter(pk=session.pk).first())
    if locked_session is None:
        raise PermissionDenied
    series_link = (TrainingSeriesSession.objects.for_organization(organization).owned_by(user)
                   .filter(assessment_session=locked_session).select_related('series').first())
    if series_link is None:
        raise PermissionDenied
    locked_criterion = (TrainingSessionCompetency.objects.for_organization(organization)
                        .owned_by(user).select_for_update()
                        .filter(pk=criterion.pk, series_session=series_link).first())
    if locked_criterion is None:
        raise PermissionDenied
    locked_student = (Student.objects.filter(assessment_session=locked_session)
                      .filter(pk=student.pk).first())
    if locked_student is None:
        raise PermissionDenied
    latest = (TrainingCompetencyResult.objects.for_organization(organization).owned_by(user)
              .filter(criterion=locked_criterion, student=locked_student)
              .order_by('-revision').first())
    if latest is None:
        if correction_reason:
            raise ValidationError('A correction reason is only allowed when a previous decision exists.')
        revision, supersedes = 1, None
    else:
        if not correction_reason or not correction_reason.strip():
            raise ValidationError('A reason is required to correct the previous competency decision.')
        revision, supersedes = latest.revision + 1, latest
    result = TrainingCompetencyResult(
        criterion=locked_criterion, student=locked_student, score=score,
        evidence=evidence, correction_reason=correction_reason.strip(),
        revision=revision, supersedes=supersedes, assessed_by=user,
        organization_id=locked_session.organization_id, created_by=user)
    result.full_clean()
    try:
        result.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError('A competency decision was submitted at the same time. Reload and retry.') from exc
    from .models import TrainingLearnerReportLink
    TrainingLearnerReportLink.objects.filter(series_session=series_link, student=locked_student,
        revoked_at__isnull=True).update(revoked_at=timezone.now())
    return result


@transaction.atomic
def issue_training_certificates(session, *, organization, user):
    """Issue immutable certificates for current passing results after a Team demo closes."""
    locked_session = (AssessmentSession.objects.for_organization(organization)
        .for_service('team').owned_by(user).select_for_update().filter(pk=session.pk).first())
    if locked_session is None:
        raise PermissionDenied
    if locked_session.status != AssessmentSession.Status.CLOSED:
        raise ValidationError('Close the Team demo before issuing certificates.')
    series_link = (TrainingSeriesSession.objects.for_organization(organization).owned_by(user)
        .select_for_update().select_related('series').filter(assessment_session=locked_session).first())
    if series_link is None:
        raise PermissionDenied
    results = (TrainingCompetencyResult.objects.for_organization(organization).owned_by(user)
        .filter(criterion__series_session=series_link)
        .select_related('criterion', 'criterion__series_session__series',
                        'criterion__series_session__assessment_session', 'student')
        .order_by('criterion_id', 'student_id', '-revision', '-pk'))
    latest = {}
    for result in results:
        latest.setdefault((result.criterion_id, result.student_id), result)
    issued = []
    for result in latest.values():
        if not result.passed:
            continue
        certificate = (TrainingCertificate.objects.for_organization(organization)
            .owned_by(user).filter(result=result).first())
        if certificate is None:
            certificate = TrainingCertificate(result=result,
                learner_name=result.student.full_name,
                learner_reference=result.student.student_id,
                series_name=series_link.series.name,
                session_name=locked_session.name,
                competency_name=result.criterion.name,
                score=result.score, passing_score=result.criterion.passing_score,
                issued_by=user, organization_id=locked_session.organization_id,
                created_by=user)
            certificate.full_clean()
            try:
                with transaction.atomic():
                    certificate.save(force_insert=True)
            except IntegrityError:
                certificate = (TrainingCertificate.objects.for_organization(organization)
                    .owned_by(user).filter(result=result).first())
                if certificate is None:
                    raise
        issued.append(certificate)
    return issued


@transaction.atomic
def submit_training_feedback(feedback_public_id, device_token, score):
    """Accept one anonymous feedback rating during active drill or post-session."""
    series_session = (TrainingSeriesSession.objects.select_for_update()
        .select_related('series', 'assessment_session')
        .filter(feedback_public_id=feedback_public_id).first())
    if series_session is None:
        raise ValidationError('This feedback link is unavailable.')
    if series_session.assessment_session.status != AssessmentSession.Status.CLOSED:
        raise ValidationError('Feedback is accepted after the demo session closes.')
    if series_session.series.feedback_minimum_responses is None:
        raise ValidationError('The series owner has not configured the feedback privacy threshold.')
    digest = TrainingFeedback.digest_device_token(device_token, series_session)
    feedback = TrainingFeedback(series_session=series_session, device_digest=digest,
        score=score, organization_id=series_session.organization_id)
    feedback.full_clean()
    try:
        feedback.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError('This browser has already submitted feedback for this demo.') from exc
    return feedback


def training_series_feedback_summary(series, *, organization, user):
    """Return only thresholded aggregates for owner-authorized series sessions."""
    locked_series = (TrainingSeries.objects.for_organization(organization).owned_by(user)
        .filter(pk=series.pk, active=True).first())
    if locked_series is None:
        raise PermissionDenied
    floor = locked_series.feedback_minimum_responses
    links = (TrainingSeriesSession.objects.for_organization(organization).owned_by(user)
        .filter(series=locked_series).select_related('assessment_session').order_by('sequence'))
    summaries = []
    for link in links:
        rows = TrainingFeedback.objects.for_organization(organization).filter(
            series_session=link, series_session__series__created_by=user)
        count = rows.count()
        aggregate = None
        if floor is not None and count >= floor:
            aggregate = aggregate_anonymous_feedback(
                rows.values_list('score', flat=True), minimum_responses=floor)
        summaries.append({
            'session': link.assessment_session,
            'sequence': link.sequence,
            'response_count': count if aggregate is not None else None,
            'minimum_responses': floor,
            'aggregate': aggregate,
        })
    return summaries


@transaction.atomic
def compile_panel_results(hearing, *, organization, user):
    """Close scoring after minimum coverage and freeze anonymous aggregates."""
    locked = (PanelHearing.objects.for_organization(organization).owned_by(user)
              .select_for_update().filter(pk=hearing.pk).first())
    if locked is None:
        raise PermissionDenied
    if locked.status != PanelHearing.Status.OPEN:
        raise ValidationError('Only an open hearing can be closed for review.')
    scoring_required = locked.workflow in (PanelHearing.Workflow.SCORED, PanelHearing.Workflow.HYBRID)
    voting_required = locked.workflow in (PanelHearing.Workflow.VOTE, PanelHearing.Workflow.HYBRID,
                                           PanelHearing.Workflow.REVIEW_VOTE)
    review_required = locked.workflow in (PanelHearing.Workflow.REVIEW,
                                           PanelHearing.Workflow.REVIEW_VOTE)
    candidates = list(PanelCandidate.objects.filter(hearing=locked).order_by('order', 'pk'))
    criteria = list(PanelCriterion.objects.filter(hearing=locked).order_by('order', 'pk'))
    panels = list(PanelScoringPanel.objects.filter(hearing=locked).prefetch_related('criteria')
        .order_by('pk'))
    if not panels:
        panels = [create_default_panel(locked)]
    if (len(panels) == 1 and panels[0].name == 'Default panel'
            and not panels[0].configuration_managed):
        # Keep the pre-panel hearing configuration authoritative until an owner
        # explicitly edits the default panel in the new panel configuration UI.
        panels[0].aggregation = locked.aggregation
        panels[0].minimum_examiners = locked.minimum_examiners
        panels[0].trim_fraction = locked.trim_fraction
    classroom_compatible_default = (
        len(panels) == 1
        and panels[0].name == 'Default panel'
        and not panels[0].configuration_managed
        and panels[0].share == Decimal('100.00')
        and panels[0].aggregation == PanelHearing.Aggregation.MEAN
    ) if panels else False
    if not candidates or (scoring_required and not criteria):
        raise ValidationError('A hearing needs candidates and the required criteria before review.')
    if scoring_required and sum((panel.share for panel in panels), Decimal('0')) != Decimal('100.00'):
        raise ValidationError('Panel shares must total exactly 100% before results can be compiled.')
    if scoring_required and any(not panel.criteria.exists() for panel in panels):
        raise ValidationError('Assign at least one criterion to every panel before results can be compiled.')

    participation_gaps = []
    if voting_required:
        ballots = PanelVote.objects.filter(hearing=locked).exclude(choice=PanelVote.Choice.ABSTAIN)
        for candidate in candidates:
            for panel in panels:
                count = ballots.filter(candidate=candidate, invitation__panel=panel).values(
                    'invitation_id').distinct().count()
                if count < panel.minimum_examiners:
                    participation_gaps.append(
                        f'{candidate.display_name} / {panel.name}: {count} of '
                        f'{panel.minimum_examiners} non-abstaining ballots')
    if review_required:
        reports = PanelWrittenReview.objects.filter(hearing=locked).exclude(
            invitation__panel__isnull=True)
        for candidate in candidates:
            conflicted_invites = PanelConflict.objects.filter(
                candidate=candidate, invitation__hearing=locked).values('invitation_id')
            for panel in panels:
                count = reports.exclude(invitation_id__in=conflicted_invites).filter(
                    candidate=candidate, invitation__panel=panel).values(
                    'invitation_id').distinct().count()
                if count < panel.minimum_examiners:
                    participation_gaps.append(
                        f'{candidate.display_name} / {panel.name}: {count} of '
                        f'{panel.minimum_examiners} written reviews')
    if participation_gaps:
        shown = '; '.join(participation_gaps[:10])
        remainder = len(participation_gaps) - 10
        if remainder > 0:
            shown += f'; and {remainder} more'
        raise ValidationError('Required examiner participation is incomplete: ' + shown +
            '. Review the candidate and panel, then follow up with an eligible examiner.')

    conflicts = set(PanelConflict.objects.filter(invitation__hearing=locked)
                    .values_list('invitation_id', 'candidate_id'))
    scores = {}
    for score in (PanelScore.objects.filter(invitation__hearing=locked,
            candidate__hearing=locked, criterion__hearing=locked)
            .select_related('invitation').order_by('candidate_id', 'criterion_id', 'invitation_id')):
        if (score.invitation_id, score.candidate_id) not in conflicts:
            scores.setdefault((score.candidate_id, score.criterion_id), []).append(score)

    if scoring_required:
        missing_coverage = []
        for candidate in candidates:
            for criterion in criteria:
                assigned = [panel for panel in panels if criterion in panel.criteria.all()
                    and panel.share > 0]
                for panel in assigned:
                    eligible_count = len({row.invitation_id for row in
                        scores.get((candidate.pk, criterion.pk), [])
                        if row.invitation.panel_id == panel.pk})
                    if not eligible_count:
                        missing_coverage.append(
                            f'{candidate.display_name} / {criterion.name} / {panel.name}')
        if missing_coverage:
            shown = '; '.join(missing_coverage[:10])
            remainder = len(missing_coverage) - 10
            if remainder > 0:
                shown += f'; and {remainder} more'
            raise ValidationError(
                'Results cannot be compiled because no eligible examiner scored: '
                f'{shown}. Check assignments and recusals, then invite a replacement if needed.')

    snapshots = []
    for candidate in candidates if scoring_required else []:
        for criterion in criteria:
            assigned = ([panel for panel in panels if criterion in panel.criteria.all()
                         and panel.share > 0] if scoring_required else [])
            if not assigned:
                if scoring_required:
                    raise ValidationError(
                        f'Criterion “{criterion.name}” must be assigned to a panel with a positive share.')
                continue
            assigned_share_total = sum((panel.share for panel in assigned), Decimal('0'))
            weighted_total = Decimal('0')
            total_raters = set()
            methodology = []
            below_minimum = False
            for panel in assigned:
                panel_rows = [row for row in scores.get((candidate.pk, criterion.pk), [])
                    if row.invitation.panel_id == panel.pk]
                unique_raters = {row.invitation_id for row in panel_rows}
                if not unique_raters:
                    raise ValidationError(
                        f'Candidate “{candidate.display_name}” has no eligible examiner score '
                        f'for “{criterion.name}”.')
                panel_below_minimum = len(unique_raters) < panel.minimum_examiners
                below_minimum = below_minimum or panel_below_minimum
                # Means may repeat; normalize to the snapshot/methodology
                # precision before model validation and weighted combination.
                value = panel_aggregate([row.score for row in panel_rows], panel.aggregation,
                    panel.trim_fraction).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)
                effective_share = panel.share / assigned_share_total
                weighted_total += value * effective_share
                total_raters.update(unique_raters)
                methodology.append({
                    'panel': panel.name,
                    'share': format(panel.share, 'f'),
                    'effective_share': format((effective_share * Decimal('100')).quantize(
                        Decimal('0.01'), rounding=ROUND_HALF_UP), 'f'),
                    'score': format(value, 'f'),
                    'rater_count': len(unique_raters),
                    'minimum_examiners': panel.minimum_examiners,
                    'below_minimum': panel_below_minimum,
                    'aggregation': panel.aggregation,
                    'trim_fraction': (format(panel.trim_fraction, 'f')
                        if panel.trim_fraction is not None else None),
                })
            single_panel = len(methodology) == 1
            # Classroom's published score uses one decimal place and
            # ROUND_HALF_UP. Keep the untouched default Panel comparable while
            # retaining six decimals for configured Panel methodologies.
            result_score = weighted_total.quantize(
                Decimal('0.1') if classroom_compatible_default else Decimal('0.000001'),
                rounding=ROUND_HALF_UP)
            snapshot = PanelResultSnapshot(
                hearing=locked, candidate=candidate, criterion=criterion,
                score=result_score, rater_count=len(total_raters),
                below_minimum=below_minimum,
                aggregation=(assigned[0].aggregation if single_panel else PanelHearing.Aggregation.MEAN),
                trim_fraction=(assigned[0].trim_fraction if single_panel else None),
                panel_methodology=methodology,
                organization_id=locked.organization_id, created_by=user)
            snapshot.full_clean()
            snapshot.save(force_insert=True)
            snapshots.append(snapshot)

    locked.status = PanelHearing.Status.REVIEW
    locked.active_candidate = None
    locked.save(update_fields=('status', 'active_candidate', 'updated_at'))
    review_reason = ('Required assessment coverage met; anonymous numeric snapshots compiled.'
        if scoring_required else
        'Decision workflow closed; private ballots and chair outcome review opened.')
    _record_panel_audit(locked, PanelAuditEvent.Action.RESULTS_COMPILED,
        review_reason, actor_user=user)
    return snapshots


@transaction.atomic
def adjust_panel_result(snapshot, adjusted_score, reason, *, organization, user):
    """Append a reasoned correction after sign-off without rewriting source results."""
    hearing_id = (PanelResultSnapshot.objects.for_organization(organization).owned_by(user)
        .filter(pk=snapshot.pk).values_list('hearing_id', flat=True).first())
    if hearing_id is None:
        raise PermissionDenied
    hearing = (PanelHearing.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=hearing_id).first())
    if hearing is None:
        raise PermissionDenied
    if hearing.status not in (PanelHearing.Status.SIGNED_OFF, PanelHearing.Status.RELEASED):
        raise ValidationError('A result can be adjusted only after sign-off.')
    locked_snapshot = (PanelResultSnapshot.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=snapshot.pk, hearing=hearing).first())
    if locked_snapshot is None:
        raise PermissionDenied
    previous = (PanelResultAdjustment.objects.for_organization(organization).owned_by(user)
        .filter(snapshot=locked_snapshot).order_by('-revision').first())
    reason = reason.strip() if isinstance(reason, str) else ''
    adjustment = PanelResultAdjustment(
        snapshot=locked_snapshot, adjusted_score=adjusted_score, reason=reason,
        revision=(previous.revision + 1 if previous else 1), supersedes=previous,
        adjusted_by=user, organization_id=hearing.organization_id, created_by=user)
    adjustment.full_clean()
    adjustment.save(force_insert=True)
    _record_panel_audit(hearing, PanelAuditEvent.Action.RESULT_ADJUSTED,
        adjustment.reason, candidate=locked_snapshot.candidate, actor_user=user)
    return adjustment


@transaction.atomic
def redact_panel_comment(hearing, score, reason, *, organization, user):
    """Remove a comment during review while retaining its digest and reasoned audit."""
    locked_hearing = (PanelHearing.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=hearing.pk).first())
    if locked_hearing is None:
        raise PermissionDenied
    if locked_hearing.status != PanelHearing.Status.REVIEW:
        raise ValidationError('Examiner comments can be redacted only while results are under review.')
    locked_score = (PanelScore.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=score.pk, candidate__hearing=locked_hearing,
            criterion__hearing=locked_hearing).first())
    if locked_score is None:
        raise PermissionDenied
    if not locked_score.comment:
        raise ValidationError('This score has no comment available for redaction.')
    reason = reason.strip() if isinstance(reason, str) else ''
    if not reason:
        raise ValidationError('A reason is required for every comment redaction.')
    digest = hashlib.sha256(locked_score.comment.encode('utf-8')).hexdigest()
    redaction = PanelCommentRedaction(
        hearing=locked_hearing, score=locked_score, original_comment_digest=digest,
        reason=reason, moderated_by=user, organization_id=locked_hearing.organization_id,
        created_by=locked_hearing.created_by)
    redaction.full_clean()
    redaction.save(force_insert=True)
    PanelScore.objects.filter(pk=locked_score.pk, organization_id=locked_hearing.organization_id,
        created_by=locked_hearing.created_by).update(comment='')
    _record_panel_audit(locked_hearing, PanelAuditEvent.Action.COMMENT_REDACTED,
        reason, candidate=locked_score.candidate, actor_user=user)
    return redaction


@transaction.atomic
def release_panel_comment(hearing, score, reason, *, organization, user):
    """Approve one anonymous examiner comment for the entrant's released results."""
    locked_hearing = (PanelHearing.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=hearing.pk).first())
    if locked_hearing is None:
        raise PermissionDenied
    if locked_hearing.status != PanelHearing.Status.REVIEW:
        raise ValidationError('Comments can be approved only while results are under review.')
    locked_score = (PanelScore.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=score.pk, candidate__hearing=locked_hearing,
            criterion__hearing=locked_hearing).first())
    if locked_score is None:
        raise PermissionDenied
    if not locked_score.comment:
        raise ValidationError('This score has no comment available for release.')
    if locked_score.comment_released_to_entrant:
        raise ValidationError('This comment is already approved for release.')
    reason = reason.strip() if isinstance(reason, str) else ''
    if not reason:
        raise ValidationError('A reason is required before releasing a comment.')
    locked_score.comment_released_to_entrant = True
    locked_score.save(update_fields=('comment_released_to_entrant', 'updated_at'))
    _record_panel_audit(locked_hearing, PanelAuditEvent.Action.COMMENT_RELEASED,
        reason, candidate=locked_score.candidate, actor_user=user)
    return locked_score


@transaction.atomic
def confirm_panel_signoff(hearing, *, organization, user, at=None):
    """Move a reviewed hearing to immutable signed-off state for its owner."""
    hearing = (PanelHearing.objects.for_organization(organization).owned_by(user)
               .select_for_update().filter(pk=hearing.pk).first())
    if hearing is None:
        raise PermissionDenied
    if hearing.status != PanelHearing.Status.REVIEW:
        raise ValidationError('Only a hearing under review can be signed off.')
    if hearing.workflow in (PanelHearing.Workflow.OUTCOME, PanelHearing.Workflow.VOTE,
                            PanelHearing.Workflow.HYBRID, PanelHearing.Workflow.REVIEW,
                            PanelHearing.Workflow.REVIEW_VOTE):
        missing = PanelCandidate.objects.filter(hearing=hearing).exclude(
            pk__in=PanelDecision.objects.filter(hearing=hearing).values('candidate_id')).exists()
        if missing:
            raise ValidationError('Record a reasoned chair outcome for every candidate before sign-off.')
    if hearing.workflow in (PanelHearing.Workflow.SCORED, PanelHearing.Workflow.HYBRID):
        expected = PanelCandidate.objects.filter(hearing=hearing).count() * PanelCriterion.objects.filter(
            hearing=hearing).count()
        actual = PanelResultSnapshot.objects.filter(hearing=hearing).count()
        if not expected or actual < expected:
            raise ValidationError('Complete result compilation before signing off.')
    hearing.status = PanelHearing.Status.SIGNED_OFF
    hearing.signed_off_at = at or timezone.now()
    hearing.save(update_fields=('status', 'signed_off_at', 'updated_at'))
    _record_panel_audit(hearing, PanelAuditEvent.Action.RESULTS_SIGNED_OFF,
        'Hearing results signed off by owner.', actor_user=user)
    return hearing


@transaction.atomic
def release_panel_results(hearing, *, organization, user, at=None):
    """Release only signed-off results, recording the release timestamp."""
    hearing = (PanelHearing.objects.for_organization(organization).owned_by(user)
               .select_for_update().filter(pk=hearing.pk).first())
    if hearing is None:
        raise PermissionDenied
    if hearing.status != PanelHearing.Status.SIGNED_OFF or not hearing.signed_off_at:
        raise ValidationError('Results must be signed off before release.')
    hearing.status = PanelHearing.Status.RELEASED
    hearing.released_at = at or timezone.now()
    hearing.save(update_fields=('status', 'released_at', 'updated_at'))
    _record_panel_audit(hearing, PanelAuditEvent.Action.RESULTS_RELEASED,
        'Signed-off hearing results released by owner.', actor_user=user)
    return hearing


@transaction.atomic
def issue_panel_entrant_result_link(hearing, candidate, *, organization, user):
    """Issue a one-time random bearer URL for one candidate's released results."""
    locked_hearing = (PanelHearing.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=hearing.pk).first())
    if locked_hearing is None:
        raise PermissionDenied
    if locked_hearing.status != PanelHearing.Status.RELEASED or not locked_hearing.released_at:
        raise ValidationError('Entrant result links can be issued only after release.')
    locked_candidate = (PanelCandidate.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=candidate.pk, hearing=locked_hearing).first())
    if locked_candidate is None:
        raise PermissionDenied
    token = secrets.token_urlsafe(32)
    link = PanelEntrantResultLink(hearing=locked_hearing, candidate=locked_candidate,
        token_digest=PanelEntrantResultLink.digest_token(token),
        organization_id=locked_hearing.organization_id, created_by=user)
    link.full_clean()
    link.save(force_insert=True)
    _record_panel_audit(locked_hearing, PanelAuditEvent.Action.RESULT_LINK_ISSUED,
        'Entrant result link issued after results release.', candidate=locked_candidate,
        actor_user=user)
    return link, token


def resolve_panel_entrant_result_link(token):
    """Resolve an active opaque entrant link only while its hearing stays released."""
    if not token or len(token) > 128:
        return None
    link = (PanelEntrantResultLink.objects.select_related('hearing', 'candidate')
        .filter(token_digest=PanelEntrantResultLink.digest_token(token), revoked_at__isnull=True,
                hearing__status=PanelHearing.Status.RELEASED)
        .first())
    if link is None or not link.hearing.released_at:
        return None
    return link


@transaction.atomic
def revoke_panel_entrant_result_link(hearing, link, reason, *, organization, user, at=None):
    """Revoke one released-result capability and retain its audit history."""
    locked_hearing = (PanelHearing.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=hearing.pk, status=PanelHearing.Status.RELEASED).first())
    if locked_hearing is None:
        raise PermissionDenied
    locked_link = (PanelEntrantResultLink.objects.for_organization(organization).owned_by(user)
        .select_for_update().filter(pk=link.pk, hearing=locked_hearing,
                                    revoked_at__isnull=True).first())
    if locked_link is None:
        raise PermissionDenied
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError('A revocation reason is required.')
    locked_link.revoked_at = at or timezone.now()
    locked_link.save(update_fields=('revoked_at', 'updated_at'))
    _record_panel_audit(locked_hearing, PanelAuditEvent.Action.RESULT_LINK_REVOKED,
        reason.strip(), candidate=locked_link.candidate, actor_user=user)
    return locked_link


DEFAULT_PANEL_PHASE_DURATIONS = {
    'presentation': 900,
    'qa': 1200,
    'deliberation': 600,
    'decision': 300,
}

NEXT_PANEL_PHASES = {
    'presentation': 'qa',
    'qa': 'deliberation',
    'deliberation': 'decision',
}


def get_panel_live_state_context(hearing):
    """Compile chamber state, candidate docket, examiner readiness, and score convergence."""
    org = hearing.organization_id
    candidates = list(PanelCandidate.objects.filter(hearing=hearing, organization_id=org).order_by('order', 'pk'))
    decisions = {d.candidate_id: d for d in PanelDecision.objects.filter(hearing=hearing, organization_id=org).order_by('revision')}

    active_candidate = hearing.active_candidate

    chamber_state = (hearing.workflow_snapshot or {}).get('chamber_state', {})
    custom_durations = chamber_state.get('phase_durations') or {}
    phase_durations_mins = {
        'presentation': int(custom_durations.get('presentation', 900)) // 60,
        'qa': int(custom_durations.get('qa', 1200)) // 60,
        'deliberation': int(custom_durations.get('deliberation', 600)) // 60,
        'decision': int(custom_durations.get('decision', 300)) // 60,
    }
    if hearing.status == PanelHearing.Status.OPEN and (not chamber_state or not chamber_state.get('phase_started_at')):
        now = timezone.now()
        init_duration = int(custom_durations.get('presentation', 900))
        chamber_state = {
            'active_phase': chamber_state.get('active_phase', 'presentation'),
            'phase_duration_seconds': chamber_state.get('phase_duration_seconds', init_duration),
            'phase_seconds_remaining': chamber_state.get('phase_seconds_remaining', init_duration),
            'phase_durations': custom_durations,
            'phase_started_at': now.isoformat(),
            'elapsed_before_pause': 0,
            'timer_paused': False,
            'timer_paused_at': None,
            'display_mode': chamber_state.get('display_mode', 'in_session'),
        }
        snapshot = dict(hearing.workflow_snapshot or {})
        snapshot['chamber_state'] = chamber_state
        PanelHearing.objects.filter(pk=hearing.pk).update(workflow_snapshot=snapshot)
        hearing.workflow_snapshot = snapshot

    active_phase = chamber_state.get('active_phase', 'presentation')
    phase_duration = chamber_state.get('phase_duration_seconds')
    if not phase_duration:
        phase_duration = int(custom_durations.get(active_phase, DEFAULT_PANEL_PHASE_DURATIONS.get(active_phase, 900)))
    elapsed_before_pause = chamber_state.get('elapsed_before_pause', 0)
    timer_paused = chamber_state.get('timer_paused', False)
    phase_started_at_str = chamber_state.get('phase_started_at')

    if timer_paused:
        phase_seconds_remaining = max(0, int(round(phase_duration - elapsed_before_pause)))
    elif phase_started_at_str:
        try:
            started_at = datetime.fromisoformat(phase_started_at_str)
            if timezone.is_naive(started_at):
                started_at = timezone.make_aware(started_at)
            now = timezone.now()
            current_elapsed = elapsed_before_pause + max(0, (now - started_at).total_seconds())
            phase_seconds_remaining = max(0, int(round(phase_duration - current_elapsed)))
        except Exception:
            phase_seconds_remaining = chamber_state.get('phase_seconds_remaining', 900)
    else:
        phase_seconds_remaining = chamber_state.get('phase_seconds_remaining', 900)

    # Transition to next step when countdown reaches zero
    if (hearing.status == PanelHearing.Status.OPEN
            and active_candidate
            and not timer_paused
            and phase_started_at_str
            and phase_seconds_remaining <= 0
            and active_phase in NEXT_PANEL_PHASES):
        next_phase = NEXT_PANEL_PHASES[active_phase]
        next_duration = int(custom_durations.get(next_phase, DEFAULT_PANEL_PHASE_DURATIONS.get(next_phase, 900)))
        now = timezone.now()
        chamber_state['active_phase'] = next_phase
        chamber_state['phase_duration_seconds'] = next_duration
        chamber_state['phase_seconds_remaining'] = next_duration
        chamber_state['phase_started_at'] = now.isoformat()
        chamber_state['elapsed_before_pause'] = 0
        snapshot = dict(hearing.workflow_snapshot or {})
        snapshot['chamber_state'] = chamber_state
        PanelHearing.objects.filter(pk=hearing.pk).update(workflow_snapshot=snapshot)
        hearing.workflow_snapshot = snapshot
        active_phase = next_phase
        phase_duration = next_duration
        phase_seconds_remaining = next_duration

    mins = max(0, phase_seconds_remaining) // 60
    secs = max(0, phase_seconds_remaining) % 60
    phase_timer_display = f'{mins}:{secs:02d}'

    display_mode = chamber_state.get('display_mode', 'in_session')

    conflicts_by_candidate = defaultdict(set)
    for inv_id, cand_id in PanelConflict.objects.filter(candidate__hearing=hearing, organization_id=org).values_list('invitation_id', 'candidate_id'):
        conflicts_by_candidate[cand_id].add(inv_id)

    scores_by_cand = defaultdict(lambda: defaultdict(list))
    for s in PanelScore.objects.filter(candidate__hearing=hearing, organization_id=org).select_related('invitation'):
        if s.invitation_id not in conflicts_by_candidate[s.candidate_id]:
            scores_by_cand[s.candidate_id][s.criterion_id].append(float(s.score))

    criteria = list(PanelCriterion.objects.filter(hearing=hearing, organization_id=org).order_by('order', 'pk'))
    invitations = list(PanelInvitation.objects.filter(hearing=hearing, organization_id=org, revoked_at__isnull=True).order_by('pk'))

    docket = []
    for cand in candidates:
        has_decision = cand.pk in decisions
        is_active = bool(active_candidate and cand.pk == active_candidate.pk)
        if is_active:
            status = 'active'
            status_label = 'On Floor'
        elif has_decision:
            status = 'completed'
            status_label = 'Completed'
        else:
            status = 'scheduled'
            status_label = 'Scheduled'
        docket.append({
            'candidate': cand,
            'status': status,
            'status_label': status_label,
            'is_active': is_active,
            'has_decision': has_decision,
            'decision': decisions.get(cand.pk),
        })

    examiners = []
    total_criteria = len(criteria)
    for inv in invitations:
        is_conflicted = bool(active_candidate and (inv.pk in conflicts_by_candidate[active_candidate.pk]))
        scored_criteria_count = 0
        if active_candidate and not is_conflicted:
            for crit in criteria:
                if PanelScore.objects.filter(candidate=active_candidate, criterion=crit, invitation=inv).exists():
                    scored_criteria_count += 1
        pct = int((scored_criteria_count / total_criteria * 100)) if total_criteria else 0
        is_done = bool(scored_criteria_count == total_criteria) if total_criteria else False
        examiners.append({
            'invitation': inv,
            'is_conflicted': is_conflicted,
            'scored_criteria_count': scored_criteria_count,
            'total_criteria': total_criteria,
            'pct': pct,
            'is_done': is_done,
        })

    convergence_criteria = []
    has_divergence = False
    if active_candidate:
        cand_scores = scores_by_cand[active_candidate.pk]
        for crit in criteria:
            vals = cand_scores.get(crit.pk, [])
            if vals:
                c_mean = round(sum(vals) / len(vals), 2)
                c_min = min(vals)
                c_max = max(vals)
                diff = round(c_max - c_min, 2)
                divergent = diff >= 1.5
                if divergent:
                    has_divergence = True
            else:
                c_mean = None
                c_min = None
                c_max = None
                diff = 0
                divergent = False
            convergence_criteria.append({
                'criterion': crit,
                'mean': c_mean,
                'min': c_min,
                'max': c_max,
                'diff': diff,
                'divergent': divergent,
                'count': len(vals),
            })

    reviews = []
    if active_candidate:
        reviews = list(PanelWrittenReview.objects.filter(hearing=hearing, candidate=active_candidate).select_related('invitation').order_by('created_at'))

    display_link = PanelLiveDisplayLink.objects.filter(hearing=hearing, organization_id=org, revoked_at__isnull=True).first()

    return {
        'hearing': hearing,
        'active_candidate': active_candidate,
        'active_phase': active_phase,
        'phase_duration': phase_duration,
        'phase_duration_seconds': phase_duration,
        'phase_seconds_remaining': phase_seconds_remaining,
        'phase_timer_display': phase_timer_display,
        'timer_paused': timer_paused,
        'display_mode': display_mode,
        'docket': docket,
        'examiners': examiners,
        'criteria': criteria,
        'convergence_criteria': convergence_criteria,
        'has_divergence': has_divergence,
        'reviews': reviews,
        'active_decision': decisions.get(active_candidate.pk) if active_candidate else None,
        'display_link': display_link,
        'outcomes': PanelDecision.Outcome.choices,
        'phase_durations_mins': phase_durations_mins,
    }


@transaction.atomic
def set_panel_chamber_active_candidate(hearing, candidate_id, *, organization, user):
    """Set active candidate on the floor and reset turn phase timer."""
    locked = PanelHearing.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=hearing.pk).first()
    if locked is None:
        raise PermissionDenied
    cand = PanelCandidate.objects.filter(hearing=locked, pk=candidate_id).first()
    if cand is None:
        raise ValidationError('Candidate not found in this hearing.')
    locked.active_candidate = cand
    now = timezone.now()
    snapshot = dict(locked.workflow_snapshot or {})
    chamber = dict(snapshot.get('chamber_state', {}))
    custom_durations = chamber.get('phase_durations') or {}
    pres_duration = int(custom_durations.get('presentation', 900))
    chamber['active_phase'] = 'presentation'
    chamber['phase_duration_seconds'] = pres_duration
    chamber['phase_seconds_remaining'] = pres_duration
    chamber['phase_started_at'] = now.isoformat()
    chamber['elapsed_before_pause'] = 0
    chamber['timer_paused'] = False
    chamber['timer_paused_at'] = None
    snapshot['chamber_state'] = chamber
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('active_candidate', 'workflow_snapshot', 'updated_at'))
    _record_panel_audit(locked, PanelAuditEvent.Action.CANDIDATE_ACTIVATED,
        f'Presenter activated on floor: {cand.display_name}.', candidate=cand, actor_user=user)
    return locked


@transaction.atomic
def set_panel_chamber_phase(hearing, phase, *, organization, user):
    """Transition active candidate phase: presentation, qa, deliberation, decision."""
    locked = PanelHearing.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=hearing.pk).first()
    if locked is None:
        raise PermissionDenied
    if phase not in DEFAULT_PANEL_PHASE_DURATIONS:
        raise ValidationError('Invalid chamber phase.')
    now = timezone.now()
    snapshot = dict(locked.workflow_snapshot or {})
    chamber = dict(snapshot.get('chamber_state', {}))
    custom_durations = chamber.get('phase_durations') or {}
    duration = int(custom_durations.get(phase, DEFAULT_PANEL_PHASE_DURATIONS[phase]))
    chamber['active_phase'] = phase
    chamber['phase_duration_seconds'] = duration
    chamber['phase_seconds_remaining'] = duration
    chamber['phase_started_at'] = now.isoformat()
    chamber['elapsed_before_pause'] = 0
    chamber['timer_paused'] = False
    chamber['timer_paused_at'] = None
    snapshot['chamber_state'] = chamber
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


@transaction.atomic
def set_panel_phase_durations(hearing, durations, *, organization, user):
    """Save custom durations for chamber steps."""
    locked = PanelHearing.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=hearing.pk).first()
    if locked is None:
        raise PermissionDenied
    snapshot = dict(locked.workflow_snapshot or {})
    chamber = dict(snapshot.get('chamber_state', {}))
    configured = dict(chamber.get('phase_durations') or {})
    for phase in ('presentation', 'qa', 'deliberation', 'decision'):
        if phase in durations:
            try:
                val = int(durations[phase])
                if 10 <= val <= 7200:
                    configured[phase] = val
            except (ValueError, TypeError):
                continue
    chamber['phase_durations'] = configured
    active_phase = chamber.get('active_phase', 'presentation')
    if active_phase in configured:
        new_dur = configured[active_phase]
        elapsed = chamber.get('elapsed_before_pause', 0)
        chamber['phase_duration_seconds'] = new_dur
        chamber['phase_seconds_remaining'] = max(0, new_dur - int(elapsed))
    snapshot['chamber_state'] = chamber
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


@transaction.atomic
def adjust_panel_chamber_timer(hearing, delta_seconds, *, organization, user):
    """Adjust remaining phase seconds."""
    locked = PanelHearing.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=hearing.pk).first()
    if locked is None:
        raise PermissionDenied
    snapshot = dict(locked.workflow_snapshot or {})
    chamber = dict(snapshot.get('chamber_state', {}))
    phase_duration = int(chamber.get('phase_duration_seconds', chamber.get('phase_seconds_remaining', 900)))
    chamber['phase_duration_seconds'] = max(0, phase_duration + int(delta_seconds))
    current_rem = int(chamber.get('phase_seconds_remaining', 900))
    chamber['phase_seconds_remaining'] = max(0, current_rem + int(delta_seconds))
    now = timezone.now()
    started_str = chamber.get('phase_started_at')
    if started_str and not chamber.get('timer_paused', False):
        try:
            started_at = datetime.fromisoformat(started_str)
            if timezone.is_naive(started_at):
                started_at = timezone.make_aware(started_at)
            elapsed = chamber.get('elapsed_before_pause', 0) + max(0, (now - started_at).total_seconds())
            if elapsed >= phase_duration:
                chamber['phase_started_at'] = now.isoformat()
                chamber['elapsed_before_pause'] = 0
                chamber['phase_duration_seconds'] = max(0, int(delta_seconds))
                chamber['phase_seconds_remaining'] = max(0, int(delta_seconds))
        except Exception:
            pass
    snapshot['chamber_state'] = chamber
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


@transaction.atomic
def toggle_panel_chamber_pause(hearing, *, organization, user):
    """Toggle timer pause state."""
    locked = PanelHearing.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=hearing.pk).first()
    if locked is None:
        raise PermissionDenied
    snapshot = dict(locked.workflow_snapshot or {})
    chamber = dict(snapshot.get('chamber_state', {}))
    was_paused = chamber.get('timer_paused', False)
    now = timezone.now()
    if not was_paused:
        started_str = chamber.get('phase_started_at')
        if started_str:
            try:
                started_at = datetime.fromisoformat(started_str)
                if timezone.is_naive(started_at):
                    started_at = timezone.make_aware(started_at)
                elapsed = max(0, (now - started_at).total_seconds())
                chamber['elapsed_before_pause'] = chamber.get('elapsed_before_pause', 0) + elapsed
            except Exception:
                pass
        chamber['timer_paused'] = True
        chamber['timer_paused_at'] = now.isoformat()
    else:
        chamber['phase_started_at'] = now.isoformat()
        chamber['timer_paused'] = False
        chamber['timer_paused_at'] = None
    snapshot['chamber_state'] = chamber
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


@transaction.atomic
def conclude_panel_chamber_candidate(hearing, candidate_id, *, organization, user):
    """Conclude current candidate and advance docket to next candidate."""
    locked = PanelHearing.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=hearing.pk).first()
    if locked is None:
        raise PermissionDenied
    candidates = list(PanelCandidate.objects.filter(hearing=locked).order_by('order', 'pk'))
    next_cand = None
    found_curr = False
    for cand in candidates:
        if found_curr:
            next_cand = cand
            break
        if cand.pk == int(candidate_id):
            found_curr = True
    locked.active_candidate = next_cand
    now = timezone.now()
    snapshot = dict(locked.workflow_snapshot or {})
    chamber = dict(snapshot.get('chamber_state', {}))
    custom_durations = chamber.get('phase_durations') or {}
    pres_duration = int(custom_durations.get('presentation', DEFAULT_PANEL_PHASE_DURATIONS.get('presentation', 900)))
    chamber['active_phase'] = 'presentation'
    chamber['phase_duration_seconds'] = pres_duration
    chamber['phase_seconds_remaining'] = pres_duration
    chamber['phase_started_at'] = now.isoformat()
    chamber['elapsed_before_pause'] = 0
    chamber['timer_paused'] = False
    chamber['timer_paused_at'] = None
    snapshot['chamber_state'] = chamber
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('active_candidate', 'workflow_snapshot', 'updated_at'))
    if next_cand:
        _record_panel_audit(locked, PanelAuditEvent.Action.CANDIDATE_ACTIVATED,
            f'Presenter activated on floor: {next_cand.display_name}.', candidate=next_cand, actor_user=user)
    return locked


@transaction.atomic
def toggle_panel_chamber_display_mode(hearing, mode, *, organization, user):
    """Set projector display mode: welcome, in_session, deliberation_private."""
    locked = PanelHearing.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=hearing.pk).first()
    if locked is None:
        raise PermissionDenied
    if mode not in ('welcome', 'in_session', 'deliberation_private'):
        raise ValidationError('Invalid display mode.')
    snapshot = dict(locked.workflow_snapshot or {})
    chamber = dict(snapshot.get('chamber_state', {}))
    chamber['display_mode'] = mode
    snapshot['chamber_state'] = chamber
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


def get_showcase_live_context(event):
    """Compile comprehensive live telemetry, attention radar, stage pitch, and floor controls."""
    org = event.organization_id
    exhibits = list(ShowcaseExhibit.objects.filter(event=event, organization_id=org)
                    .select_related('category').order_by('category__order', 'category__name', 'order', 'pk'))
    exhibit_ids = [e.pk for e in exhibits]

    ratings = {
        row['exhibit_id']: row for row in ShowcaseRating.objects.filter(
            event=event, exhibit_id__in=exhibit_ids
        ).values('exhibit_id').annotate(count=Count('pk'), mean=Avg('stars'))
    }
    assignments = {
        row['exhibit_id']: row for row in ShowcaseJudgeExhibitAssignment.objects.filter(
            event=event, exhibit_id__in=exhibit_ids, active=True
        ).exclude(conflict_declaration__isnull=False)
        .values('exhibit_id').annotate(assigned=Count('pk'), submitted=Count('evaluation', distinct=True))
    }
    scores = {
        row['assignment__exhibit_id']: row for row in ShowcaseJudgeEvaluation.objects.filter(
            assignment__event=event
        ).values('assignment__exhibit_id').annotate(count=Count('pk'), mean=Avg('score'))
    }

    total_ratings = sum(r.get('count', 0) for r in ratings.values())
    total_assigned_scorecards = sum(a.get('assigned', 0) for a in assignments.values())
    total_submitted_scorecards = sum(s.get('count', 0) for s in scores.values())
    avg_stars_all = None
    if total_ratings > 0:
        star_sum = sum((r.get('mean') or 0) * r.get('count', 0) for r in ratings.values())
        avg_stars_all = round(star_sum / total_ratings, 2)

    snapshot = event.workflow_snapshot or {}
    floor_state = snapshot.get('floor_state', {})
    visitor_voting_paused = floor_state.get('visitor_voting_paused', False)
    judge_scoring_paused = floor_state.get('judge_scoring_paused', False)
    projector_mode = floor_state.get('projector_mode', 'qr_welcome')
    broadcast_banner = floor_state.get('broadcast_banner', '')

    stage_state = snapshot.get('stage_state', {})
    active_exhibit_id = stage_state.get('active_exhibit_id')
    now_ts = timezone.now().timestamp()

    active_exhibit = None
    if active_exhibit_id:
        active_exhibit = next((e for e in exhibits if e.pk == int(active_exhibit_id)), None)
    presented_ids = {int(item) for item in stage_state.get('presented', [])}
    pitch = showcase_pitch_timer(stage_state if active_exhibit else {}, now_ts)
    pitch_seconds_remaining = pitch['remaining']
    timer_paused = pitch['state'] == 'paused'

    rows = []
    attention_count = 0
    categories = set()
    for ex in exhibits:
        if ex.category:
            categories.add(ex.category.name)
        aud = ratings.get(ex.pk, {})
        cov = assignments.get(ex.pk, {})
        sc = scores.get(ex.pk, {})

        aud_count = aud.get('count', 0)
        aud_mean = round(aud.get('mean'), 2) if aud.get('mean') is not None else None
        j_assigned = cov.get('assigned', 0)
        j_submitted = sc.get('count', 0)
        j_mean = round(sc.get('mean'), 1) if sc.get('mean') is not None else None

        missing_judge = (j_assigned == 0)
        pending_scores = (j_submitted < j_assigned)
        zero_ratings = (aud_count == 0)
        needs_attention = (missing_judge or zero_ratings or pending_scores)
        if needs_attention:
            attention_count += 1

        rows.append({
            'exhibit': ex,
            'audience_count': aud_count,
            'audience_mean': aud_mean,
            'judge_assigned_count': j_assigned,
            'judge_submitted_count': j_submitted,
            'judge_mean': j_mean,
            'missing_judge': missing_judge,
            'pending_scores': pending_scores,
            'zero_ratings': zero_ratings,
            'needs_attention': needs_attention,
            'is_stage_active': bool(active_exhibit and ex.pk == active_exhibit.pk),
            'stage_status': ('on_stage' if active_exhibit and ex.pk == active_exhibit.pk
                             else 'presented' if ex.pk in presented_ids else 'waiting'),
        })

    judges = list(ShowcaseJudgeInvitation.objects.filter(event=event, organization_id=org, revoked_at__isnull=True).order_by('email'))

    return {
        'event': event,
        'rows': rows,
        'categories': sorted(list(categories)),
        'total_ratings': total_ratings,
        'avg_stars_all': avg_stars_all,
        'total_assigned_scorecards': total_assigned_scorecards,
        'total_submitted_scorecards': total_submitted_scorecards,
        'pending_scorecards': max(0, total_assigned_scorecards - total_submitted_scorecards),
        'attention_count': attention_count,
        'visitor_voting_paused': visitor_voting_paused,
        'judge_scoring_paused': judge_scoring_paused,
        'projector_mode': projector_mode,
        'broadcast_banner': broadcast_banner,
        'active_exhibit': active_exhibit,
        'pitch_seconds_remaining': pitch_seconds_remaining,
        'timer_paused': timer_paused,
        'pitch_state': pitch['state'],
        'pitch_duration': pitch['duration'],
        'pitch_ends_at': pitch['ends_at'],
        'server_now': now_ts,
        'judges': judges,
        'awards_preview': showcase_awards(event, allow_preview=True),
    }


@transaction.atomic
def toggle_showcase_floor_visitor_voting(event, *, organization, user):
    """Toggle visitor rating acceptance globally."""
    locked = ShowcaseEvent.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=event.pk).first()
    if locked is None:
        raise PermissionDenied
    snapshot = dict(locked.workflow_snapshot or {})
    floor = dict(snapshot.get('floor_state', {}))
    floor['visitor_voting_paused'] = not floor.get('visitor_voting_paused', False)
    snapshot['floor_state'] = floor
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


@transaction.atomic
def toggle_showcase_floor_judge_scoring(event, *, organization, user):
    """Toggle judge scoring acceptance globally."""
    locked = ShowcaseEvent.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=event.pk).first()
    if locked is None:
        raise PermissionDenied
    snapshot = dict(locked.workflow_snapshot or {})
    floor = dict(snapshot.get('floor_state', {}))
    floor['judge_scoring_paused'] = not floor.get('judge_scoring_paused', False)
    snapshot['floor_state'] = floor
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


@transaction.atomic
def assign_showcase_roving_judge(event, exhibit_id, judge_invitation_id, *, organization, user):
    """Assign an invited judge to an exhibit dynamically during the live event."""
    locked = ShowcaseEvent.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=event.pk).first()
    if locked is None:
        raise PermissionDenied
    exhibit = ShowcaseExhibit.objects.filter(event=locked, pk=exhibit_id).first()
    invitation = ShowcaseJudgeInvitation.objects.filter(event=locked, pk=judge_invitation_id).first()
    if exhibit is None or invitation is None:
        raise ValidationError('Exhibit or Judge invitation not found.')
    assignment, created = ShowcaseJudgeExhibitAssignment.objects.get_or_create(
        event=locked,
        exhibit=exhibit,
        invitation=invitation,
        defaults={'organization_id': locked.organization_id, 'created_by': user, 'active': True}
    )
    if not created and not assignment.active:
        assignment.active = True
        assignment.save(update_fields=('active', 'updated_at'))
    return assignment


PITCH_SECONDS = 180


def showcase_pitch_timer(stage, now_ts):
    """Server-owned pitch clock: `state` is idle (no exhibit started), running,
    paused or finished; `remaining` is derived from the stored deadline, so a
    reload or a second screen always sees the same time.
    """
    duration = max(1, int(stage.get('timer_duration') or PITCH_SECONDS))
    ends_at = stage.get('timer_ends_at')
    if stage.get('timer_paused'):
        remaining = int(stage.get('paused_remaining_seconds', duration))
        state, ends_at = 'paused', None
    elif ends_at is None:
        remaining, state = duration, 'idle'
    else:
        remaining = math.ceil(ends_at - now_ts)
        state = 'running' if remaining > 0 else 'finished'
    return {'state': state, 'remaining': max(0, min(remaining, 24 * 3600)),
            'duration': duration, 'ends_at': ends_at if state == 'running' else None}


def _locked_stage_event(event, organization, user):
    locked = (ShowcaseEvent.objects.for_organization(organization).owned_by(user)
              .select_for_update().filter(pk=event.pk).first())
    if locked is None:
        raise PermissionDenied
    snapshot = dict(locked.workflow_snapshot or {})
    return locked, snapshot, dict(snapshot.get('stage_state', {}))


def _save_stage(locked, snapshot, stage):
    snapshot['stage_state'] = stage
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


def _presented_after(stage, next_exhibit_id):
    """Exhibits that have already had their turn on stage, in order."""
    presented = [int(item) for item in stage.get('presented', [])]
    previous = stage.get('active_exhibit_id')
    if previous and int(previous) != next_exhibit_id and int(previous) not in presented:
        presented.append(int(previous))
    return presented


def showcase_exhibit_on_stage(event, exhibit_id):
    """True only for the exhibit the organizer has explicitly put on stage."""
    active = ((event.workflow_snapshot or {}).get('stage_state') or {}).get('active_exhibit_id')
    return bool(active) and int(active) == int(exhibit_id)


@transaction.atomic
def stage_clear_showcase_exhibit(event, *, organization, user):
    """Take the current exhibit off stage and stop its pitch clock."""
    locked, snapshot, stage = _locked_stage_event(event, organization, user)
    if not stage.get('active_exhibit_id'):
        raise ValidationError('No exhibit is on stage.')
    stage['presented'] = _presented_after(stage, None)
    for key in ('active_exhibit_id', 'timer_ends_at', 'timer_duration', 'paused_remaining_seconds'):
        stage.pop(key, None)
    stage['timer_paused'] = False
    return _save_stage(locked, snapshot, stage)


@transaction.atomic
def stage_set_showcase_exhibit(event, exhibit_id, *, organization, user):
    """Put an exhibit on stage and start its pitch clock."""
    locked, snapshot, stage = _locked_stage_event(event, organization, user)
    exhibit = ShowcaseExhibit.objects.filter(event=locked, pk=exhibit_id).first()
    if exhibit is None:
        raise ValidationError('Exhibit not found.')
    stage['presented'] = _presented_after(stage, exhibit.pk)
    stage.update({
        'active_exhibit_id': exhibit.pk, 'timer_duration': PITCH_SECONDS,
        'timer_ends_at': timezone.now().timestamp() + PITCH_SECONDS,
        'paused_remaining_seconds': PITCH_SECONDS, 'timer_paused': False,
    })
    return _save_stage(locked, snapshot, stage)


@transaction.atomic
def stage_adjust_showcase_timer(event, delta_seconds, *, organization, user):
    """Add or remove pitch seconds; the clock must have been started."""
    locked, snapshot, stage = _locked_stage_event(event, organization, user)
    now_ts = timezone.now().timestamp()
    pitch = showcase_pitch_timer(stage, now_ts)
    if pitch['state'] == 'idle':
        raise ValidationError('Put an exhibit on stage to start the pitch clock first.')
    new_remaining = max(0, pitch['remaining'] + int(delta_seconds))
    stage['timer_duration'] = max(pitch['duration'], new_remaining)
    if pitch['state'] == 'paused':
        stage['paused_remaining_seconds'] = new_remaining
    else:
        stage['timer_ends_at'] = now_ts + new_remaining
    return _save_stage(locked, snapshot, stage)


@transaction.atomic
def stage_toggle_showcase_pause(event, *, organization, user):
    """Pause the running pitch clock, or resume it with the time that was left."""
    locked, snapshot, stage = _locked_stage_event(event, organization, user)
    now_ts = timezone.now().timestamp()
    pitch = showcase_pitch_timer(stage, now_ts)
    if pitch['state'] == 'idle':
        raise ValidationError('Put an exhibit on stage to start the pitch clock first.')
    if pitch['state'] == 'paused':
        stage['timer_ends_at'] = now_ts + pitch['remaining']
        stage['timer_paused'] = False
    else:
        stage['paused_remaining_seconds'] = pitch['remaining']
        stage['timer_paused'] = True
    return _save_stage(locked, snapshot, stage)


@transaction.atomic
def set_showcase_projector_mode(event, mode, *, organization, user):
    """Set venue projector display mode."""
    locked = ShowcaseEvent.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=event.pk).first()
    if locked is None:
        raise PermissionDenied
    snapshot = dict(locked.workflow_snapshot or {})
    floor = dict(snapshot.get('floor_state', {}))
    floor['projector_mode'] = mode
    snapshot['floor_state'] = floor
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


def get_or_create_competition_live_state(competition):
    """Retrieve or initialize live arena state for an active competition."""
    org_id = competition.organization_id
    live_state = CompetitionLiveState.objects.filter(competition=competition, organization_id=org_id).first()
    if live_state is None:
        latest_round = CompetitionRound.objects.filter(competition=competition, organization_id=org_id).order_by('-order').first()
        first_entry = None
        if latest_round:
            first_round_entry = CompetitionRoundEntry.objects.filter(round=latest_round, organization_id=org_id).select_related('entry').first()
            if first_round_entry:
                first_entry = first_round_entry.entry
        live_state = CompetitionLiveState.objects.create(
            organization_id=org_id,
            created_by=competition.created_by,
            competition=competition,
            active_round=latest_round,
            active_entry=first_entry,
            pitch_seconds_remaining=180,
            timer_paused=False,
            audience_voting_open=True,
            projector_mode='stage',
        )
    return live_state


def get_competition_live_state_context(competition):
    """Compile telemetry, stage pitch controls, judge matrix, audience meter, and round standings."""
    org_id = competition.organization_id
    live_state = get_or_create_competition_live_state(competition)
    rounds = list(CompetitionRound.objects.filter(competition=competition, organization_id=org_id).order_by('order'))
    active_round = live_state.active_round
    if not active_round and rounds:
        active_round = rounds[-1]
        live_state.active_round = active_round
        live_state.save(update_fields=('active_round', 'updated_at'))

    tracks = list(CompetitionTrack.objects.filter(competition=competition, organization_id=org_id).order_by('pk'))

    round_entries = []
    if active_round:
        round_entries = list(CompetitionRoundEntry.objects.filter(round=active_round, organization_id=org_id)
            .select_related('entry', 'track').order_by('entry__order', 'entry__pk'))

    active_entry = live_state.active_entry
    if active_entry and round_entries:
        if not any(re.entry_id == active_entry.pk for re in round_entries):
            active_entry = round_entries[0].entry
            live_state.active_entry = active_entry
            live_state.save(update_fields=('active_entry', 'updated_at'))
    elif not active_entry and round_entries:
        active_entry = round_entries[0].entry
        live_state.active_entry = active_entry
        live_state.save(update_fields=('active_entry', 'updated_at'))

    active_round_entry = None
    if active_entry and round_entries:
        active_round_entry = next((re for re in round_entries if re.entry_id == active_entry.pk), None)

    active_track = active_round_entry.track if active_round_entry else (tracks[0] if tracks else None)

    # Judge matrix for active round and active track
    judge_rows = []
    judge_scores_total = Decimal('0')
    judge_scores_count = 0
    if active_round and active_track and active_round_entry:
        invitations = list(CompetitionJudgeInvitation.objects.filter(
            round=active_round, track=active_track, organization_id=org_id, revoked_at__isnull=True
        ).order_by('email'))
        for inv in invitations:
            scores = list(CompetitionJudgeScore.objects.filter(
                invitation=inv, round_entry=active_round_entry, organization_id=org_id
            ))
            if scores:
                avg_val = Decimal(str(sum(s.score for s in scores) / len(scores))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                judge_scores_total += avg_val
                judge_scores_count += 1
                status = 'submitted'
                score_val = avg_val
            elif inv.submitted_at:
                score_val = None
                status = 'submitted'
            else:
                score_val = None
                status = 'awaiting'
            judge_rows.append({
                'invitation': inv,
                'email': inv.email,
                'status': status,
                'score': score_val,
            })

    avg_judge_score = (judge_scores_total / Decimal(judge_scores_count)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if judge_scores_count > 0 else None

    # Audience join code and ballots count
    join_code = None
    ballots_count = 0
    if active_round and active_track:
        join_code_obj = CompetitionAudienceJoinCode.objects.filter(round=active_round, track=active_track).first()
        if join_code_obj:
            join_code = join_code_obj.code
        else:
            try:
                join_code = competition_audience_join_code(competition, active_round, active_track)
            except Exception:
                join_code = 'JOIN123'
        ballots_count = CompetitionAudienceBallot.objects.filter(
            competition=competition, round=active_round, track=active_track
        ).count()

    # Round standings
    standings = []
    has_boundary_tie = False
    cutoff_rank = 3
    if round_entries:
        sorted_entries = sorted(
            round_entries,
            key=lambda re: (
                re.score is not None,
                re.score if re.score is not None else (re.judge_score if re.judge_score is not None else Decimal('-1'))
            ),
            reverse=True
        )
        for idx, re in enumerate(sorted_entries, start=1):
            effective_score = re.score if re.score is not None else re.judge_score
            is_qualified = (idx <= cutoff_rank)
            standings.append({
                'rank': idx,
                'round_entry': re,
                'entry': re.entry,
                'track': re.track,
                'judge_score': re.judge_score,
                'audience_score': re.audience_score,
                'score': re.score,
                'effective_score': effective_score,
                'is_qualified': is_qualified,
                'is_active': bool(active_entry and re.entry_id == active_entry.pk),
            })
        if len(standings) > cutoff_rank:
            boundary_score = standings[cutoff_rank - 1]['effective_score']
            next_score = standings[cutoff_rank]['effective_score']
            if boundary_score is not None and boundary_score == next_score:
                has_boundary_tie = True

    unresolved_ties = False
    if has_boundary_tie and active_round:
        tie_decisions = CompetitionLeaderboardTieDecision.objects.filter(
            round=active_round, organization_id=org_id
        ).exists()
        unresolved_ties = not tie_decisions

    can_advance = (
        active_round is not None
        and active_round.advancement_locked_at is None
        and not unresolved_ties
        and len(standings) > 0
    )

    return {
        'competition': competition,
        'live_state': live_state,
        'rounds': rounds,
        'active_round': active_round,
        'tracks': tracks,
        'active_track': active_track,
        'round_entries': round_entries,
        'active_entry': active_entry,
        'active_round_entry': active_round_entry,
        'pitch_seconds_remaining': live_state.pitch_seconds_remaining,
        'timer_paused': live_state.timer_paused,
        'audience_voting_open': live_state.audience_voting_open and (active_round.audience_locked_at is None if active_round else True),
        'join_code': join_code,
        'ballots_count': ballots_count,
        'judge_rows': judge_rows,
        'avg_judge_score': avg_judge_score,
        'standings': standings,
        'cutoff_rank': cutoff_rank,
        'has_boundary_tie': has_boundary_tie,
        'unresolved_ties': unresolved_ties,
        'can_advance': can_advance,
        'projector_mode': live_state.projector_mode,
        'broadcast_banner': live_state.broadcast_banner,
    }


@transaction.atomic
def set_competition_stage_entry(competition, entry_id, *, organization, user):
    """Set the active entrant presenting on the arena stage."""
    live_state = get_or_create_competition_live_state(competition)
    entry = CompetitionEntry.objects.filter(pk=entry_id, organization_id=competition.organization_id).first()
    if entry is None:
        raise ValidationError('Entrant not found.')
    live_state.active_entry = entry
    live_state.pitch_seconds_remaining = 180
    live_state.timer_paused = False
    live_state.save(update_fields=('active_entry', 'pitch_seconds_remaining', 'timer_paused', 'updated_at'))
    return live_state


@transaction.atomic
def adjust_competition_pitch_timer(competition, delta_seconds, *, organization, user):
    """Adjust pitch countdown seconds."""
    live_state = get_or_create_competition_live_state(competition)
    live_state.pitch_seconds_remaining = max(0, live_state.pitch_seconds_remaining + int(delta_seconds))
    live_state.save(update_fields=('pitch_seconds_remaining', 'updated_at'))
    return live_state


@transaction.atomic
def toggle_competition_pitch_pause(competition, *, organization, user):
    """Toggle pitch timer pause."""
    live_state = get_or_create_competition_live_state(competition)
    live_state.timer_paused = not live_state.timer_paused
    live_state.save(update_fields=('timer_paused', 'updated_at'))
    return live_state


@transaction.atomic
def toggle_competition_audience_voting(competition, *, organization, user):
    """Toggle audience voting open/locked state."""
    live_state = get_or_create_competition_live_state(competition)
    live_state.audience_voting_open = not live_state.audience_voting_open
    live_state.save(update_fields=('audience_voting_open', 'updated_at'))
    return live_state


@transaction.atomic
def set_competition_projector_mode(competition, mode, *, organization, user):
    """Set the arena projector display mode."""
    live_state = get_or_create_competition_live_state(competition)
    live_state.projector_mode = mode
    live_state.save(update_fields=('projector_mode', 'updated_at'))
    return live_state


def _calc_drill_seconds_remaining(snapshot, now=None):
    """Compute drill countdown remaining seconds against wall-clock time."""
    if now is None:
        now = timezone.now()
    phase_duration = int(snapshot.get('phase_duration_seconds', snapshot.get('drill_seconds_remaining', 900)))
    if snapshot.get('timer_paused', False):
        return max(0, int(snapshot.get('drill_seconds_remaining', phase_duration)))
    phase_started_at_str = snapshot.get('phase_started_at')
    elapsed_before_pause = float(snapshot.get('elapsed_before_pause', 0.0))
    if phase_started_at_str:
        try:
            started_at = datetime.fromisoformat(phase_started_at_str)
            if timezone.is_naive(started_at):
                started_at = timezone.make_aware(started_at)
            elapsed = elapsed_before_pause + max(0.0, (now - started_at).total_seconds())
            return max(0, int(round(phase_duration - elapsed)))
        except (ValueError, TypeError):
            pass
    return max(0, int(snapshot.get('drill_seconds_remaining', phase_duration)))


def training_drill_timer(snapshot, now=None):
    """Server-owned drill clock: the browser only counts down to `ends_at_ms`
    on the server's clock (`server_now_ms`), so every screen agrees."""
    now = now or timezone.now()
    seconds = _calc_drill_seconds_remaining(snapshot, now)
    paused = bool(snapshot.get('timer_paused', False))
    duration = max(1, int(snapshot.get('phase_duration_seconds', 900)))
    server_now_ms = int(now.timestamp() * 1000)
    return {
        'seconds': seconds,
        'paused': paused,
        'duration': max(duration, seconds),
        'expired': seconds == 0 and not paused,
        'display': f'{seconds // 60}:{seconds % 60:02d}',
        'percent': min(100, round(seconds * 100 / max(duration, seconds, 1))),
        'urgent': 0 < seconds <= 60,
        'ends_at_ms': None if paused else server_now_ms + seconds * 1000,
        'server_now_ms': server_now_ms,
    }


def get_training_session_live_state(series, session):
    """Compile workshop telemetry, drill phase pipeline, competencies, and pod trackers."""
    org_id = series.organization_id
    snapshot = dict(session.workflow_snapshot or {})
    drill_phase = snapshot.get('drill_phase', 'briefing')
    now = timezone.now()
    drill_seconds_remaining = _calc_drill_seconds_remaining(snapshot, now)
    timer_paused = bool(snapshot.get('timer_paused', False))
    visited_pods = set(snapshot.get('visited_pods', []))
    notes = dict(snapshot.get('notes', {}))

    participants = list(TrainingParticipant.objects.filter(
        series=series, organization_id=org_id
    ).order_by('group_name', 'order', 'pk'))

    students = list(session.assessment_session.students.all().order_by('full_name', 'pk'))
    student_by_name = {s.full_name.strip().casefold(): s for s in students}

    criteria = list(TrainingSessionCompetency.objects.filter(
        series_session=session, organization_id=org_id
    ).order_by('order', 'pk'))

    results = list(TrainingCompetencyResult.objects.filter(
        criterion__series_session=session, organization_id=org_id
    ).order_by('criterion_id', 'student_id', '-revision'))

    results_map = {}
    for r in results:
        results_map.setdefault((r.student_id, r.criterion_id), r)

    # Breakout pods grouping
    pods_dict = {}
    participant_rows = []
    for p in participants:
        student = student_by_name.get(p.display_name.strip().casefold())
        comp_scores = []
        for c in criteria:
            res = results_map.get((student.pk if student else None, c.pk))
            comp_scores.append({
                'criterion': c,
                'result': res,
                'score': res.score if res else None,
            })
        row_data = {
            'participant': p,
            'student': student,
            'note': notes.get(str(p.pk), ''),
            'competency_scores': comp_scores,
        }
        participant_rows.append(row_data)
        pods_dict.setdefault(p.group_name, []).append(row_data)

    pods = []
    for group_name, members in sorted(pods_dict.items()):
        pods.append({
            'name': group_name,
            'members': members,
            'is_visited': group_name in visited_pods,
        })

    # Attendee feedback
    feedback_rows = TrainingFeedback.objects.filter(series_session=session, organization_id=org_id)
    feedback_count = feedback_rows.count()
    mean_val = feedback_rows.aggregate(avg=Avg('score'))['avg']
    feedback_score_mean = round(mean_val, 1) if mean_val is not None else None

    # Pace distribution breakdown
    pace_high = feedback_rows.filter(score__gte=4).count()
    pace_mid = feedback_rows.filter(score=3).count()
    pace_low = feedback_rows.filter(score__lte=2).count()

    return {
        'series': series,
        'session': session,
        'assessment_session': session.assessment_session,
        'drill_phase': drill_phase,
        'drill_seconds_remaining': drill_seconds_remaining,
        'drill_timer': training_drill_timer(snapshot, now),
        'timer_paused': timer_paused,
        'visited_pods': list(visited_pods),
        'notes': notes,
        'participants': participant_rows,
        'pods': pods,
        'criteria': criteria,
        'feedback_count': feedback_count,
        'feedback_score_mean': feedback_score_mean,
        'pace_high': pace_high,
        'pace_mid': pace_mid,
        'pace_low': pace_low,
        'feedback_public_id': session.feedback_public_id,
    }


@transaction.atomic
def set_training_drill_phase(session, phase, *, organization, user):
    """Advance drill phase between briefing, simulation, and debrief."""
    locked = TrainingSeriesSession.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=session.pk).first()
    if locked is None:
        raise PermissionDenied
    if phase not in ('briefing', 'simulation', 'debrief'):
        raise ValidationError('Phase must be briefing, simulation, or debrief.')
    snapshot = dict(locked.workflow_snapshot or {})
    snapshot['drill_phase'] = phase
    now = timezone.now()
    if phase == 'simulation':
        phase_duration = 900
    elif phase == 'debrief':
        phase_duration = 600
    else:
        phase_duration = 300
    snapshot['phase_duration_seconds'] = phase_duration
    snapshot['drill_seconds_remaining'] = phase_duration
    snapshot['phase_started_at'] = now.isoformat()
    snapshot['elapsed_before_pause'] = 0.0
    snapshot['timer_paused'] = False
    snapshot['timer_paused_at'] = None
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


@transaction.atomic
def adjust_training_drill_timer(session, delta_seconds, *, organization, user):
    """Adjust countdown seconds for current drill phase."""
    locked = TrainingSeriesSession.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=session.pk).first()
    if locked is None:
        raise PermissionDenied
    snapshot = dict(locked.workflow_snapshot or {})
    now = timezone.now()
    sec = _calc_drill_seconds_remaining(snapshot, now)
    new_sec = max(0, sec + int(delta_seconds))
    snapshot['phase_duration_seconds'] = new_sec
    snapshot['drill_seconds_remaining'] = new_sec
    snapshot['phase_started_at'] = now.isoformat()
    snapshot['elapsed_before_pause'] = 0.0
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


@transaction.atomic
def toggle_training_drill_pause(session, *, organization, user):
    """Toggle timer pause for current drill."""
    locked = TrainingSeriesSession.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=session.pk).first()
    if locked is None:
        raise PermissionDenied
    snapshot = dict(locked.workflow_snapshot or {})
    now = timezone.now()
    is_paused = bool(snapshot.get('timer_paused', False))
    if is_paused:
        # Resume timer
        snapshot['timer_paused'] = False
        snapshot['timer_paused_at'] = None
        snapshot['phase_started_at'] = now.isoformat()
    else:
        # Pause timer
        sec = _calc_drill_seconds_remaining(snapshot, now)
        snapshot['timer_paused'] = True
        snapshot['timer_paused_at'] = now.isoformat()
        snapshot['phase_duration_seconds'] = sec
        snapshot['drill_seconds_remaining'] = sec
        snapshot['elapsed_before_pause'] = 0.0
        snapshot['phase_started_at'] = now.isoformat()
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


@transaction.atomic
def mark_training_pod_visited(session, pod_name, *, organization, user):
    """Toggle coach check-in state on a breakout team pod."""
    locked = TrainingSeriesSession.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=session.pk).first()
    if locked is None:
        raise PermissionDenied
    snapshot = dict(locked.workflow_snapshot or {})
    visited = set(snapshot.get('visited_pods', []))
    if pod_name in visited:
        visited.remove(pod_name)
    else:
        visited.add(pod_name)
    snapshot['visited_pods'] = list(visited)
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked


@transaction.atomic
def save_training_observation_note(session, key, note, *, organization, user):
    """Save qualitative coach observation note."""
    locked = TrainingSeriesSession.objects.for_organization(organization).owned_by(user).select_for_update().filter(pk=session.pk).first()
    if locked is None:
        raise PermissionDenied
    snapshot = dict(locked.workflow_snapshot or {})
    notes = dict(snapshot.get('notes', {}))
    notes[str(key)] = str(note).strip()
    snapshot['notes'] = notes
    locked.workflow_snapshot = snapshot
    locked.save(update_fields=('workflow_snapshot', 'updated_at'))
    return locked




