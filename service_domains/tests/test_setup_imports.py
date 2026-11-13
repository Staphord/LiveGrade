"""Spreadsheet imports for Team competencies and Competition criteria, and the setup-flow fixes."""
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user, sign_in
from assessments.models import AssessmentSession
from assessments.sheets import workbook_bytes
from service_domains import competition_criteria_import as crit
from service_domains import embedded_import
from service_domains import training_competency_import as comp
from service_domains.models import (
    Competition, CompetitionCriterion, CompetitionEntry, CompetitionTrack, TrainingCompetency,
    TrainingParticipant, TrainingSeries, TrainingSeriesSession,
)

XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def sheet(headers, rows, name='data.xlsx'):
    return SimpleUploadedFile(name, workbook_bytes(headers, rows).getvalue(), content_type=XLSX)


class TemplateStylesheetGuardTests(SimpleTestCase):
    def test_every_page_with_a_dropzone_loads_the_dropzone_styles(self):
        """A dropzone without its stylesheet stretches the hidden file input over the page."""
        root = Path(settings.BASE_DIR) / 'templates'
        offenders = []
        for path in root.rglob('*.html'):
            text = path.read_text()
            uses = 'assessments/_partials/import_dropzone.html' in text
            if not uses or path.name in ('import_dropzone.html', '_import_card.html'):
                continue
            if 'import_dropzone_styles' in text or '_showcase_import_styles' in text:
                continue
            offenders.append(str(path.relative_to(root)))
        # Pages that only render inside a parent which already loads the styles.
        allowed = {'assessments/polls/_question_import.html'}
        self.assertEqual([item for item in offenders if item not in allowed], [])


class CompetencyImportParseTests(SimpleTestCase):
    HEADERS = ['Competency', 'Description', 'Pass score']

    def parse(self, rows, headers=None):
        return comp.parse_competencies(sheet(headers or self.HEADERS, rows))

    def test_valid_rows(self):
        parsed = self.parse([['Incident response', 'Escalates', 70], ['Comms', '', '60.5']])
        self.assertIsNone(parsed['fatal'])
        self.assertEqual([r['passing_score'] for r in parsed['rows']], ['70', '60.5'])
        self.assertTrue(all(not r['errors'] for r in parsed['rows']))

    def test_row_errors(self):
        rows = self.parse([['', 'x', 'abc'], ['N' * 121, 'D' * 1001, 101], ['Dec', '', 1.234]])['rows']
        joined = [' '.join(r['errors']) for r in rows]
        self.assertIn('name is required', joined[0])
        self.assertIn('Pass score must be', joined[0])
        self.assertIn('longer than 120', joined[1])
        self.assertIn('Description is longer', joined[1])
        self.assertIn('Pass score must be', joined[2])

    def test_formula_cells_are_rejected(self):
        raw = [self.HEADERS, ['=cmd()', 'd', 50], ['Ok', '=1+1', 50]]
        with patch.object(comp, '_read_bounded_rows', return_value=raw):
            rows = comp.parse_competencies(SimpleUploadedFile('x.xlsx', b'x'))['rows']
        self.assertTrue(all('Formula cells' in ' '.join(r['errors']) for r in rows))

    def test_non_finite_pass_score(self):
        self.assertIn('Pass score must be', self.parse([['A', '', 'NaN']])['rows'][0]['errors'][0])

    def test_fatal_files(self):
        self.assertEqual(comp.parse_competencies(SimpleUploadedFile('x.xlsx', b'junk'))['fatal'], comp.UNREADABLE)
        self.assertIn('empty', self.parse([], [''])['fatal'])
        self.assertIn('Competency and Pass score', self.parse([['a']], ['Name'])['fatal'])
        self.assertIn('more than 100', self.parse([[f'c{i}', '', 50] for i in range(101)])['fatal'])
        with patch.object(comp, 'MAX_UPLOAD_BYTES', 1):
            self.assertIn('10 MB', self.parse([['a', '', 50]])['fatal'])
        with patch.object(comp, '_read_bounded_rows', return_value=comp.TOO_MANY_PHYSICAL_ROWS):
            self.assertIn('physical row limit', self.parse([['a', '', 50]])['fatal'])

    def test_sample_workbook_parses_cleanly(self):
        parsed = comp.parse_competencies(SimpleUploadedFile('s.xlsx', comp.sample_workbook().getvalue()))
        self.assertTrue(all(not row['errors'] for row in parsed['rows']))


