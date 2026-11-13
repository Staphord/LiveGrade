"""Asynchronous Panel invitation delivery; bearer values stay out of task payloads."""
import secrets
from email.utils import formataddr, parseaddr

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse
from django.template.loader import render_to_string
from django.utils import timezone

from .models import CoAssessmentExaminer, PanelInvitation, PanelHearing


@shared_task
def send_panel_invitation_email(invitation_id, public_base_url=''):
    """Generate a capability in the worker, send it, and persist only its digest."""
    with transaction.atomic():
        invitation = (PanelInvitation.objects.select_for_update()
            .select_related('hearing').filter(pk=invitation_id).first())
        if invitation is None or invitation.delivery_status != PanelInvitation.DeliveryStatus.PENDING:
            return 'ignored'
        if (invitation.revoked_at or invitation.declined_at or invitation.accepted_at
                or invitation.expires_at <= timezone.now()
                or invitation.hearing.status not in (PanelHearing.Status.DRAFT, PanelHearing.Status.OPEN)):
            invitation.delivery_status = PanelInvitation.DeliveryStatus.FAILED
            invitation.delivery_error = 'invitation_unusable'
            invitation.save(update_fields=('delivery_status', 'delivery_error', 'updated_at'))
            return 'unusable'
        token = secrets.token_urlsafe(32)
        invitation.token_digest = PanelInvitation.digest_token(token)
        invitation.delivery_status = PanelInvitation.DeliveryStatus.SENDING
        invitation.delivery_attempts += 1
        attempt_number = invitation.delivery_attempts
        invitation.delivery_error = ''
        invitation.save(update_fields=('token_digest', 'delivery_status', 'delivery_attempts',
                                       'delivery_error', 'updated_at'))
        email = invitation.email
        hearing_name = invitation.hearing.name
        expiry = invitation.expires_at

    path = reverse('panel_examiner', kwargs={'token': token})
    configured_base = settings.LIVEGRADE_PUBLIC_BASE_URL or public_base_url or settings.LIVEGRADE_BASE_URL
    invitation_url = f'{configured_base.rstrip("/")}{path}'
    _display_name, sender_address = parseaddr(settings.DEFAULT_FROM_EMAIL)
    from_email = formataddr(('LiveGrade', sender_address or settings.DEFAULT_FROM_EMAIL))
    expiry_label = timezone.localtime(expiry).strftime('%d %b %Y at %H:%M %Z')
    email_context = {'hearing_name': hearing_name, 'invitation_url': invitation_url,
                     'expiry_label': expiry_label}
    try:
        sent = send_mail(
            subject=f'LiveGrade Panel invitation: {hearing_name}',
            message=render_to_string('emails/panel_examiner_invitation.txt', email_context),
            from_email=from_email,
            recipient_list=[email],
            fail_silently=False,
            html_message=render_to_string('emails/panel_examiner_invitation.html', email_context),
        )
        error = '' if sent == 1 else 'mail_not_accepted'
    except Exception:
        error = 'mail_backend_error'

    with transaction.atomic():
        current = PanelInvitation.objects.select_for_update().filter(pk=invitation_id).first()
        if (current is None or current.delivery_status != PanelInvitation.DeliveryStatus.SENDING
                or current.delivery_attempts != attempt_number):
            return 'superseded'
        if error:
            current.delivery_status = PanelInvitation.DeliveryStatus.FAILED
            current.delivery_error = error
            current.save(update_fields=('delivery_status', 'delivery_error', 'updated_at'))
            return 'failed'
        current.delivery_status = PanelInvitation.DeliveryStatus.SENT
        current.sent_at = timezone.now()
        current.delivery_error = ''
        current.save(update_fields=('delivery_status', 'sent_at', 'delivery_error', 'updated_at'))
    return 'sent'


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_coassessment_examiner_invitation_email(self, examiner_id, public_base_url=''):
    """Email a co-assessment examiner their private scoring link."""
    examiner = (CoAssessmentExaminer.objects.select_related('session')
                .filter(pk=examiner_id).first())
    if examiner is None or not examiner.invite_token:
        return 'ignored'
    path = reverse('coassessment_examiner_portal', kwargs={'token': examiner.invite_token})
    configured_base = settings.LIVEGRADE_PUBLIC_BASE_URL or public_base_url or settings.LIVEGRADE_BASE_URL
    _display_name, sender_address = parseaddr(settings.DEFAULT_FROM_EMAIL)
    from_email = formataddr(('LiveGrade', sender_address or settings.DEFAULT_FROM_EMAIL))
    context = {
        'session_name': examiner.session.name,
        'examiner_name': examiner.display_name,
        'is_chair': examiner.is_chair,
        'invitation_url': f'{configured_base.rstrip("/")}{path}',
    }
    try:
        sent = send_mail(
            subject=f'LiveGrade Co-Assessment invitation: {examiner.session.name}',
            message=render_to_string('emails/coassessment_examiner_invitation.txt', context),
            from_email=from_email,
            recipient_list=[examiner.email],
            fail_silently=False,
            html_message=render_to_string('emails/coassessment_examiner_invitation.html', context),
        )
    except Exception as exc:
        raise self.retry(exc=exc)
    return 'sent' if sent == 1 else 'mail_not_accepted'
