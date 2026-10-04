"""A template comment must never reach the page (see DevPerf's test of the same name)."""
import re
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse

from accounts.testing import make_org, make_user, sign_in
from assessments.models import (AssessmentSession, GroupMembership, PresentationGroup,
                                RubricCategory, Student)

COMMENT = re.compile(r'\{%\s*comment[^%]*%\}(.*?)\{%\s*endcomment\s*%\}|\{#([^\n]*?)#\}', re.S)


def comment_sentences():
    found = []
    for path in sorted((Path(settings.BASE_DIR) / 'templates').rglob('*.html')):
        for match in COMMENT.finditer(path.read_text(errors='ignore')):
            body = match.group(1) or match.group(2) or ''
            for sentence in re.split(r'[.\n]', body):
                sentence = ' '.join(sentence.split())
                if len(sentence) >= 25 and len(re.findall(r'[A-Za-z]{3,}', sentence)) >= 3:
                    found.append((str(path.relative_to(settings.BASE_DIR)), sentence))
    return found


class NoCommentLeakTests(TestCase):

    def setUp(self):
        self.org = make_org('Uni', oversee=True)
        self.ann = make_user('ann', first_name='Ann', last_name='Owner')
        sign_in(self.client, self.ann, self.org)
        self.session = AssessmentSession.objects.create(
            organization_id=self.org.pk, name='Demo', created_by=self.ann, status='live')
        self.student = Student.objects.create(assessment_session=self.session, full_name='Stu Dent', student_id='S1')
        group = PresentationGroup.objects.create(assessment_session=self.session, name='Red', order=1)
        GroupMembership.objects.create(group=group, student=self.student)
        RubricCategory.objects.create(assessment_session=self.session, name='Q', scope='group', max_points=10, weight=60)

    def lecturer_pages(self):
        pk = self.session.pk
        for url in ('/', '/no-access/', reverse('assessment_session_list'), reverse('assessment_session_list') + '?scope=all',
                    reverse('assessment_session_create'), reverse('assessment_session_detail', args=[pk]),
                    reverse('assessment_roster', args=[pk]), reverse('assessment_rubric', args=[pk]),
                    reverse('assessment_groups', args=[pk]), reverse('assessment_participation', args=[pk]),
                    reverse('assessment_results', args=[pk]), reverse('assessment_transfer', args=[pk])):
            yield url, self.client.get(url, follow=True)

    def public_pages(self):
        visitor = Client()
        yield '/', visitor.get('/')
        uuid = self.session.uuid
        for name in ('assessment_join_screen', 'student_join'):
            yield name, visitor.get(reverse(name, args=[uuid]), follow=True)

    def test_no_comment_sentence_appears_in_any_page(self):
        sentences = comment_sentences()
        self.assertGreater(len(sentences), 5)
        leaks = []
        for url, response in [*self.lecturer_pages(), *self.public_pages()]:
            html = ' '.join(response.content.decode().split())
            for path, sentence in sentences:
                if sentence in html:
                    leaks.append(f'{url}: "{sentence[:60]}" (from {path})')
        self.assertEqual(leaks, [])