class TrainingCompetencyImportTests(TestCase):
    def setUp(self):
        self.org = make_org('Competency import org')
        self.owner = make_user('competency-import-owner')
        self.stranger = make_user('competency-import-stranger')
        self.series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Import series')
        self.import_url = reverse('training_competency_import', args=(self.series.pk,))
        self.manage = reverse('training_series_manage', args=(self.series.pk,))
        sign_in(self.client, self.owner, self.org)

    def call(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'team'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def upload(self, rows, headers=('Competency', 'Description', 'Pass score')):
        return self.call('post', self.import_url, {'file': sheet(list(headers), rows)})

    def token(self):
        return self.client.session[f'embedded_import_preview_training-competency_{self.series.pk}']

    def test_step_has_the_established_dropzone_and_import_card(self):
        page = self.call('get', f'{self.manage}?step=competencies')
        self.assertContains(page, 'data-import-dropzone')
        self.assertContains(page, 'id="team-competency-dropzone"')
        self.assertContains(page, 'Import competencies')
        self.assertContains(page, 'Preview competencies')
        self.assertContains(page, reverse('training_competency_import_sample', args=(self.series.pk,)))
        self.assertContains(page, '.import-dropzone{border:1.5px dashed')
        self.assertEqual(page.content.decode().count('id="id_file"'), 1)

    def test_preview_then_apply_adds_competencies(self):
        response = self.upload([['Incident response', 'Escalates', 70], ['Comms', '', 60]])
        self.assertRedirects(response, f'{self.manage}?step=competencies', fetch_redirect_response=False)
        self.assertEqual(TrainingCompetency.objects.count(), 0)

        page = self.call('get', self.manage)
        self.assertEqual(page.context['active_step'], 'competencies')
        self.assertContains(page, 'Ready to import')
        self.assertContains(page, '2 competencies will be added')
        applied = self.call('post', self.import_url, {'action': 'apply', 'preview_token': self.token()})
        self.assertRedirects(applied, f'{self.manage}?step=competencies', fetch_redirect_response=False)
        self.assertEqual([c.name for c in TrainingCompetency.objects.order_by('order')],
                         ['Incident response', 'Comms'])
        self.assertEqual(TrainingCompetency.objects.get(name='Comms').passing_score, Decimal('60'))

    def test_new_rows_follow_the_existing_order_and_duplicates_are_flagged(self):
        TrainingCompetency.objects.create(series=self.series, name='Existing', passing_score=50,
            order=4, organization_id=self.org.id, created_by=self.owner)
        self.upload([['existing', '', 50], ['Fresh', '', 50], ['fresh', '', 50]])
        page = self.call('get', self.manage)
        self.assertContains(page, 'Needs attention')
        self.assertContains(page, 'already exists in the series')
        self.assertContains(page, 'repeated from row 3')

        self.upload([['Fresh', '', 50]])
        self.call('post', self.import_url, {'action': 'apply', 'preview_token': self.token()})
        self.assertEqual(TrainingCompetency.objects.get(name='Fresh').order, 5)

    def test_cancel_empty_and_invalid_uploads(self):
        self.upload([['A', '', 50]])
        cancelled = self.call('post', self.import_url, {'action': 'cancel'})
        self.assertRedirects(cancelled, f'{self.manage}?step=competencies', fetch_redirect_response=False)
        self.assertNotContains(self.call('get', self.manage), 'Spreadsheet preview')

        self.call('post', self.import_url, {'file': SimpleUploadedFile('x.csv', b'a,b')})
        self.assertContains(self.call('get', self.manage), 'Choose an Excel .xlsx file.')
        self.assertNotContains(self.call('get', self.manage), 'Choose an Excel .xlsx file.')
        self.upload([])
        self.assertContains(self.call('get', self.manage), 'The file has no competency rows.')

    def test_apply_guards(self):
        for token, expected in (('', 'This field is required.'), ('garbage', 'expired or is invalid')):
            self.call('post', self.import_url, {'action': 'apply', 'preview_token': token})
            self.assertContains(self.call('get', self.manage), expected)
        parsed = comp.parse_competencies(sheet(['Competency', 'Pass score'], [['A', 50]]))
        for owner_id, parsed_value in ((self.stranger.pk, parsed), (self.owner.pk, 'nope')):
            forged = signing.dumps({'owner': self.series.pk, 'organization': self.org.id,
                'user': owner_id, 'parsed': parsed_value}, salt='livegrade.embedded.import.training-competency.v1')
            self.assertEqual(self.call('post', self.import_url,
                {'action': 'apply', 'preview_token': forged}).status_code, 404)
        stale = signing.dumps({'owner': self.series.pk, 'organization': self.org.id,
            'user': self.owner.pk, 'parsed': comp.parse_competencies(sheet(['Competency', 'Pass score'], [['', 5]]))},
            salt='livegrade.embedded.import.training-competency.v1')
        self.call('post', self.import_url, {'action': 'apply', 'preview_token': stale})
        self.assertContains(self.call('get', self.manage), 'The competency import has errors')
        self.assertEqual(TrainingCompetency.objects.count(), 0)
        oversized = self.call('post', self.import_url, {'action': 'apply', 'preview_token': 'x' * 150_001})
        self.assertEqual(oversized.status_code, 302)

    def test_stored_state_is_scoped_to_owner(self):
        key = f'embedded_import_preview_training-competency_{self.series.pk}'
        errors = f'embedded_import_errors_training-competency_{self.series.pk}'
        session = self.client.session
        session[key] = 'tampered'
        session[errors] = {'owner': self.series.pk, 'organization': self.org.id,
                           'user': self.stranger.pk, 'errors': ['not yours']}
        session.save()
        self.assertNotContains(self.call('get', self.manage), 'not yours')
        self.assertNotIn(key, self.client.session)
        session = self.client.session
        other = TrainingSeries.objects.create(organization_id=self.org.id, created_by=self.owner, name='Other')
        parsed = comp.parse_competencies(sheet(['Competency', 'Pass score'], [['A', 50]]))
        session[key] = signing.dumps({'owner': other.pk, 'organization': self.org.id,
            'user': self.owner.pk, 'parsed': parsed}, salt='livegrade.embedded.import.training-competency.v1')
        session[errors] = {'owner': self.series.pk, 'organization': self.org.id,
                           'user': self.owner.pk, 'errors': 'not a list'}
        session.save()
        self.assertNotContains(self.call('get', self.manage), 'Spreadsheet preview')

    def test_sample_download_method_and_access(self):
        sample = self.call('get', reverse('training_competency_import_sample', args=(self.series.pk,)))
        self.assertEqual(sample['Content-Type'], XLSX)
        self.assertEqual(self.call('get', self.import_url).status_code, 405)
        sign_in(self.client, self.stranger, self.org)
        self.assertEqual(self.call('post', self.import_url, {'action': 'cancel'}).status_code, 404)


class CriteriaImportParseTests(SimpleTestCase):
    HEADERS = ['Track', 'Criterion', 'Weight', 'Description']

    def parse(self, rows, headers=None):
        return crit.parse_criteria(sheet(headers or self.HEADERS, rows))

    def test_valid_and_error_rows(self):
        good = self.parse([['Main', 'Idea', 50, 'd']])
        self.assertEqual(good['rows'][0]['weight'], '50')
        rows = self.parse([['', '', 'abc', ''], ['T', 'N' * 121, 0, 'D' * 1001], ['T', 'x', 100.123, '']])['rows']
        joined = [' '.join(r['errors']) for r in rows]
        self.assertIn('Track is required', joined[0])
        self.assertIn('Criterion name is required', joined[0])
        self.assertIn('Weight must be', joined[0])
        self.assertIn('longer than 120', joined[1])
        self.assertIn('Weight must be', joined[1])
        self.assertIn('Weight must be', joined[2])

    def test_formulas_and_fatal_files(self):
        raw = [self.HEADERS, ['=a', 'b', 10, ''], ['T', 'c', 10, '=x']]
        with patch.object(crit, '_read_bounded_rows', return_value=raw):
            rows = crit.parse_criteria(SimpleUploadedFile('x.xlsx', b'x'))['rows']
        self.assertTrue(all('Formula cells' in ' '.join(r['errors']) for r in rows))
        self.assertEqual(crit.parse_criteria(SimpleUploadedFile('x.xlsx', b'junk'))['fatal'], crit.UNREADABLE)
        self.assertIn('empty', self.parse([], [''])['fatal'])
        self.assertIn('Track, Criterion and Weight', self.parse([['a']], ['Name'])['fatal'])
        self.assertIn('more than 200', self.parse([['T', f'c{i}', 1, ''] for i in range(201)])['fatal'])
        with patch.object(crit, 'MAX_UPLOAD_BYTES', 1):
            self.assertIn('10 MB', self.parse([['T', 'a', 1, '']])['fatal'])
        with patch.object(crit, '_read_bounded_rows', return_value=crit.TOO_MANY_PHYSICAL_ROWS):
            self.assertIn('physical row limit', self.parse([['T', 'a', 1, '']])['fatal'])

    def test_sample_workbook_uses_the_first_track(self):
        for names in (['Robotics'], []):
            parsed = crit.parse_criteria(SimpleUploadedFile('s.xlsx', crit.sample_workbook(names).getvalue()))
            self.assertEqual(parsed['rows'][0]['track'], names[0] if names else 'Main track')


class CompetitionCriteriaImportTests(TestCase):
    def setUp(self):
        self.org = make_org('Criteria import org')
        self.owner = make_user('criteria-import-owner')
        self.stranger = make_user('criteria-import-stranger')
        self.competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Import cup', status=Competition.Status.DRAFT)
        self.track = CompetitionTrack.objects.create(organization_id=self.org.id,
            created_by=self.owner, competition=self.competition, name='Robotics')
        self.import_url = reverse('competition_criteria_import', args=(self.competition.pk,))
        self.manage = reverse('competition_manage', args=(self.competition.pk,))
        sign_in(self.client, self.owner, self.org)

    def call(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'competition'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def upload(self, rows):
        return self.call('post', self.import_url,
                         {'file': sheet(['Track', 'Criterion', 'Weight', 'Description'], rows)})

    def token(self):
        return self.client.session[f'embedded_import_preview_competition-criteria_{self.competition.pk}']

    def test_step_has_the_dropzone_card_beside_the_manual_form(self):
        page = self.call('get', f'{self.manage}?step=criteria')
        self.assertContains(page, 'id="competition-criteria-dropzone"')
        self.assertContains(page, 'Import judging criteria')
        self.assertContains(page, 'Add judging criterion')
        html = page.content.decode()
        self.assertEqual(html.count('id="criteria_import_file"'), 1)
        self.assertEqual(html.count('id="entry_import_file"'), 1)

    def test_preview_apply_and_ordering(self):
        CompetitionCriterion.objects.create(track=self.track, name='Existing', weight=Decimal('20'),
            order=3, organization_id=self.org.id, created_by=self.owner)
        self.upload([['robotics', 'Idea', 40, 'd'], ['Robotics', 'Delivery', 40, '']])
        page = self.call('get', self.manage)
        self.assertEqual(page.context['active_step'], 'criteria')
        self.assertContains(page, 'Ready to import')
        self.call('post', self.import_url, {'action': 'apply', 'preview_token': self.token()})
        names = list(CompetitionCriterion.objects.order_by('order').values_list('name', 'order'))
        self.assertEqual(names, [('Existing', 3), ('Idea', 4), ('Delivery', 5)])

    def test_plan_flags_unknown_tracks_duplicates_and_overweight(self):
        CompetitionCriterion.objects.create(track=self.track, name='Idea', weight=Decimal('70'),
            organization_id=self.org.id, created_by=self.owner)
        self.upload([['Nowhere', 'A', 10, ''], ['Robotics', 'idea', 10, ''],
                     ['Robotics', 'B', 40, ''], ['Robotics', 'b', 5, '']])
        page = self.call('get', self.manage)
        self.assertContains(page, 'no track called')
        self.assertContains(page, 'already has a criterion with that name')
        self.assertContains(page, 'repeated from row')
        self.assertContains(page, 'would total')
        self.assertEqual(CompetitionCriterion.objects.count(), 1)

    def test_empty_file_cancel_and_bad_uploads(self):
        self.upload([])
        self.assertContains(self.call('get', self.manage), 'The file has no criteria rows.')
        self.upload([['Robotics', 'Idea', 100, '']])
        self.call('post', self.import_url, {'action': 'cancel'})
        self.assertNotContains(self.call('get', self.manage), 'Spreadsheet preview')
        self.call('post', self.import_url, {'file': SimpleUploadedFile('x.csv', b'a')})
        self.assertContains(self.call('get', self.manage), 'Choose an Excel .xlsx file.')

    def test_apply_refuses_started_competitions_and_bad_tokens(self):
        self.upload([['Robotics', 'Idea', 100, '']])
        token = self.token()
        Competition.objects.filter(pk=self.competition.pk).update(status=Competition.Status.ACTIVE)
        self.call('post', self.import_url, {'action': 'apply', 'preview_token': token})
        self.assertContains(self.call('get', self.manage), 'only be imported before the competition starts')
        self.assertEqual(CompetitionCriterion.objects.count(), 0)

        Competition.objects.filter(pk=self.competition.pk).update(status=Competition.Status.DRAFT)
        stale = signing.dumps({'owner': self.competition.pk, 'organization': self.org.id,
            'user': self.owner.pk, 'parsed': crit.parse_criteria(sheet(['Track', 'Criterion', 'Weight'], [['Robotics', '', 5]]))},
            salt='livegrade.embedded.import.competition-criteria.v1')
        self.call('post', self.import_url, {'action': 'apply', 'preview_token': stale})
        self.assertContains(self.call('get', self.manage), 'The criteria import has errors')

    def test_sample_download_and_access(self):
        sample = self.call('get', reverse('competition_criteria_import_sample', args=(self.competition.pk,)))
        self.assertEqual(sample['Content-Type'], XLSX)
        self.assertEqual(self.call('get', self.import_url).status_code, 405)
        sign_in(self.client, self.stranger, self.org)
        self.assertEqual(self.call('post', self.import_url, {'action': 'cancel'}).status_code, 404)


class SeriesReviewAndWorkshopTests(TestCase):
    def setUp(self):
        self.org = make_org('Review org')
        self.owner = make_user('review-owner')
        self.series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Review series')
        sign_in(self.client, self.owner, self.org)
        self.manage = reverse('training_series_manage', args=(self.series.pk,))

    def call(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda s: s == 'team'):
            return getattr(self.client, method)(url, *args, **kwargs)

    def session(self, status=AssessmentSession.Status.DRAFT, name='Demo one'):
        assessment = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, name=name, status=status)
        return TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=self.series, assessment_session=assessment,
            sequence=self.series.series_sessions.count() + 1)

    def test_review_step_only_reviews_and_leads_to_the_workshop(self):
        empty = self.call('get', f'{self.manage}?step=review')
        self.assertContains(empty, 'Nothing is configured here')
        self.assertContains(empty, 'Create the first demo')
        link = self.session()
        TrainingParticipant.objects.create(series=self.series, display_name='Amina', group_name='A',
            organization_id=self.org.id, created_by=self.owner)
        page = self.call('get', f'{self.manage}?step=review')
        self.assertEqual(page.context['live_link'], link)
        self.assertContains(page, reverse('training_session_live', args=(self.series.pk, link.pk)))
        self.assertContains(page, 'Go to live workshop: Demo one')
        html = page.content.decode()
        review = html[html.index('id="series-review"'):]
        review = review[:review.index('</section>')]
        self.assertNotIn('<form', review)
        self.assertNotIn('Certificates of Competence', html)

    def test_review_when_every_demo_is_closed(self):
        self.session(status=AssessmentSession.Status.CLOSED)
        page = self.call('get', f'{self.manage}?step=review')
        self.assertIsNone(page.context['live_link'])
        self.assertContains(page, 'Every demo is closed')

    def test_feedback_setting_lives_in_the_demos_step(self):
        page = self.call('get', f'{self.manage}?step=demos')
        html = page.content.decode()
        demos = html[html.index('data-panel-panel="demos"'):html.index('data-panel-panel="review"')]
        self.assertIn('name="action" value="feedback_privacy"', demos)
        self.assertEqual(html.count('name="action" value="feedback_privacy"'), 1)
        saved = self.call('post', self.manage, {'action': 'feedback_privacy', 'minimum_responses': '5',
                                                'setup_step': 'demos'})
        self.assertRedirects(saved, f'{self.manage}?step=demos', fetch_redirect_response=False)
        invalid = self.call('post', self.manage, {'action': 'feedback_privacy', 'minimum_responses': 'x'})
        self.assertEqual(invalid.context['active_step'], 'demos')

    def test_each_setup_action_returns_to_its_own_step(self):
        for action, step in (('series_details', 'details'), ('participant', 'roster'),
                             ('competency', 'competencies'), ('create_session', 'demos')):
            response = self.call('post', self.manage, {'action': action})
            self.assertEqual(response.context['active_step'], step, action)

    def test_review_step_completes_once_a_demo_has_started(self):
        self.session(status=AssessmentSession.Status.DRAFT)
        done = lambda page: {s['key']: s['done'] for s in page.context['stepper_steps']}['review']  # noqa: E731
        self.assertFalse(done(self.call('get', self.manage)))
        self.session(status=AssessmentSession.Status.LIVE, name='Demo two')
        self.assertTrue(done(self.call('get', self.manage)))

    def workshop(self, link):
        return self.call('get', reverse('training_session_live', args=(self.series.pk, link.pk)))

    def test_draft_workshop_offers_start_and_hides_the_drill_console(self):
        link = self.session()
        page = self.workshop(link)
        self.assertEqual(page.context['session_status'], 'draft')
        self.assertFalse(page.context['can_go_live'])
        self.assertContains(page, 'Not live yet')
        self.assertContains(page, 'Start session')
        self.assertContains(page, 'disabled title="Finish the session setup first"')
        self.assertNotContains(page, 'id="training-live-hero"')
        self.assertNotContains(page, 'Projector screen')
        self.assertNotContains(page, 'Outcomes &amp; certificates')
        self.assertNotContains(page, 'Finish session')

    def test_start_action_requires_a_ready_draft_session(self):
        link = self.session()
        action = reverse('training_session_live_action', args=(self.series.pk, link.pk))
        refused = self.call('post', action, {'action': 'start_session'})
        self.assertRedirects(refused, reverse('training_session_live', args=(self.series.pk, link.pk)),
                             fetch_redirect_response=False)
        ajax = self.call('post', action, {'action': 'start_session'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(ajax.status_code, 400)
        self.assertIn('rubric', ajax.json()['error'])

        with patch.object(AssessmentSession, 'can_go_live', return_value=True):
            started = self.call('post', action, {'action': 'start_session'})
            self.assertEqual(started.status_code, 302)
            link.assessment_session.refresh_from_db()
            self.assertEqual(link.assessment_session.status, AssessmentSession.Status.LIVE)
            again = self.call('post', action, {'action': 'start_session'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(again.status_code, 400)
        second = self.session(name='Demo two')
        with patch.object(AssessmentSession, 'can_go_live', return_value=True):
            fetched = self.call('post', reverse('training_session_live_action', args=(self.series.pk, second.pk)),
                                {'action': 'start_session'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(fetched.json()['redirect'], reverse('training_session_live', args=(self.series.pk, second.pk)))

    def test_ready_draft_workshop_enables_start(self):
        link = self.session()
        with patch.object(AssessmentSession, 'can_go_live', return_value=True):
            page = self.workshop(link)
        self.assertTrue(page.context['can_go_live'])
        self.assertContains(page, 'The rubric and groups are ready.')
        self.assertNotContains(page, 'disabled title="Finish the session setup first"')

    def test_drill_actions_need_a_live_session(self):
        link = self.session()
        action = reverse('training_session_live_action', args=(self.series.pk, link.pk))
        refused = self.call('post', action, {'action': 'set_drill_phase', 'phase': 'simulation'},
                            HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()['error'], 'This session is not live.')

    def test_live_workshop_has_one_of_each_control(self):
        link = self.session(status=AssessmentSession.Status.LIVE)
        html = self.workshop(link).content.decode()
        manage = reverse('training_series_manage', args=(self.series.pk,))
        self.assertEqual(html.count('Edit setup'), 1)
        self.assertEqual(html.count('name="action" value="conclude_session"'), 1)
        self.assertEqual(html.count('training-finish-modal'), 0)
        self.assertNotIn('training-feedback-qr-modal', html)
        self.assertEqual(html.count('aria-label="Workshop workspace"'), 1)
        self.assertEqual(html.count('Session 1'), html.count('Session 1'))
        self.assertIn('id="training-conn-badge"', html)
        self.assertEqual(html.count('Open projector screen'), 1)
        self.assertNotIn('page-tab" target="_blank"', html)
        self.assertIn('Outcomes &amp; certificates', html)
        self.assertNotIn('Cohort workshop drill live observation', html)

    def test_closed_workshop_shows_only_the_closed_status(self):
        link = self.session(status=AssessmentSession.Status.CLOSED)
        page = self.workshop(link)
        html = page.content.decode()
        self.assertContains(page, 'This session is closed')
        self.assertContains(page, reverse('training_session_competencies', args=(link.assessment_session_id,)))
        for gone in ('id="training-live-hero"', 'id="training-conn-badge"', 'Finish session', 'Edit setup',
                     'Open projector screen', 'training-feedback-qr-modal'):
            self.assertNotIn(gone, html)
        self.assertIn('Competency trends', html)


class EmbeddedImportHelperTests(TestCase):
    def test_load_preview_clears_a_session_key_with_no_token(self):
        owner = TrainingSeries.objects.create(organization_id=make_org('Helper org').id,
            created_by=make_user('helper-owner'), name='Helper')
        request = type('R', (), {})()
        request.session = {f'embedded_import_preview_x_{owner.pk}': ''}
        request.organization = {'id': owner.organization_id}
        request.user = owner.created_by
        self.assertEqual(embedded_import.load_preview(request, 'x', owner, lambda *a: None), (None, ''))


class ImportEdgeCaseTests(TestCase):
    def setUp(self):
        self.org = make_org('Edge org')
        self.owner = make_user('edge-owner')
        self.stranger = make_user('edge-stranger')
        self.series = TrainingSeries.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Edge series')
        self.competition = Competition.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Edge cup', status=Competition.Status.DRAFT)
        sign_in(self.client, self.owner, self.org)

    def call(self, method, url, *args, **kwargs):
        with patch('portal.middleware.is_released', side_effect=lambda s: s in ('team', 'competition')):
            return getattr(self.client, method)(url, *args, **kwargs)

    def test_unreadable_files_show_a_fatal_preview_for_both_imports(self):
        junk = lambda: SimpleUploadedFile('broken.xlsx', b'not a workbook')  # noqa: E731
        self.call('post', reverse('training_competency_import', args=(self.series.pk,)), {'file': junk()})
        page = self.call('get', reverse('training_series_manage', args=(self.series.pk,)))
        self.assertContains(page, 'Needs attention')
        self.assertEqual(page.context['competency_import_plan']['errors'], [(None, comp.UNREADABLE)])
        self.call('post', reverse('competition_criteria_import', args=(self.competition.pk,)), {'file': junk()})
        page = self.call('get', reverse('competition_manage', args=(self.competition.pk,)))
        self.assertEqual(page.context['criteria_import_plan']['errors'], [(None, crit.UNREADABLE)])

    def test_applying_for_an_owner_that_vanished_is_not_found(self):
        from django.core.exceptions import PermissionDenied
        url = reverse('training_competency_import', args=(self.series.pk,))
        self.call('post', url, {'file': sheet(['Competency', 'Pass score'], [['A', 50]])})
        token = self.client.session[f'embedded_import_preview_training-competency_{self.series.pk}']
        with patch('service_domains.views.apply_competencies', side_effect=PermissionDenied):
            response = self.call('post', url, {'action': 'apply', 'preview_token': token})
        self.assertEqual(response.status_code, 404)

    def test_apply_functions_refuse_other_owners(self):
        from django.core.exceptions import PermissionDenied
        claim = self.org.as_claim()
        with self.assertRaises(PermissionDenied):
            comp.apply_competencies(self.series, {'fatal': None, 'rows': []}, organization=claim,
                                    user=self.stranger)
        with self.assertRaises(PermissionDenied):
            crit.apply_criteria(self.competition, {'fatal': None, 'rows': []}, organization=claim,
                                user=self.stranger)

    def test_plans_report_fatal_parses(self):
        fatal = {'fatal': 'Broken.', 'rows': []}
        self.assertEqual(comp.plan_competencies(self.series, fatal)['errors'], [(None, 'Broken.')])
        self.assertEqual(crit.plan_criteria(self.competition, fatal)['errors'], [(None, 'Broken.')])

    def test_unknown_training_live_actions_are_refused(self):
        assessment = AssessmentSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Live demo', status=AssessmentSession.Status.LIVE)
        link = TrainingSeriesSession.objects.create(organization_id=self.org.id,
            created_by=self.owner, series=self.series, assessment_session=assessment, sequence=1)
        response = self.call('post', reverse('training_session_live_action', args=(self.series.pk, link.pk)),
                             {'action': 'launch_rocket'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Unknown action', response.json()['error'])
