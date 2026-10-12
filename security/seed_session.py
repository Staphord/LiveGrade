"""Seeds an EPHEMERAL database for security testing and prints NAME=value lines.

Creates two lecturers in two different organizations, each with a live session. Org A's
session is full of XSS payloads in every free-text field a user can type or import
(session, student, programme, group, topic, location, rubric category), so the probe and
ZAP have stored-XSS canaries on every page that renders them.

The sessions are the way the test suite builds them (accounts.testing); the OIDC login
cannot be driven by ZAP. Never run this against a real database: it creates users.

Usage:  python security/seed_session.py >> "$GITHUB_ENV"     (or eval in security/local_dast.sh)
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.test import Client  # noqa: E402
from django.utils import timezone  # noqa: E402

from accounts.testing import make_org, make_user, sign_in  # noqa: E402
from assessments.models import (  # noqa: E402
    AssessmentSession, GroupMembership, PresentationGroup, PresentationTurn,
    RubricCategory, Student)

# Distinct numbers so probe.py can tell "payload came back raw" from the app's own markup.
XSS = {
    'script': '"><script>alert(9001)</script>',
    'img': '<img src=x onerror=alert(9002)>',
    'attr': "' autofocus onfocus=alert(9003) x='",
    'breakout': '</script><script>alert(9004)</script>',
    'tmpl': '{{7*7}}{%25 debug %25}${7*7}',
    'formula': '=HYPERLINK("http://evil.example/?x","click")',
}


def build(label, org, user, payloads):
    p = payloads
    session = AssessmentSession.objects.create(
        organization_id=org.pk, created_by=user,
        name=f"{label} session {p.get('script', '')}"[:150],
        status=AssessmentSession.Status.LIVE)
    for i, (name, scope) in enumerate((
            (f"Delivery {p.get('img', '')}", RubricCategory.Scope.GROUP),
            (f"Teamwork {p.get('attr', '')}", RubricCategory.Scope.INDIVIDUAL))):
        RubricCategory.objects.create(
            assessment_session=session, name=name[:150], scope=scope, weight=50,
            description=p.get('script', '')[:255], order=i)
    presenters = PresentationGroup.objects.create(
        assessment_session=session, name=f"Presenters {p.get('script', '')}"[:100],
        topic=p.get('img', '')[:200], location=p.get('breakout', '')[:120], order=1)
    evaluators = PresentationGroup.objects.create(
        assessment_session=session, name='Evaluators', order=2)
    members = []
    for i, key in enumerate(('script', 'img', 'attr', 'breakout', 'formula')):
        student = Student.objects.create(
            assessment_session=session, full_name=f"Pat{i} {p.get(key, '')}"[:150],
            student_id=f'ZAP{i}', email=f'pat{i}@example.com',
            programme=p.get(key, '')[:120])
        GroupMembership.objects.create(group=presenters, student=student)
        members.append(student)
    grader = Student.objects.create(
        assessment_session=session, full_name='GraderPlain', student_id='ZAPG',
        email='grader@example.com')
    GroupMembership.objects.create(group=evaluators, student=grader)
    now = timezone.now()
    PresentationTurn.objects.create(
        assessment_session=session, group=presenters, status=PresentationTurn.Status.ACTIVE,
        opened_at=now, voting_opened_at=now, voting_ends_at=now + timedelta(hours=4),
        presentation_ends_at=now + timedelta(hours=4))
    return session, presenters, members[0], grader


out = {}
for label, name, payloads in (('A', 'Scan Org A', XSS), ('B', 'Scan Org B', {})):
    org = make_org(name)
    user = make_user(f'zap-{label.lower()}', first_name=label)
    client = Client()
    sign_in(client, user, org)
    session, group, student, grader = build(label, org, user, payloads)
    out.update({
        f'ORG_{label}_COOKIE': client.cookies[settings.SESSION_COOKIE_NAME].value,
        f'ORG_{label}_SESSION_PK': session.pk,
        f'ORG_{label}_SESSION_UUID': session.uuid,
        f'ORG_{label}_GROUP_PK': group.pk,
        f'ORG_{label}_STUDENT_PK': student.pk,
        f'ORG_{label}_GRADER_NAME': grader.full_name,
    })
for key, value in out.items():
    print(f'{key}={value}')
