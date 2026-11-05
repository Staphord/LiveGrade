import csv
import io
import re
import uuid
from collections import Counter
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.http import Http404
from django.test import RequestFactory, TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (AssessmentSession, GroupMembership, PresentationGroup,
                                SessionTemplate, Student, TemplateRubricCategory)
from service_domains.models import (TrainingCompetency, TrainingCompetencyResult,
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
        with patch('portal.middleware.is_released', side_effect=lambda slug: slug == 'team'):
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
        self.assertContains(self.request('get', outcomes_url), 'Verify issued certificate')
        public_url = reverse('training_certificate_verify', kwargs={'public_id': certificate.public_id})
        verification = self.request('get', public_url)
        self.assertContains(verification, 'matches the learner’s current competency decision')
        self.assertContains(verification, 'Amina Juma')
        self.assertContains(verification, 'Clear communication')
        self.assertNotContains(verification, 'private@example.org')
        self.assertNotContains(verification, 'Provided a clear and accurate demonstration')
        with patch('service_domains.views.TrainingCompetencyResult.objects.filter') as latest_query:
            latest_query.return_value.order_by.return_value.values_list.return_value.first.return_value = None
            orphaned_verification = self.request('get', public_url)
        self.assertEqual(orphaned_verification.status_code, 200)
        self.assertContains(orphaned_verification, 'a later decision supersedes it')
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
        self.assertContains(superseded, 'a later decision supersedes it')
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
        response = self.request('post', reverse('training_series_list'), {
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
        self.assertContains(response, 'Demo 3: 90')
        self.assertContains(response, '55.00 → 90.00 (35.00)')
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
        self.assertContains(response, 'Team competency trends')
        self.assertContains(response, 'Team demo 2: 80.00 (2 learners)')
        self.assertContains(response, '60.00 → 95.00 (35.00)')

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

    def test_series_list_get_and_invalid_create_render_validation(self):
        sign_in(self.client, self.owner, self.org)
        page = self.request('get', reverse('training_series_list'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Getting started')
        self.assertContains(page, 'Set up your first Team and Training series')
        self.assertContains(page, 'Create a Team demo setup')
        self.assertContains(page, 'Create each demo from the series')
        self.assertContains(page, 'href="#create-series"')
        self.assertContains(page, 'Purpose or intended skill goal')
        TrainingSeries.objects.create(organization_id=self.org.id, created_by=self.owner,
            name='Existing series')
        existing = self.request('get', reverse('training_series_list'))
        self.assertContains(existing, 'Your workflow')
        self.assertContains(existing, 'Your Team and Training workspace')
        self.assertNotContains(existing, 'Set up your first Team and Training series')
        self.assertContains(existing, 'Continue your latest series: Existing series')
        invalid = self.request('post', reverse('training_series_list'), {'name': '', 'description': ''})
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
        created = self.request('post', reverse('training_series_list'), {
            'name': 'New staff induction', 'description': '',
        })
        self.assertEqual(created.status_code, 302)
        series = TrainingSeries.objects.get(name='New staff induction')
        detail = self.request('get', reverse('training_series_manage', args=(series.pk,)))
        self.assertContains(detail, 'Choose a ready-made Team rubric below')
        self.assertContains(detail, 'Import a team roster')
        self.assertContains(detail, 'Create next demo session')
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
        response = self.request('post', reverse('training_series_list'), {'name': 'Team', 'description': ''})
        series = TrainingSeries.objects.get(name='Team')
        detail = reverse('training_series_manage', args=(series.pk,))
        template = SessionTemplate.objects.create(organization_id=self.org.id,
            created_by=self.other, service='team', name='Other owner template')
        rejected = self.request('post', detail, {
            'action': 'create_session', 'name': 'Week 1', 'template': f'saved:{template.pk}',
        })
        self.assertEqual(rejected.status_code, 200)
        self.assertFalse(AssessmentSession.objects.filter(name='Week 1').exists())


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
