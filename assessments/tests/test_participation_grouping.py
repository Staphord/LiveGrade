"""The Participation page lists people by their presenting group. Only the listing changed."""
from io import BytesIO

import openpyxl

from assessments.models import (Evaluation, EvaluationScore, ParticipationRecord, PresentationGroup,
                                PresentationTurn, Student)
from assessments.tests.test_edges_lecturer_views import LecturerTestCase


class ParticipationGroupingTests(LecturerTestCase):
    """Fixture: Red (Ann Able, Bob Best) and Blue (Cat Cole, Dan Dyer); a student with no group
    who sorts first alphabetically; and the groups' order swapped so the order must come from them."""

    def setUp(self):
        super().setUp()
        PresentationGroup.objects.filter(pk=self.blue.pk).update(order=1)
        PresentationGroup.objects.filter(pk=self.red.pk).update(order=2)
        self.loner = Student.objects.create(assessment_session=self.session, full_name='Aaron Zed', student_id='S9')

    def names(self, response):
        return [row['student'].full_name for row in response.context['rows']]

    def page(self, **params):
        return self.client.get(self.url('assessment_participation'), params)

    def test_people_are_listed_by_group_in_presenting_order_then_by_name_with_no_group_last(self):
        self.assertEqual(self.names(self.page()),
                         ['Cat Cole', 'Dan Dyer', 'Ann Able', 'Bob Best', 'Aaron Zed'])

    def test_each_group_gets_a_header_with_its_student_count_and_the_loner_goes_under_no_group(self):
        page = self.page()
        html = page.content.decode()
        self.assertContains(page, '<header class="participation-section-head', count=3)
        self.assertContains(page, '>2 students<', count=2)
        self.assertContains(page, '>1 student<')
        self.assertContains(page, 'No group yet', count=2)  # the section header and Aaron's own team banner
        self.assertLess(html.index('>Blue<'), html.index('>Red<'))
        self.assertLess(html.index('>Red<'), html.index('fw-bold">No group yet'))

    def test_a_group_s_topic_and_location_show_under_its_name(self):
        PresentationGroup.objects.filter(pk=self.blue.pk).update(topic='Smart maps', location='Room 7')
        page = self.page()
        self.assertContains(page, 'bi-lightbulb me-1"></i>Smart maps')
        self.assertContains(page, 'bi-geo-alt me-1"></i>Room 7')

    def test_the_group_on_stage_is_marked_in_its_header(self):
        self.set_status('live')
        self.active_turn(self.red)
        page = self.page()
        self.assertContains(page, '<span class="section-live ', count=1)
        html = page.content.decode()
        marker = html.index('<span class="section-live ')
        self.assertLess(html.index('>Red<'), marker)
        self.assertLess(marker, html.index('fw-bold">No group yet'))

    def test_the_status_filters_still_filter_and_keep_the_grouping(self):
        ParticipationRecord.objects.create(assessment_session=self.session, student=self.dan)
        turn = PresentationTurn.objects.create(assessment_session=self.session, group=self.red, status='closed')
        evaluation = Evaluation.objects.create(presentation_turn=turn, evaluator=self.cat)
        EvaluationScore.objects.create(evaluation=evaluation, rubric_category=self.quality, value=5)
        everyone = self.page()
        none = self.page(status='none')
        self.assertEqual(everyone.context['total'], 5)
        self.assertTrue(set(self.names(none)) < set(self.names(everyone)))
        order = self.names(everyone)
        self.assertEqual(self.names(none), [name for name in order if name in self.names(none)])

    def test_the_live_refresh_lists_them_the_same_way(self):
        data = self.client.get(self.url('assessment_participation_refresh')).json()
        html = data['cards_html']
        self.assertLess(html.index('Cat Cole'), html.index('Ann Able'))
        self.assertLess(html.index('Ann Able'), html.index('Aaron Zed'))
        self.assertIn('id="participation-cards"', html.split('>', 1)[0])

    def test_every_card_is_still_there_with_what_the_page_scripts_look_for(self):
        html = self.page().content.decode()
        for student in Student.objects.all():
            self.assertIn(f'data-student-id="{student.pk}"', html)
        self.assertEqual(html.count('class="participant-card '), 5)

    def test_the_excel_export_keeps_its_own_alphabetical_order(self):
        response = self.client.get(self.url('assessment_results_export'))
        sheet = openpyxl.load_workbook(BytesIO(response.content))['Participation']
        listed = [row[0] for row in sheet.iter_rows(min_row=2, values_only=True)]
        self.assertEqual(listed, ['Aaron Zed', 'Ann Able', 'Bob Best', 'Cat Cole', 'Dan Dyer'])

    def test_a_session_with_no_students_still_says_so(self):
        Student.objects.all().delete()
        self.assertContains(self.page(), 'No students on the roster yet.')
