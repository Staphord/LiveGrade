import csv
import io
import json
import re
import uuid
import zipfile
from datetime import timedelta
from collections import Counter
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection
from django.http import Http404
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (AssessmentSession, GroupMembership, PresentationGroup,
                                SessionTemplate, Student, TemplateRubricCategory)
from service_domains.models import (TrainingCompetency, TrainingCompetencyResult,
                                    TrainingFeedback, TrainingLearnerReportLink,
                                    TrainingParticipant, TrainingSeries, TrainingSeriesSession,
                                    TrainingSessionCompetency, TrainingCertificate)
from service_domains.team_forms import CreateTrainingSessionForm
from service_domains.operations import record_training_competency_result, issue_training_certificates
from service_domains.operations import (training_series_competency_trends,
                                        training_series_competency_export)
from service_domains.views import training_series_list


class TrainingSeriesWorkspaceTests(TestCase):
    def setUp(self):
        self.org = make_org('Training workspace')
        self.owner = make_user('training-workspace-owner')
        self.other = make_user('training-workspace-other')

    def request(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'), \
                patch('accounts.context.is_released', side_effect=lambda slug: slug == 'team'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def test_series_manage_forms_have_unique_control_ids(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Accessible series')

        response = self.request('get', reverse('training_series_manage', args=(series.pk,)))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        ids = re.findall(r'\bid="([^"]+)"', html)
        label_targets = re.findall(r'<label[^>]*\bfor="([^"]+)"', html)
        duplicates = [element_id for element_id, count in Counter(ids).items() if count > 1]
        self.assertEqual(duplicates, [])
        self.assertEqual(html.count('id="series_details_name"'), 1)
        self.assertEqual(html.count('id="participant_display_name"'), 1)
        self.assertEqual(html.count('id="competency_name"'), 1)
        self.assertEqual(html.count('id="launch_name"'), 1)
        for expected_id in ('series_details_name', 'participant_display_name',
                'competency_name', 'launch_name'):
            with self.subTest(label_target=expected_id):
                self.assertIn(expected_id, ids)
                self.assertIn(expected_id, label_targets)

    def test_invalid_series_forms_keep_control_ids_unique(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Accessible series')
        url = reverse('training_series_manage', args=(series.pk,))
        invalid_forms = (
            ({'action': 'series_details', 'name': '', 'description': '', 'cadence_note': ''},
             'series_details_name'),
            ({'action': 'participant', 'display_name': '', 'reference': '', 'group_name': ''},
             'participant_display_name'),
            ({'action': 'competency', 'name': '', 'description': '', 'passing_score': ''},
             'competency_name'),
            ({'action': 'create_session', 'name': '', 'scheduled_for': '', 'template': '',
              'identify_by': ''}, 'launch_name'),
        )

        for payload, expected_id in invalid_forms:
            with self.subTest(action=payload['action']):
                response = self.request('post', url, payload)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                ids = re.findall(r'\bid="([^"]+)"', html)
                label_targets = re.findall(r'<label[^>]*\bfor="([^"]+)"', html)
                duplicates = [element_id for element_id, count in Counter(ids).items()
                    if count > 1]
                self.assertEqual(duplicates, [])
                self.assertIn(expected_id, ids)
                self.assertEqual(html.count(f'id="{expected_id}"'), 1)
                self.assertIn(expected_id, label_targets)

    def test_qualifying_closed_demo_issues_verifiable_current_certificate(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Leadership practice')
        session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Final demo',
            identify_by=AssessmentSession.IdentifyBy.STUDENT_ID,
            status=AssessmentSession.Status.CLOSED)
        student = Student.objects.create(assessment_session=session, full_name='Amina Juma',
            student_id='L-42', email='private@example.org')
        link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, assessment_session=session, sequence=1)
        criterion = TrainingSessionCompetency.objects.create(
            organization_id=self.org.id, created_by=self.owner,
            series_session=link, name='Clear communication', passing_score=70)
        result = record_training_competency_result(session, criterion, student, 85,
            'Provided a clear and accurate demonstration.',
            organization=self.org.as_claim(), user=self.owner)
        session.status = AssessmentSession.Status.DRAFT
        session.save(update_fields=('status',))
        outcomes_url = reverse('training_session_competencies', kwargs={'pk': session.pk})
        rejected_issue = self.request('post', outcomes_url, {'action': 'issue_certificates'})
        self.assertEqual(rejected_issue.status_code, 200)
        self.assertContains(rejected_issue, 'Close the Team demo')
        with patch('service_domains.views.issue_training_certificates',
                   side_effect=PermissionDenied):
            denied_issue = self.request('post', outcomes_url, {'action': 'issue_certificates'})
        self.assertEqual(denied_issue.status_code, 404)
        with self.assertRaisesMessage(ValidationError, 'Close the Team demo'):
            issue_training_certificates(session,
                organization=self.org.as_claim(), user=self.owner)
        session.status = AssessmentSession.Status.CLOSED
        session.save(update_fields=('status',))
        issued = issue_training_certificates(session,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(issued), 1)
        certificate = issued[0]
        self.assertEqual(certificate.result_id, result.pk)
        self.assertEqual(certificate.learner_reference, 'L-42')
        self.assertEqual(issue_training_certificates(session,
            organization=self.org.as_claim(), user=self.owner), [certificate])

        issue_response = self.request('post', outcomes_url, {'action': 'issue_certificates'})
        self.assertEqual(issue_response.status_code, 302)
        self.assertEqual(TrainingCertificate.objects.count(), 1)
        self.assertContains(self.request('get', outcomes_url), 'View certificate verification')
        public_url = reverse('training_certificate_verify', kwargs={'public_id': certificate.public_id})
        verification = self.request('get', public_url)
        self.assertContains(verification, 'matches the learner’s latest competency decision')
        self.assertContains(verification, 'Amina Juma')
        self.assertContains(verification, 'Clear communication')
        self.assertNotContains(verification, 'private@example.org')
        self.assertNotContains(verification, 'Provided a clear and accurate demonstration')
        with patch('service_domains.views.TrainingCompetencyResult.objects.filter') as latest_query:
            latest_query.return_value.order_by.return_value.values_list.return_value.first.return_value = None
            orphaned_verification = self.request('get', public_url)
        self.assertEqual(orphaned_verification.status_code, 200)
        self.assertContains(orphaned_verification, 'Verified, but superseded')
        self.assertContains(orphaned_verification, 'A later decision is now on record')
        missing_url = reverse('training_certificate_verify', kwargs={'public_id': uuid.UUID(int=0)})
        self.assertEqual(self.request('get', missing_url).status_code, 404)
        from service_domains.views import training_certificate_verify
        wrong_service_request = RequestFactory().get(public_url)
        wrong_service_request.service = 'competition'
        with self.assertRaises(Http404):
            training_certificate_verify(wrong_service_request, certificate.public_id)

        correction = record_training_competency_result(session, criterion, student, 60,
            'The reassessment did not meet the competency threshold.',
            organization=self.org.as_claim(), user=self.owner,
            correction_reason='A scoring error was found in the original decision.')
        self.assertEqual(issue_training_certificates(session,
            organization=self.org.as_claim(), user=self.owner), [])
        superseded = self.request('get', public_url)
        self.assertContains(superseded, 'Verified, but superseded')
        self.assertContains(superseded, 'A later decision is now on record')
        self.assertNotContains(superseded, '60.00')
        self.assertNotContains(superseded, 'passing score 70.00')
        self.assertNotContains(superseded, correction.evidence)

        restored = record_training_competency_result(session, criterion, student, 90,
            'The learner met the standard in the later reassessment.',
            organization=self.org.as_claim(), user=self.owner,
            correction_reason='The assessor verified the updated submission.')
        replacement = issue_training_certificates(session,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(replacement), 1)
        self.assertEqual(replacement[0].result_id, restored.pk)
        self.assertNotEqual(replacement[0].public_id, certificate.public_id)

    def test_certificate_record_validation_and_owner_scope_fail_closed(self):
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Practice series')
        open_session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Open demo',
            identify_by=AssessmentSession.IdentifyBy.STUDENT_ID)
        student = Student.objects.create(assessment_session=open_session,
            full_name='Learner', student_id='L-1')
        link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, assessment_session=open_session, sequence=1)
        criterion = TrainingSessionCompetency.objects.create(
            organization_id=self.org.id, created_by=self.owner, series_session=link,
            name='Demonstration', passing_score=70)
        result = record_training_competency_result(open_session, criterion, student, 80,
            'The learner met the evidence standard.', organization=self.org.as_claim(), user=self.owner)
        with self.assertRaises(PermissionDenied):
            issue_training_certificates(open_session,
                organization=self.org.as_claim(), user=self.other)
        orphan = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Unlinked demo',
            identify_by=AssessmentSession.IdentifyBy.STUDENT_ID,
            status=AssessmentSession.Status.CLOSED)
        with self.assertRaises(PermissionDenied):
            issue_training_certificates(orphan,
                organization=self.org.as_claim(), user=self.owner)

        candidate = TrainingCertificate(result=result, learner_name=student.full_name,
            learner_reference=student.student_id, series_name=series.name,
            session_name=open_session.name, competency_name=criterion.name,
            score=result.score, passing_score=criterion.passing_score,
            issued_by=self.owner, organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            candidate.full_clean()
        self.assertIn('Certificates can be issued after the Team demo closes',
            str(caught.exception))
        open_session.status = AssessmentSession.Status.CLOSED
        open_session.save(update_fields=('status',))

        wrong_org = TrainingCertificate(result=result, learner_name=student.full_name,
            learner_reference=student.student_id, series_name=series.name,
            session_name=open_session.name, competency_name=criterion.name,
            score=result.score, passing_score=criterion.passing_score,
            issued_by=self.owner, organization_id=self.org.id + 1, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            wrong_org.full_clean()
        self.assertIn('same organization', str(caught.exception))

        wrong_owner = TrainingCertificate(result=result, learner_name=student.full_name,
            learner_reference=student.student_id, series_name=series.name,
            session_name=open_session.name, competency_name=criterion.name,
            score=result.score, passing_score=criterion.passing_score,
            issued_by=self.owner, organization_id=self.org.id, created_by=self.other)
        with self.assertRaises(ValidationError) as caught:
            wrong_owner.full_clean()
        self.assertIn('same owner', str(caught.exception))

        wrong_snapshot = TrainingCertificate(result=result, learner_name='Changed learner',
            learner_reference=student.student_id, series_name=series.name,
            session_name=open_session.name, competency_name=criterion.name,
            score=result.score, passing_score=criterion.passing_score,
            issued_by=self.owner, organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            wrong_snapshot.full_clean()
        self.assertIn('snapshot the linked passing result', str(caught.exception))

        failed = record_training_competency_result(open_session, criterion, student, 50,
            'The learner has not met the standard.', organization=self.org.as_claim(),
            user=self.owner, correction_reason='The first decision was recorded in error.')
        not_qualified = TrainingCertificate(result=failed, learner_name=student.full_name,
            learner_reference=student.student_id, series_name=series.name,
            session_name=open_session.name, competency_name=criterion.name,
            score=failed.score, passing_score=criterion.passing_score,
            issued_by=self.owner, organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError) as caught:
            not_qualified.full_clean()
        self.assertIn('requires a passing competency result', str(caught.exception))

        empty = TrainingCertificate(organization_id=self.org.id, created_by=self.owner)
        with self.assertRaises(ValidationError):
            empty.full_clean()
        valid = TrainingCertificate(result=result, learner_name=student.full_name,
            learner_reference=student.student_id, series_name=series.name,
            session_name=open_session.name, competency_name=criterion.name,
            score=result.score, passing_score=criterion.passing_score,
            issued_by=self.owner, organization_id=self.org.id, created_by=self.owner)
        valid.save(force_insert=True)
        valid.learner_name = 'Mutated'
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            valid.save()
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            valid.delete()

        racer = Student.objects.create(assessment_session=open_session,
            full_name='Second learner', student_id='L-2')
        record_training_competency_result(open_session, criterion, racer, 90,
            'The second learner met the evidence standard.',
            organization=self.org.as_claim(), user=self.owner)

        original_full_clean = TrainingCertificate.full_clean

        def simultaneous_issue(certificate, *args, **kwargs):
            original_full_clean(certificate, *args, **kwargs)
            TrainingCertificate.objects.bulk_create([certificate])

        with patch.object(TrainingCertificate, 'save', autospec=True,
                          side_effect=IntegrityError('Simulated one-to-one issue race.')):
            with patch.object(TrainingCertificate, 'full_clean', autospec=True,
                              side_effect=simultaneous_issue):
                raced = issue_training_certificates(open_session,
                    organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(raced), 1)
        self.assertEqual(raced[0].result.student_id, racer.pk)
        self.assertTrue(TrainingCertificate.objects.filter(result=raced[0].result).exists())

        third = Student.objects.create(assessment_session=open_session,
            full_name='Third learner', student_id='L-3')
        record_training_competency_result(open_session, criterion, third, 88,
            'The third learner met the evidence standard.',
            organization=self.org.as_claim(), user=self.owner)
        with patch.object(TrainingCertificate, 'save', autospec=True,
                          side_effect=IntegrityError('Simulated missing race row.')):
            with self.assertRaises(IntegrityError):
                issue_training_certificates(open_session,
                    organization=self.org.as_claim(), user=self.owner)

    def test_owner_can_manage_series_and_create_next_session_with_reused_roster_and_rubric(self):
        sign_in(self.client, self.owner, self.org)
        response = self.request('post', reverse('training_series_create'), {
            'name': 'Product demos', 'description': 'Weekly team practice',
        })
        series = TrainingSeries.objects.get(name='Product demos')
        self.assertEqual(response.status_code, 302)
        detail = reverse('training_series_manage', args=(series.pk,))
        self.assertEqual(response['Location'], detail)

        self.request('post', detail, {
            'action': 'participant', 'display_name': 'Amina',
            'reference': 'A-17', 'group_name': 'Team North',
        })
        self.request('post', detail, {
            'action': 'participant', 'display_name': 'Juma',
            'reference': 'J-11', 'group_name': 'Team North',
        })
        self.request('post', detail, {
            'action': 'competency', 'name': 'Evidence',
            'description': 'Explain the demo evidence.', 'passing_score': '70',
        })
        template = SessionTemplate.objects.create(
            organization_id=self.org.id, created_by=self.owner, service='team',
            name='Team demo rubric', identify_by=AssessmentSession.IdentifyBy.FULL_NAME)
        TemplateRubricCategory.objects.create(template=template, name='Clarity',
            description='Explain clearly', scope='group', weight=100, order=0)
        created = self.request('post', detail, {
            'action': 'create_session', 'name': 'Week 1 demo', 'template': f'saved:{template.pk}',
        })
        self.assertEqual(created.status_code, 302)
        session = AssessmentSession.objects.get(name='Week 1 demo', service='team')
        self.assertEqual(created['Location'], f'/team/app/{session.pk}/')
        console = self.request('get', created['Location'])
        self.assertEqual(console.status_code, 200)
        self.assertContains(console, 'Week 1 demo')
        self.assertContains(console, f'/team/app/{session.pk}/groups/')
        self.assertContains(console, f'/team/app/{session.pk}/rubric/')
        classroom_path = f'/classroom/app/{session.pk}/'
        self.assertEqual(self.request('get', classroom_path).status_code, 404)
        self.assertEqual(list(session.students.values_list('full_name', flat=True)), ['Amina', 'Juma'])
        self.assertEqual(session.groups.get().name, 'Team North')
        self.assertEqual(list(session.rubric_categories.values_list('name', flat=True)), ['Clarity'])
        self.assertEqual(TrainingSeriesSession.objects.get(assessment_session=session).sequence, 1)
        self.assertEqual(TrainingParticipant.objects.filter(series=series).count(), 2)
        self.assertEqual(TrainingCompetency.objects.filter(series=series).count(), 1)
        snapshot = TrainingSessionCompetency.objects.get(series_session__assessment_session=session)
        self.assertEqual((snapshot.name, snapshot.passing_score), ('Evidence', 70))
        source = TrainingCompetency.objects.get(series=series, name='Evidence')
        source.passing_score = 95
        source.save(update_fields=('passing_score', 'updated_at'))
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.passing_score, 70)
        outcomes_url = reverse('training_session_competencies', kwargs={'pk': session.pk})
        outcomes_page = self.request('get', outcomes_url)
        self.assertEqual(outcomes_page.status_code, 200)
        self.assertContains(outcomes_page, 'Amina')
        self.assertContains(outcomes_page, 'Evidence')
        invalid_score = self.request('post', outcomes_url, {
            'criterion_id': str(snapshot.pk), 'student_id': str(session.students.first().pk),
            'score': '101', 'evidence': 'Outside the score range.',
        })
        self.assertEqual(invalid_score.status_code, 400)
        self.assertContains(invalid_score, 'Ensure this value is less than or equal to 100', status_code=400)
        foreign_criterion = self.request('post', outcomes_url, {
            'criterion_id': '999999', 'student_id': str(session.students.first().pk),
            'score': '80', 'evidence': 'Should not reach another criterion.',
        })
        self.assertEqual(foreign_criterion.status_code, 404)
        first_student = session.students.get(full_name='Amina')
        first_decision = self.request('post', outcomes_url, {
            'criterion_id': str(snapshot.pk), 'student_id': str(first_student.pk),
            'score': '85', 'evidence': 'Explained the supporting field evidence.',
        })
        self.assertEqual(first_decision.status_code, 302)
        passing = TrainingCompetencyResult.objects.get(criterion=snapshot, student=first_student)
        self.assertTrue(passing.passed)
        self.assertEqual(passing.assessed_by, self.owner)

        missing_reason = self.request('post', outcomes_url, {
            'criterion_id': str(snapshot.pk), 'student_id': str(first_student.pk),
            'score': '65', 'evidence': 'Reassessment did not meet the evidence standard.',
            'correction_reason': '',
        })
        self.assertEqual(missing_reason.status_code, 400)
        self.assertContains(missing_reason, 'A reason is required to correct', status_code=400)
        self.assertEqual(missing_reason.content.decode().count(
            'A reason is required to correct the previous competency decision.'), 1)
        self.assertEqual(TrainingCompetencyResult.objects.filter(criterion=snapshot,
            student=first_student).count(), 1)

        corrected = self.request('post', outcomes_url, {
            'criterion_id': str(snapshot.pk), 'student_id': str(first_student.pk),
            'score': '65', 'evidence': 'Reassessment did not meet the evidence standard.',
            'correction_reason': 'The original evidence was attributed to the wrong demo.',
        })
        self.assertEqual(corrected.status_code, 302)
        revisions = list(TrainingCompetencyResult.objects.filter(
            criterion=snapshot, student=first_student).order_by('revision'))
        self.assertEqual([item.revision for item in revisions], [1, 2])
        self.assertEqual(revisions[1].supersedes, revisions[0])
        self.assertFalse(revisions[1].passed)
        self.assertContains(self.request('get', outcomes_url), 'Not yet passed')
        with patch('service_domains.views.record_training_competency_result', side_effect=PermissionDenied):
            denied_decision = self.request('post', outcomes_url, {
                'criterion_id': str(snapshot.pk), 'student_id': str(session.students.get(full_name='Juma').pk),
                'score': '80', 'evidence': 'This decision must be refused.',
            })
        self.assertEqual(denied_decision.status_code, 404)
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            revisions[0].save()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            revisions[0].delete()
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            TrainingCompetencyResult.objects.filter(pk=revisions[0].pk).update(score=99)
        with self.assertRaisesMessage(ValidationError, 'append-only'):
            TrainingCompetencyResult.objects.filter(pk=revisions[0].pk).delete()
        snapshot.name = 'Changed criterion'
        with self.assertRaisesMessage(ValidationError, 'immutable snapshots'):
            snapshot.save()

        sign_in(self.client, self.other, self.org)
        self.assertEqual(self.request('get', outcomes_url).status_code, 404)
        outside_org = make_org('Outside training workspace')
        sign_in(self.client, self.owner, outside_org)
        self.assertEqual(self.request('get', outcomes_url).status_code, 404)

        sign_in(self.client, self.owner, self.org)
        series_page = self.request('get', detail)
        self.assertContains(series_page, 'Week 1 demo')
        self.assertContains(series_page, '/team/app/')

    def test_series_trend_uses_latest_revisions_and_renders_three_source_sessions(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Quarterly practice')
        competency = TrainingCompetency.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, name='Evidence', passing_score=70)
        incomplete_competency = TrainingCompetency.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, name='One-off', passing_score=70)
        for sequence, score in enumerate((55, 70, 85), start=1):
            session = AssessmentSession.objects.create(organization_id=self.org.id,
                created_by=self.owner, service='team', name=f'Demo {sequence}',
                identify_by=AssessmentSession.IdentifyBy.STUDENT_ID)
            student = Student.objects.create(assessment_session=session,
                full_name='Amina', student_id='A-17')
            name_only_student = Student.objects.create(assessment_session=session,
                full_name='Batu', student_id='')
            link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
                created_by=self.owner, series=series, assessment_session=session, sequence=sequence)
            criterion = TrainingSessionCompetency.objects.create(organization_id=self.org.id,
                created_by=self.owner, series_session=link, source_competency=competency,
                name='Evidence', passing_score=70)
            decision = TrainingCompetencyResult(organization_id=self.org.id,
                created_by=self.owner, criterion=criterion, student=student,
                score=score, evidence=f'Demo evidence {sequence}', assessed_by=self.owner)
            decision.full_clean()
            decision.save()
            name_result = TrainingCompetencyResult(organization_id=self.org.id,
                created_by=self.owner, criterion=criterion, student=name_only_student,
                score=score + 1, evidence=f'Name-matched evidence {sequence}', assessed_by=self.owner)
            name_result.full_clean()
            name_result.save()
            if sequence == 3:
                correction = TrainingCompetencyResult(organization_id=self.org.id,
                    created_by=self.owner, criterion=criterion, student=student,
                    score=90, evidence='Corrected demo evidence', assessed_by=self.owner,
                    correction_reason='Reassessed against the rubric', revision=2, supersedes=decision)
                correction.full_clean()
                correction.save()
            if sequence == 1:
                one_off = TrainingSessionCompetency.objects.create(organization_id=self.org.id,
                    created_by=self.owner, series_session=link, source_competency=incomplete_competency,
                    name='One-off', passing_score=70)
                single = TrainingCompetencyResult(organization_id=self.org.id,
                    created_by=self.owner, criterion=one_off, student=student,
                    score=60, evidence='Only one demo', assessed_by=self.owner)
                single.full_clean()
                single.save()
                blank_student = Student.objects.create(assessment_session=session, full_name='', student_id='')
                blank = TrainingCompetencyResult(organization_id=self.org.id,
                    created_by=self.owner, criterion=criterion, student=blank_student,
                    score=50, evidence='No stable identity', assessed_by=self.owner)
                blank.full_clean()
                blank.save()
            if sequence == 2:
                for _ in range(2):
                    duplicate = Student.objects.create(assessment_session=session,
                        full_name='Duplicate Name', student_id='')
                    ambiguous = TrainingCompetencyResult(organization_id=self.org.id,
                        created_by=self.owner, criterion=criterion, student=duplicate,
                        score=50, evidence='Ambiguous full name', assessed_by=self.owner)
                    ambiguous.full_clean()
                    ambiguous.save()

        trends = training_series_competency_trends(series,
            organization=self.org.as_claim(), user=self.owner)
        learner_trends = trends['learners']
        self.assertEqual([trend['learner'] for trend in learner_trends], ['Amina', 'Batu'])
        self.assertEqual(learner_trends[0]['sessions'], [
            ('Demo 1', 55, False), ('Demo 2', 70, True), ('Demo 3', 90, True)])
        self.assertEqual(learner_trends[0]['change'], 35)
        response = self.request('get', reverse('training_series_trends', args=(series.pk,)))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Amina')
        self.assertContains(response, 'Demo 3 score: 90 out of 100')
        self.assertContains(response, 'Change +35.0')
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])

    def test_team_trends_show_group_mean_and_member_count_across_three_demos(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Team presentation practice')
        competency = TrainingCompetency.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, name='Evidence', passing_score=70)
        score_pairs = ((50, 70), (70, 90), (90, 100))
        for sequence, scores in enumerate(score_pairs, start=1):
            session = AssessmentSession.objects.create(organization_id=self.org.id,
                created_by=self.owner, service='team', name=f'Team demo {sequence}',
                identify_by=AssessmentSession.IdentifyBy.STUDENT_ID)
            group = PresentationGroup.objects.create(assessment_session=session,
                name=('Blue team' if sequence != 2 else 'BLUE TEAM'))
            PresentationGroup.objects.create(assessment_session=session, name='   ')
            green = PresentationGroup.objects.create(assessment_session=session, name='Green team')
            if sequence == 2:
                PresentationGroup.objects.create(assessment_session=session, name=' green team ')
            link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
                created_by=self.owner, series=series, assessment_session=session,
                sequence=sequence)
            criterion = TrainingSessionCompetency.objects.create(
                organization_id=self.org.id, created_by=self.owner, series_session=link,
                source_competency=competency, name='Evidence', passing_score=70)
            for index, score in enumerate(scores, start=1):
                student = Student.objects.create(assessment_session=session,
                    full_name=f'Learner {index}', student_id=f'L-{index}')
                GroupMembership.objects.create(group=group, student=student)
                result = TrainingCompetencyResult(organization_id=self.org.id,
                    created_by=self.owner, criterion=criterion, student=student,
                    score=score, evidence='Demonstrated the competency.', assessed_by=self.owner)
                result.full_clean()
                result.save()
            green_student = Student.objects.create(assessment_session=session,
                full_name='Green learner', student_id='G-1')
            GroupMembership.objects.create(group=green, student=green_student)
            green_result = TrainingCompetencyResult(organization_id=self.org.id,
                created_by=self.owner, criterion=criterion, student=green_student,
                score=75, evidence='Demonstrated the competency.', assessed_by=self.owner)
            green_result.full_clean()
            green_result.save()

        trends = training_series_competency_trends(series,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(trends['teams']), 1)
        team_trend = trends['teams'][0]
        self.assertEqual(team_trend['team'], 'Blue team')
        self.assertEqual(team_trend['competency'], 'Evidence')
        self.assertEqual(team_trend['sessions'], [
            ('Team demo 1', 60, 2), ('Team demo 2', 80, 2), ('Team demo 3', 95, 2)])
        self.assertEqual(team_trend['change'], 35)
        response = self.request('get', reverse('training_series_trends', args=(series.pk,)))
        self.assertContains(response, 'Team competency progress')
        self.assertContains(response, 'Team demo 2 team average: 80 out of 100')
        self.assertContains(response, 'Change +35.0')

    def test_series_trends_need_three_scored_demos_and_fail_closed_on_wrong_owner(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='New series')
        self.assertEqual(training_series_competency_trends(series,
            organization=self.org.as_claim(), user=self.owner), {'learners': [], 'teams': []})
        response = self.request('get', reverse('training_series_trends', args=(series.pk,)))
        self.assertContains(response, 'at least three demos')
        with self.assertRaises(PermissionDenied):
            training_series_competency_trends(series,
                organization=self.org.as_claim(), user=self.other)

    def test_trend_view_converts_lost_scope_to_not_found(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Restricted series')
        with patch('service_domains.views.training_series_competency_trends',
                   side_effect=PermissionDenied):
            response = self.request('get', reverse('training_series_trends', args=(series.pk,)))
        self.assertEqual(response.status_code, 404)

    def test_hr_csv_exports_latest_scoped_outcomes_and_blocks_spreadsheet_formulas(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Leadership programme')
        competency = TrainingCompetency.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, name='Clear communication', passing_score=70)
        session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Demo 1',
            identify_by=AssessmentSession.IdentifyBy.FULL_NAME)
        student = Student.objects.create(assessment_session=session,
            full_name='=HYPERLINK("https://evil.example")', student_id='+1+1',
            email='private@example.test')
        link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, assessment_session=session, sequence=1)
        snapshot = TrainingSessionCompetency.objects.create(organization_id=self.org.id,
            created_by=self.owner, series_session=link, source_competency=competency,
            name=competency.name, passing_score=competency.passing_score)
        original = TrainingCompetencyResult(organization_id=self.org.id, created_by=self.owner,
            criterion=snapshot, student=student, score=55, evidence='old private evidence',
            assessed_by=self.owner)
        original.full_clean()
        original.save()
        correction = TrainingCompetencyResult(organization_id=self.org.id, created_by=self.owner,
            criterion=snapshot, student=student, score=85, evidence='new private evidence',
            assessed_by=self.owner, revision=2, supersedes=original,
            correction_reason='Corrected after reassessment')
        correction.full_clean()
        correction.save()

        rows = training_series_competency_export(series,
            organization=self.org.as_claim(), user=self.owner)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['score'], rows[0]['revision'], rows[0]['passed']), (85, 2, True))
        response = self.request('get', reverse('training_series_export', args=(series.pk,)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertIn('private, no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        table = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(table[1][3:9], ["'+1+1", "'=HYPERLINK(\"https://evil.example\")",
            'Clear communication', '85.00', '70.00', 'passed'])
        self.assertEqual(table[1][9], '2')
        self.assertNotIn('evidence', response.content.decode().lower())
        self.assertNotIn('private@example.test', response.content.decode())
        self.assertNotIn('old private evidence', response.content.decode())

    def test_hr_csv_scope_is_owner_org_and_team_service_only(self):
        sign_in(self.client, self.other, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Private programme')
        url = reverse('training_series_export', args=(series.pk,))
        self.assertEqual(self.request('get', url).status_code, 404)
        sign_in(self.client, self.owner, make_org('Different export organization'))
        self.assertEqual(self.request('get', url).status_code, 404)
        sign_in(self.client, self.owner, self.org)
        self.assertEqual(self.client.get('/classroom/series/1/outcomes.csv').status_code, 404)
        with self.assertRaises(PermissionDenied):
            training_series_competency_export(series,
                organization=self.org.as_claim(), user=self.other)

    def test_hr_export_for_empty_series_is_valid_header_only_csv(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Empty programme')
        response = self.request('get', reverse('training_series_export', args=(series.pk,)))
        self.assertEqual(response.status_code, 200)
        table = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(len(table), 1)
        self.assertEqual(table[0][0:3], ['series', 'demo_sequence', 'demo_session'])

    def test_hr_export_omits_inconsistent_student_session_and_hides_lost_scope(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Scoped programme')
        session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Valid demo')
        link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, assessment_session=session, sequence=1)
        criterion = TrainingSessionCompetency.objects.create(organization_id=self.org.id,
            created_by=self.owner, series_session=link, name='Accuracy', passing_score=70)
        foreign_session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Unlinked demo')
        foreign_student = Student.objects.create(assessment_session=foreign_session,
            full_name='Unlinked Learner')
        # Simulate legacy/corrupt data bypassing model validation and prove the
        # export independently checks the result's learner/session relation.
        TrainingCompetencyResult.objects.bulk_create([TrainingCompetencyResult(
            organization_id=self.org.id, created_by=self.owner, criterion=criterion,
            student=foreign_student, score=90, evidence='Must not export')])
        self.assertEqual(training_series_competency_export(series,
            organization=self.org.as_claim(), user=self.owner), [])
        with patch('service_domains.views.training_series_competency_export',
                   side_effect=PermissionDenied):
            response = self.request('get', reverse('training_series_export', args=(series.pk,)))
        self.assertEqual(response.status_code, 404)

    def test_series_dashboard_is_read_only_and_creation_validates(self):
        sign_in(self.client, self.owner, self.org)
        page = self.request('get', reverse('training_series_list'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Team and Training dashboard')
        self.assertContains(page, 'Getting started')
        self.assertContains(page, 'Set up your first Team and Training series')
        self.assertContains(page, 'Largest team rosters')
        self.assertNotContains(page, 'Purpose or intended skill goal')
        self.assertNotContains(page, 'Longitudinal Competency Growth')
        self.assertEqual(self.request('post', reverse('training_series_list')).status_code, 405)
        TrainingSeries.objects.create(organization_id=self.org.id, created_by=self.owner,
            name='Existing series')
        existing = self.request('get', reverse('training_series_list'))
        self.assertNotContains(existing, 'Your workflow')
        self.assertNotContains(existing, 'Your Team and Training workspace')
        self.assertNotContains(existing, 'Set up your first Team and Training series')
        self.assertContains(existing, 'Existing series')
        invalid = self.request('post', reverse('training_series_create'), {'name': '', 'description': ''})
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, 'This field is required.')

    def test_series_purpose_and_cadence_can_be_updated(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='New supervisors')
        url = reverse('training_series_manage', args=(series.pk,))
        response = self.request('post', url, {
            'action': 'series_details', 'name': 'Supervisor practice',
            'description': 'Give useful feedback using observed evidence.',
            'cadence_note': 'monthly on the first Friday',
        })
        self.assertEqual(response.status_code, 302)
        series.refresh_from_db()
        self.assertEqual(series.name, 'Supervisor practice')
        self.assertEqual(series.cadence_note, 'monthly on the first Friday')
        page = self.request('get', url)
        self.assertContains(page, 'Give useful feedback using observed evidence.')
        self.assertContains(page, 'monthly on the first Friday')

    def test_new_series_explains_how_to_create_its_first_team_template(self):
        sign_in(self.client, self.owner, self.org)
        created = self.request('post', reverse('training_series_create'), {
            'name': 'New staff induction', 'description': '',
        }, follow=True)
        self.assertEqual(created.status_code, 200)
        self.assertContains(created, 'Training series created.')
        series = TrainingSeries.objects.get(name='New staff induction')
        detail = created
        self.assertContains(detail, 'built-in or saved Team rubric')
        self.assertContains(detail, 'Import a roster')
        self.assertContains(detail, 'Create demo session')
        self.assertNotContains(detail, 'Create your first Team session setup')
        setup = self.request('get', '/team/app/new/')
        self.assertEqual(setup.status_code, 200)
        self.assertEqual(setup.context['form'].instance.service, 'team')
        self.assertContains(setup, 'New Team demo setup')
        self.assertContains(setup, 'Create a reusable Team demo setup')
        created_setup = self.request('post', '/team/app/new/', {
            'name': 'Team template source', 'identify_by': 'full_name',
            'ungraded_penalty': '1',
        })
        self.assertEqual(created_setup.status_code, 302)
        team_session = AssessmentSession.objects.get(name='Team template source')
        self.assertEqual(team_session.service, 'team')
        workspace = self.request('get', created_setup['Location'])
        self.assertContains(workspace, 'This session can become your Team template')
        self.assertContains(workspace, 'Save as template')
        self.assertNotContains(workspace, 'Competency outcomes')

    def test_team_starter_creates_a_rostered_demo_without_a_setup_session(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Pitch cohort')
        TrainingParticipant.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, display_name='Amina Yusuf',
            reference='EMP-001', group_name='Orchid')
        TrainingCompetency.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, name='Evidence', passing_score=70)
        response = self.request('post', reverse('training_series_manage', args=(series.pk,)), {
            'action': 'create_session', 'name': 'Week one pitch',
            'template': 'starter:pitch-practice', 'identify_by': 'student_id',
            'scheduled_for': '2026-11-02T09:30',
        })
        self.assertEqual(response.status_code, 302)
        session = AssessmentSession.objects.get(name='Week one pitch')
        self.assertEqual(session.identify_by, AssessmentSession.IdentifyBy.STUDENT_ID)
        self.assertEqual(list(session.rubric_categories.values_list('name', flat=True)), [
            'Value and audience', 'Evidence and feasibility', 'Communication and responses'])
        self.assertEqual(session.students.get().student_id, 'EMP-001')
        self.assertEqual(list(session.groups.values_list('name', flat=True)), ['Orchid'])
        link = TrainingSeriesSession.objects.get(assessment_session=session)
        self.assertEqual(link.scheduled_for.isoformat(), '2026-11-02T09:30:00+00:00')
        self.assertEqual(link.competency_criteria.get().name, 'Evidence')

    def test_workspace_denies_foreign_owner_and_reports_empty_roster_or_invalid_action(self):
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Private series')
        detail = reverse('training_series_manage', args=(series.pk,))
        sign_in(self.client, self.other, self.org)
        self.assertEqual(self.request('get', detail).status_code, 404)

        sign_in(self.client, self.owner, self.org)
        invalid = self.request('post', detail, {
            'action': 'create_session', 'name': 'Week 1', 'template': '',
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, 'This field is required.')
        unknown = self.request('post', detail, {'action': 'unknown'})
        self.assertEqual(unknown.status_code, 404)

    def test_template_queryset_is_limited_to_team_owner_and_organization(self):
        sign_in(self.client, self.owner, self.org)
        response = self.request('post', reverse('training_series_create'), {'name': 'Team', 'description': ''})
        series = TrainingSeries.objects.get(name='Team')
        detail = reverse('training_series_manage', args=(series.pk,))
        template = SessionTemplate.objects.create(organization_id=self.org.id,
            created_by=self.other, service='team', name='Other owner template')
        rejected = self.request('post', detail, {
            'action': 'create_session', 'name': 'Week 1', 'template': f'saved:{template.pk}',
        })
        self.assertEqual(rejected.status_code, 200)
        self.assertFalse(AssessmentSession.objects.filter(name='Week 1').exists())


class TrainingSeriesDashboardAndListTests(TestCase):
    def setUp(self):
        self.org = make_org('Training series UI')
        self.other_org = make_org('Other training series UI')
        self.owner = make_user('training-series-ui-owner')
        self.other = make_user('training-series-ui-other')
        sign_in(self.client, self.owner, self.org)

    def request(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'), \
                patch('accounts.context.is_released', side_effect=lambda slug: slug == 'team'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def create_series(self, name, *, owner=None, org=None, active=True):
        return TrainingSeries.objects.create(
            organization_id=(org or self.org).id, created_by=owner or self.owner,
            name=name, active=active)

    def test_dashboard_uses_workspace_totals_and_has_no_inline_create_form(self):
        first = self.create_series('Newest series')
        older = self.create_series('Older series')
        TrainingParticipant.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=first, display_name='Amina', group_name='Blue')
        TrainingCompetency.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=first, name='Communication', passing_score=70)
        session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='First demo')
        TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=first, assessment_session=session, sequence=1)
        self.create_series('Other owner series', owner=self.other)
        self.create_series('Other organization series', org=self.other_org)
        self.create_series('Inactive series', active=False)

        response = self.request('get', reverse('training_series_list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['dashboard_stats']['total_series'], 2)
        self.assertEqual(response.context['dashboard_stats']['total_participants'], 1)
        self.assertEqual(response.context['dashboard_stats']['total_competencies'], 1)
        self.assertEqual(response.context['dashboard_stats']['total_demos'], 1)
        self.assertEqual([item.pk for item in response.context['recent_series']],
            [older.pk, first.pk])
        self.assertContains(response, 'Team and Training dashboard')
        self.assertContains(response, 'Largest team rosters')
        self.assertContains(response, 'Series status')
        self.assertNotContains(response, 'Longitudinal Competency Growth')
        self.assertNotContains(response, 'Competency Pillar Overview')
        self.assertNotContains(response, 'Getting started')
        self.assertNotContains(response, 'name="name"')
        self.assertNotContains(response, 'Inactive series')

    def test_dashboard_empty_state_contains_onboarding_and_create_action(self):
        response = self.request('get', reverse('training_series_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'You haven’t created a training series yet')
        self.assertContains(response, 'Create a series')
        self.assertContains(response, 'Name the series and add its purpose.')
        self.assertNotContains(response, 'Inactive series')

    def test_series_create_get_is_read_only_and_valid_post_enters_setup(self):
        create_url = reverse('training_series_create')

        page = self.request('get', create_url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(TrainingSeries.objects.count(), 0)
        self.assertContains(page, 'Series details')
        self.assertContains(page, 'data-panel-steps')
        self.assertContains(page, 'disabled')
        self.assertNotContains(page, 'Your series has been created')

        invalid = self.request('post', create_url, {'name': '', 'description': ''})
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(TrainingSeries.objects.count(), 0)
        self.assertContains(invalid, 'This field is required.')

        created = self.request('post', create_url, {
            'action': 'series_details', 'name': 'New staff training',
            'description': 'Practice clear feedback.', 'cadence_note': 'Monthly',
        })
        self.assertEqual(created.status_code, 302)
        series = TrainingSeries.objects.get(name='New staff training')
        self.assertEqual(created['Location'], reverse('training_series_manage', args=(series.pk,)))
        self.assertEqual(list(self.client.session.get('_messages', [])), [])

    def test_table_search_pagination_and_ajax_fragment(self):
        for index in range(26):
            self.create_series(f'Training {index:02}')
        url = reverse('training_series_table')

        first_page = self.request('get', url)
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.context['paginator'].per_page, 25)
        self.assertEqual(first_page.context['paginator'].count, 26)
        self.assertContains(first_page, 'Page 1 of 2')
        self.assertContains(first_page, 'Rows per page')
        self.assertNotContains(first_page, 'Inactive')

        invalid_size = self.request('get', url, {'per_page': '10', 'page': 'x'})
        self.assertEqual(invalid_size.context['per_page'], 25)
        self.assertEqual(invalid_size.context['page_obj'].number, 1)
        last_page = self.request('get', url, {'page': '999'})
        self.assertEqual(last_page.context['page_obj'].number, 2)
        first_page_again = self.request('get', url, {'page': '0'})
        # The shared assessment paginator clamps out-of-range pages to the last page.
        self.assertEqual(first_page_again.context['page_obj'].number, 2)
        bad_size = self.request('get', url, {'per_page': 'abc'})
        self.assertEqual(bad_size.context['per_page'], 25)
        unknown_status = self.request('get', url, {'status': 'unknown'})
        self.assertEqual(unknown_status.context['filtered_count'], 26)
        for page_size in (25, 50, 100):
            with self.subTest(page_size=page_size):
                page = self.request('get', url, {'per_page': str(page_size)})
                self.assertEqual(page.context['per_page'], page_size)

        searched = self.request('get', url, {'q': 'Training 25'})
        self.assertEqual(searched.context['filtered_count'], 1)
        self.assertContains(searched, 'Training 25')
        self.assertNotContains(searched, 'Training 24')
        too_long = self.request('get', url, {'q': 'x' * 101})
        self.assertEqual(len(too_long.context['search_query']), 100)
        max_length = self.request('get', url, {'q': 'x' * 100})
        self.assertEqual(len(max_length.context['search_query']), 100)

        ajax = self.request('get', url, {'q': 'Training 25'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(ajax.status_code, 200)
        self.assertIn('X-Requested-With', ajax['Vary'])
        self.assertNotContains(ajax, '<html')
        self.assertContains(ajax, 'team-results')
        self.assertContains(first_page, 'aria-label="Select all training series on this page"')
        self.assertContains(first_page, 'aria-label="Actions for Training 25"')
        self.assertContains(first_page, 'data-team-delete-trigger')
        self.assertContains(first_page, 'Download export')
        self.assertContains(first_page, 'I have saved this export')
        self.assertNotContains(first_page, 'data-confirm=')

    def test_table_has_three_distinct_empty_states_and_sidebar_routes(self):
        empty = self.request('get', reverse('training_series_table'))
        self.assertContains(empty, 'No training series yet')
        self.assertContains(empty, 'Create a series')

        self.create_series('Existing series')
        filtered = self.request('get', reverse('training_series_table'), {'q': 'missing'})
        self.assertContains(filtered, 'No matching series')
        self.assertContains(filtered, 'Try a different series name')

        dashboard = self.request('get', reverse('training_series_list'))
        listing = self.request('get', reverse('training_series_table'))
        self.assertEqual(dashboard.context['current_service'].slug, 'team')
        self.assertEqual(dashboard.wsgi_request.resolver_match.url_name, 'training_series_list')
        self.assertContains(dashboard, 'aria-current="page"')
        self.assertContains(listing, 'Training series')
        self.assertNotContains(listing, 'Team and Training dashboard" class="nav-link active')

    def test_table_pagination_boundary_at_25_and_26(self):
        url = reverse('training_series_table')
        for index in range(25):
            self.create_series(f'Boundary {index:02}')
        page_25 = self.request('get', url)
        self.assertEqual(page_25.context['paginator'].num_pages, 1)
        self.assertNotContains(page_25, 'Page 1 of 1')
        self.create_series('Boundary 25')
        page_26 = self.request('get', url)
        self.assertEqual(page_26.context['paginator'].num_pages, 2)
        self.assertContains(page_26, 'Page 1 of 2')

    def test_delete_export_contains_protected_records_and_removes_linked_session_tree(self):
        series = self.create_series('Series with protected records')
        series.feedback_minimum_responses = 2
        series.save(update_fields=('feedback_minimum_responses', 'updated_at'))
        session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Closed demo',
            status=AssessmentSession.Status.CLOSED)
        student = Student.objects.create(assessment_session=session,
            full_name='Amina Juma', student_id='TEAM-42', email='learner@example.test')
        link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, assessment_session=session, sequence=1)
        criterion = TrainingSessionCompetency.objects.create(
            organization_id=self.org.id, created_by=self.owner, series_session=link,
            name='Clear communication', passing_score=70)
        result = record_training_competency_result(session, criterion, student, 80,
            'Used specific evidence to explain the outcome.',
            organization=self.org.as_claim(), user=self.owner)
        correction = record_training_competency_result(session, criterion, student, 85,
            'Followed up with a stronger demonstration.',
            organization=self.org.as_claim(), user=self.owner,
            correction_reason='A second assessor reviewed the evidence.')
        TrainingCertificate.objects.create(organization_id=self.org.id,
            created_by=self.owner, result=result, learner_name='Amina Juma',
            learner_reference='TEAM-42', series_name=series.name, session_name=session.name,
            competency_name=criterion.name, score=80, passing_score=70)
        TrainingLearnerReportLink.objects.create(organization_id=self.org.id,
            created_by=self.owner, series_session=link, student=student,
            token_digest='a' * 64, expires_at=timezone.now() + timedelta(days=1),
            snapshot={'summary': 'private learner report'})
        TrainingFeedback.objects.create(organization_id=self.org.id,
            created_by=None, series_session=link, device_digest='b' * 64, score=5)

        export = self.request('get', reverse('training_series_delete_export', args=(series.pk,)))
        self.assertEqual(export.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(export.content))
        data = json.loads(archive.read('team-series-export.json'))
        self.assertEqual(len(data['learner_reports']), 1)
        self.assertEqual(data['anonymous_feedback'][0]['score'], 5)
        self.assertEqual(len(data['competency_results']), 2)
        self.assertEqual(len(data['certificates']), 1)
        self.assertNotIn('token_digest', json.dumps(data))
        self.assertNotIn('device_digest', json.dumps(data))
        self.assertIn('assessments.student', {row['model'] for row in data['linked_assessment_records']})
        self.assertIn('Used specific evidence', json.dumps(data))

        removed = self.request('post', reverse('training_series_delete', args=(series.pk,)), {
            'delete_export_token': export['X-Delete-Export-Token'],
        })
        self.assertEqual(removed.status_code, 302)
        self.assertFalse(TrainingSeries.objects.filter(pk=series.pk).exists())
        self.assertFalse(AssessmentSession.objects.filter(pk=session.pk).exists())
        self.assertFalse(TrainingLearnerReportLink.objects.filter(pk=data['learner_reports'][0]['id']).exists())
        self.assertFalse(TrainingFeedback.objects.filter(series_session_id=link.pk).exists())
        self.assertFalse(TrainingCompetencyResult.objects.filter(pk__in=[result.pk, correction.pk]).exists())
        self.assertFalse(TrainingCertificate.objects.filter(result_id=result.pk).exists())

    def test_list_query_count_does_not_grow_with_page_size(self):
        for index in range(25):
            self.create_series(f'Series {index:02}')

        counts = []
        for per_page in ('25', '50'):
            with CaptureQueriesContext(connection) as queries:
                self.request('get', reverse('training_series_table'), {'per_page': per_page})
            counts.append(len(queries))

        self.assertEqual(counts[0], counts[1])

    def test_single_delete_requires_export_then_deletes_linked_session(self):
        series = self.create_series('Series to remove')
        session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Linked demo')
        TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=series, assessment_session=session, sequence=1)
        delete_url = reverse('training_series_delete', args=(series.pk,))

        self.assertEqual(self.request('get', delete_url).status_code, 405)
        denied = self.request('post', delete_url)
        self.assertEqual(denied.status_code, 302)
        self.assertTrue(TrainingSeries.objects.filter(pk=series.pk).exists())

        export = self.request('get', reverse('training_series_delete_export', args=(series.pk,)))
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export['Content-Type'], 'application/zip')
        self.assertTrue(export['X-Delete-Export-Token'])
        archive = zipfile.ZipFile(io.BytesIO(export.content))
        exported = json.loads(archive.read('team-series-export.json'))
        self.assertEqual(exported['series'][0]['name'], 'Series to remove')
        deleted = self.request('post', delete_url, {
            'delete_export_token': export['X-Delete-Export-Token'],
        })
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(TrainingSeries.objects.filter(pk=series.pk).exists())
        self.assertFalse(AssessmentSession.objects.filter(pk=session.pk).exists())

    def test_bulk_delete_is_all_or_nothing_and_rejects_bad_or_oversized_ids(self):
        first = self.create_series('First series')
        second = self.create_series('Second series')
        foreign = self.create_series('Other owner series', owner=self.other)
        url = reverse('training_series_bulk_delete')

        invalid_ids = ([], ['abc'], [str(index) for index in range(101)], ['1' * 20])
        for series_ids in invalid_ids:
            with self.subTest(ids=len(series_ids)):
                response = self.request('post', url, {'series_ids': series_ids})
                self.assertEqual(response.status_code, 302)
        self.assertTrue(TrainingSeries.objects.filter(pk=first.pk, pk__in=[first.pk]).exists())
        self.assertTrue(TrainingSeries.objects.filter(pk=second.pk).exists())

        export = self.request('post', reverse('training_series_bulk_delete_export'), {
            'series_ids': [str(first.pk), str(foreign.pk)],
        })
        self.assertEqual(export.status_code, 404)
        self.assertTrue(TrainingSeries.objects.filter(pk=first.pk).exists())

        own_export = self.request('post', reverse('training_series_bulk_delete_export'), {
            'series_ids': [str(first.pk)],
        })
        mixed_delete = self.request('post', url, {
            'series_ids': [str(first.pk), str(foreign.pk)],
            'delete_export_token': own_export['X-Delete-Export-Token'],
        })
        self.assertEqual(mixed_delete.status_code, 302)
        self.assertTrue(TrainingSeries.objects.filter(pk=first.pk).exists())
        self.assertTrue(TrainingSeries.objects.filter(pk=foreign.pk).exists())

        export = self.request('post', reverse('training_series_bulk_delete_export'), {
            'series_ids': [str(first.pk), str(second.pk), str(first.pk)],
        })
        self.assertEqual(export.status_code, 200)
        deleted = self.request('post', url, {
            'series_ids': [str(first.pk), str(second.pk), str(first.pk)],
            'delete_export_token': export['X-Delete-Export-Token'],
        })
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(TrainingSeries.objects.filter(pk__in=[first.pk, second.pk]).exists())
        self.assertTrue(TrainingSeries.objects.filter(pk=foreign.pk).exists())

    def test_delete_last_item_from_last_page_returns_to_a_valid_page(self):
        series = [self.create_series(f'Page item {index:02}') for index in range(26)]
        target = series[-1]
        export = self.request('get', reverse('training_series_delete_export', args=(target.pk,)))
        response = self.request('post', reverse('training_series_delete', args=(target.pk,)), {
            'delete_export_token': export['X-Delete-Export-Token'],
            'page': '2', 'per_page': '25',
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].number, 1)
        self.assertEqual(response.context['paginator'].count, 25)

    def test_new_routes_enforce_login_owner_and_organization_scope(self):
        series = self.create_series('Privately owned series')
        table_url = reverse('training_series_table')
        create_url = reverse('training_series_create')
        self.client.logout()
        anonymous = self.request('get', table_url)
        self.assertEqual(anonymous.status_code, 302)
        self.assertEqual(anonymous['Location'], reverse('service_entry', kwargs={'slug': 'team'}))

        sign_in(self.client, self.other, self.org)
        self.assertEqual(self.request('get', reverse('training_series_manage', args=(series.pk,))).status_code, 404)
        self.assertEqual(self.request('get', reverse('training_series_delete_export', args=(series.pk,))).status_code, 404)
        self.assertEqual(self.request('post', reverse('training_series_delete', args=(series.pk,))).status_code, 404)
        self.assertEqual(self.request('get', create_url).status_code, 200)

        sign_in(self.client, self.owner, self.other_org)
        self.assertEqual(self.request('get', reverse('training_series_delete_export', args=(series.pk,))).status_code, 404)
        self.assertEqual(self.request('post', reverse('training_series_delete', args=(series.pk,))).status_code, 404)
        self.assertTrue(TrainingSeries.objects.filter(pk=series.pk).exists())


class TrainingCompetencyDecisionOperationTests(TestCase):
    def request(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def setUp(self):
        self.org = make_org('Competency decisions')
        self.other_org = make_org('Other competency organization')
        self.owner = make_user('competency-assessor')
        self.other = make_user('other-assessor')
        self.series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Decision series')
        self.source = TrainingCompetency.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=self.series, name='Evidence', passing_score=70)
        self.session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Demo', identify_by='full_name')
        self.link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=self.series, assessment_session=self.session, sequence=1)
        self.criterion = TrainingSessionCompetency.objects.create(
            organization_id=self.org.id, created_by=self.owner, series_session=self.link,
            source_competency=self.source, name='Evidence', passing_score=70)
        self.student = Student.objects.create(assessment_session=self.session, full_name='Amina')

    def record(self, **kwargs):
        options = {'organization': self.org.as_claim(), 'user': self.owner}
        options.update(kwargs)
        return record_training_competency_result(
            self.session, self.criterion, self.student, 80, 'Demonstrated the criterion.', **options)

    def test_first_decision_rejects_unexpected_correction_reason_and_out_of_range_score(self):
        with self.assertRaisesMessage(ValidationError, 'only allowed when a previous'):
            record_training_competency_result(self.session, self.criterion, self.student, 80,
                'Demonstrated the criterion.', organization=self.org.as_claim(), user=self.owner,
                correction_reason='No previous result')
        with self.assertRaises(ValidationError):
            record_training_competency_result(self.session, self.criterion, self.student, 101,
                'Outside range.', organization=self.org.as_claim(), user=self.owner)
        self.assertFalse(TrainingCompetencyResult.objects.exists())

    def test_decision_requires_owner_scope_linked_criterion_and_session_student(self):
        with self.assertRaises(PermissionDenied):
            self.record(user=self.other)
        with self.assertRaises(PermissionDenied):
            self.record(organization=self.other_org.as_claim())

        other_series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other series')
        other_link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=other_series, assessment_session=AssessmentSession.objects.create(
                organization_id=self.org.id, created_by=self.owner, service='team', name='Other demo'), sequence=1)
        foreign_criterion = TrainingSessionCompetency.objects.create(
            organization_id=self.org.id, created_by=self.owner, series_session=other_link,
            name='Other criterion', passing_score=60)
        with self.assertRaises(PermissionDenied):
            record_training_competency_result(self.session, foreign_criterion, self.student, 80,
                'Wrong session criterion.', organization=self.org.as_claim(), user=self.owner)

        outsider = Student.objects.create(assessment_session=other_link.assessment_session,
            full_name='Other learner')
        with self.assertRaises(PermissionDenied):
            record_training_competency_result(self.session, self.criterion, outsider, 80,
                'Wrong session learner.', organization=self.org.as_claim(), user=self.owner)
        self.assertFalse(TrainingCompetencyResult.objects.exists())

    def test_revision_chain_requires_reason_and_preserves_original_if_insert_fails(self):
        first = self.record()
        with self.assertRaisesMessage(ValidationError, 'reason is required'):
            self.record()
        with patch('service_domains.operations.TrainingCompetencyResult.save',
                   side_effect=IntegrityError('duplicate revision')):
            with self.assertRaisesMessage(ValidationError, 'submitted at the same time'):
                self.record(correction_reason='Correct a mistaken observation.')
        self.assertEqual(TrainingCompetencyResult.objects.count(), 1)
        correction = self.record(correction_reason='Correct a mistaken observation.')
        self.assertEqual(correction.supersedes, first)
        self.assertEqual(correction.revision, 2)

    def test_result_model_rejects_cross_session_student_empty_evidence_and_bad_revision(self):
        other_session = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Unlinked learner session')
        outsider = Student.objects.create(assessment_session=other_session, full_name='Outsider')
        wrong_student = TrainingCompetencyResult(organization_id=self.org.id,
            created_by=self.owner, criterion=self.criterion, student=outsider, score=80,
            evidence='Evidence')
        with self.assertRaisesMessage(ValidationError, 'must belong to this Team session'):
            wrong_student.full_clean()
        empty_evidence = TrainingCompetencyResult(organization_id=self.org.id,
            created_by=self.owner, criterion=self.criterion, student=self.student, score=80, evidence=' ')
        with self.assertRaisesMessage(ValidationError, 'Record the evidence'):
            empty_evidence.full_clean()
        wrong_revision = TrainingCompetencyResult(organization_id=self.org.id,
            created_by=self.owner, criterion=self.criterion, student=self.student, score=80,
            evidence='Evidence', revision=2)
        with self.assertRaisesMessage(ValidationError, 'must be revision one'):
            wrong_revision.full_clean()

        first = self.record()
        wrong_series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Another series')
        wrong_source = TrainingCompetency.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=wrong_series, name='Different', passing_score=60)
        snapshot = TrainingSessionCompetency(organization_id=self.org.id,
            created_by=self.owner, series_session=self.link, source_competency=wrong_source,
            name='Wrong source', passing_score=60)
        with self.assertRaisesMessage(ValidationError, 'must belong to this series'):
            snapshot.full_clean()

        other_criterion = TrainingSessionCompetency.objects.create(
            organization_id=self.org.id, created_by=self.owner, series_session=self.link,
            name='Other criterion', passing_score=60)
        wrong_chain = TrainingCompetencyResult(organization_id=self.org.id,
            created_by=self.owner, criterion=other_criterion, student=self.student,
            score=80, evidence='Evidence', correction_reason='Wrong prior record.',
            revision=2, supersedes=first)
        with self.assertRaisesMessage(ValidationError, 'same-learner decision'):
            wrong_chain.full_clean()
        missing_reason = TrainingCompetencyResult(organization_id=self.org.id,
            created_by=self.owner, criterion=self.criterion, student=self.student,
            score=80, evidence='Evidence', revision=2, supersedes=first)
        with self.assertRaisesMessage(ValidationError, 'reason is required'):
            missing_reason.full_clean()

    def test_session_without_series_link_is_denied_by_operation_and_outcomes_view(self):
        orphan = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Unlinked demo')
        with self.assertRaises(PermissionDenied):
            record_training_competency_result(orphan, self.criterion, self.student, 80,
                'No series link.', organization=self.org.as_claim(), user=self.owner)
        sign_in(self.client, self.owner, self.org)
        url = reverse('training_session_competencies', kwargs={'pk': orphan.pk})
        self.assertEqual(self.request('get', url).status_code, 404)

    def test_duplicate_roster_reference_and_invalid_competency_are_rejected(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Practice')
        detail = reverse('training_series_manage', args=(series.pk,))
        TrainingParticipant.objects.create(organization_id=self.org.id, created_by=self.owner,
            series=series, display_name='Amina', reference='A-17', group_name='Team A')
        duplicate = self.request('post', detail, {
            'action': 'participant', 'display_name': 'Juma',
            'reference': 'A-17', 'group_name': 'Team B',
        })
        self.assertEqual(duplicate.status_code, 200)
        self.assertContains(duplicate, 'reference')
        invalid_competency = self.request('post', detail, {
            'action': 'competency', 'name': 'Evidence', 'description': '', 'passing_score': '101',
        })
        self.assertEqual(invalid_competency.status_code, 200)
        self.assertContains(invalid_competency, 'Ensure this value is less than or equal to 100')
        self.assertEqual(series.participants.count(), 1)
        self.assertEqual(series.competencies.count(), 0)

    def test_invalid_participant_form_is_redisplayed_without_write(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Roster validation')
        detail = reverse('training_series_manage', args=(series.pk,))
        response = self.request('post', detail, {
            'action': 'participant', 'display_name': '', 'reference': '', 'group_name': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This field is required.')
        self.assertEqual(series.participants.count(), 0)

    def test_empty_roster_cannot_create_session_even_if_form_is_submitted_directly(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Empty')
        detail = reverse('training_series_manage', args=(series.pk,))
        template = SessionTemplate.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Team rubric')
        response = self.request('post', detail, {
            'action': 'create_session', 'name': 'Should not exist', 'template': f'saved:{template.pk}',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Add at least one series participant')
        self.assertFalse(AssessmentSession.objects.filter(name='Should not exist').exists())

    def test_competency_model_validation_and_operation_permission_errors_are_shown(self):
        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Decision series')
        detail = reverse('training_series_manage', args=(series.pk,))
        TrainingCompetency.objects.create(organization_id=self.org.id, created_by=self.owner,
            series=series, name='Evidence', passing_score=70)
        duplicate = self.request('post', detail, {
            'action': 'competency', 'name': 'Evidence', 'description': '', 'passing_score': '70',
        })
        self.assertEqual(duplicate.status_code, 200)
        self.assertContains(duplicate, 'already exists')

        template = SessionTemplate.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='team', name='Team rubric')
        TrainingParticipant.objects.create(organization_id=self.org.id, created_by=self.owner,
            series=series, display_name='Amina', group_name='Team A')
        with patch('service_domains.views.create_next_training_session', side_effect=PermissionDenied):
            refused = self.request('post', detail, {
                'action': 'create_session', 'name': 'Week 1', 'template': f'saved:{template.pk}',
            })
        self.assertEqual(refused.status_code, 404)

    def test_team_owner_wrapper_rejects_a_different_active_service(self):
        request = RequestFactory().get('/team/series/')
        request.service = 'showcase'
        with self.assertRaises(Http404):
            training_series_list(request)

    def test_template_form_rejects_non_team_template_if_queryset_is_broadened(self):
        template = SessionTemplate.objects.create(organization_id=self.org.id,
            created_by=self.owner, service='classroom', name='Classroom template')
        form = CreateTrainingSessionForm({'name': 'Week 1', 'template': f'saved:{template.pk}'},
            organization=self.org.as_claim(), user=self.owner)
        self.assertFalse(form.is_valid())
        self.assertIn('Select a valid choice', form.errors['template'][0])

    def test_team_series_step_navigation_save_advance_embedded_import_and_removals(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from service_domains.training_roster_import import sample_training_roster

        sign_in(self.client, self.owner, self.org)
        series = TrainingSeries.objects.create(
            organization_id=self.org.id, created_by=self.owner, name='Initial Series'
        )
        detail = reverse('training_series_manage', args=(series.pk,))

        saved_res = self.request('post', detail, {
            'action': 'series_details',
            'name': 'Updated Series',
            'description': 'Skill mastery',
            'completion_rule': 'all_competencies',
            'certificate_valid_days': '365',
        })
        self.assertEqual(saved_res.status_code, 302)
        self.assertEqual(saved_res['Location'], f'{detail}?step=details&saved=1')
        details_page = self.request('get', f'{detail}?step=details&saved=1')
        self.assertContains(details_page, 'Series details saved.')
        self.assertContains(details_page, 'id="team-unsaved-dialog"')
        self.assertContains(details_page, 'data-panel-details-form')

        advance_res = self.request('post', detail, {
            'action': 'series_details',
            'name': 'Advanced Series',
            'description': 'Skill mastery',
            'completion_rule': 'all_competencies',
            'certificate_valid_days': '365',
            'advance': '1',
        })
        self.assertEqual(advance_res.status_code, 302)
        self.assertEqual(advance_res['Location'], f'{detail}?step=roster')

        add_p = self.request('post', detail, {
            'action': 'participant',
            'display_name': 'Grace',
            'reference': 'GR-1',
            'group_name': 'Cohort 1',
            'setup_step': 'roster',
        })
        self.assertEqual(add_p.status_code, 302)
        self.assertEqual(add_p['Location'], f'{detail}?step=roster')
        participant = TrainingParticipant.objects.get(series=series, display_name='Grace')

        add_c = self.request('post', detail, {
            'action': 'competency',
            'name': 'Safety Check',
            'description': 'Pre-flight check',
            'passing_score': '80',
            'setup_step': 'competencies',
        })
        self.assertEqual(add_c.status_code, 302)
        self.assertEqual(add_c['Location'], f'{detail}?step=competencies')
        competency = TrainingCompetency.objects.get(series=series, name='Safety Check')

        rm_p = self.request('post', detail, {
            'action': 'remove_participant',
            'participant_id': str(participant.pk),
            'setup_step': 'roster',
        })
        self.assertEqual(rm_p.status_code, 302)
        self.assertEqual(rm_p['Location'], f'{detail}?step=roster')
        self.assertFalse(TrainingParticipant.objects.filter(pk=participant.pk).exists())

        rm_c = self.request('post', detail, {
            'action': 'remove_competency',
            'competency_id': str(competency.pk),
            'setup_step': 'competencies',
        })
        self.assertEqual(rm_c.status_code, 302)
        self.assertEqual(rm_c['Location'], f'{detail}?step=competencies')
        self.assertFalse(TrainingCompetency.objects.filter(pk=competency.pk).exists())

        priv_res = self.request('post', detail, {
            'action': 'feedback_privacy',
            'minimum_responses': '5',
            'setup_step': 'demos',
        })
        self.assertEqual(priv_res.status_code, 302)
        self.assertEqual(priv_res['Location'], f'{detail}?step=demos')

        import_url = reverse('training_series_roster_import', args=(series.pk,))
        empty_upload = self.request('post', f'{import_url}?embedded=1', {'setup_step': 'roster'})
        self.assertEqual(empty_upload.status_code, 302)
        self.assertEqual(empty_upload['Location'], f'{detail}?step=roster')

        bad_ext = SimpleUploadedFile('roster.csv', b'a,b,c', content_type='text/csv')
        bad_upload = self.request('post', f'{import_url}?embedded=1', {'file': bad_ext, 'setup_step': 'roster'})
        self.assertEqual(bad_upload.status_code, 302)
        self.assertEqual(bad_upload['Location'], f'{detail}?step=roster')

        corrupt_xlsx = SimpleUploadedFile('roster.xlsx', b'not-a-zip', content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        corrupt_upload = self.request('post', f'{import_url}?embedded=1', {'file': corrupt_xlsx, 'setup_step': 'roster'})
        self.assertEqual(corrupt_upload.status_code, 302)
        self.assertEqual(corrupt_upload['Location'], f'{detail}?step=roster')

        valid_xlsx = SimpleUploadedFile(
            'roster.xlsx',
            sample_training_roster().getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        ok_upload = self.request('post', f'{import_url}?embedded=1', {'file': valid_xlsx, 'setup_step': 'roster'})
        self.assertEqual(ok_upload.status_code, 302)
        self.assertEqual(ok_upload['Location'], f'{detail}?step=roster')

        preview_manage = self.request('get', detail)
        self.assertEqual(preview_manage.context['active_step'], 'roster')
        self.assertContains(preview_manage, 'id="roster-inline-preview"')

        cancel_res = self.request('post', f'{import_url}?embedded=1', {'action': 'cancel', 'setup_step': 'roster'})
        self.assertEqual(cancel_res.status_code, 302)
        self.assertEqual(cancel_res['Location'], f'{detail}?step=roster')

        valid_xlsx_2 = SimpleUploadedFile(
            'roster.xlsx',
            sample_training_roster().getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.request('post', f'{import_url}?embedded=1', {'file': valid_xlsx_2, 'setup_step': 'roster'})
        apply_res = self.request('post', f'{import_url}?embedded=1', {'action': 'apply', 'setup_step': 'roster'})
        self.assertEqual(apply_res.status_code, 302)
        self.assertEqual(apply_res['Location'], f'{detail}?step=roster')
        self.assertGreater(series.participants.count(), 0)

