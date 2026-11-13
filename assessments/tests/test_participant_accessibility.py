"""PE-04: structural participant-page accessibility regressions."""
from datetime import timedelta
import gzip
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import re

from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from assessments.models import PresentationTurn, PrivacyNotice
from assessments.tests.test_edges_core import SessionFixture
from assessments.tests.participant_flow import join_as


class _RenderedMarkupAudit(HTMLParser):
    """Dependency-free checks for common accessibility defects in rendered HTML."""

    VOID_ELEMENTS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []
        self.duplicate_ids = set()
        self.ids = {}
        self.wrapped_labels = {}
        self.feed(markup)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        node = {'tag': tag, 'attrs': attrs, 'text': ''}
        element_id = attrs.get('id')
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.add(element_id)
            self.ids[element_id] = node
        self.elements.append(node)
        if tag in {'input', 'select', 'textarea'} and attrs.get('id'):
            for ancestor in self.stack:
                if ancestor['tag'] == 'label':
                    self.wrapped_labels[attrs['id']] = ancestor
        if tag not in self.VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]['tag'] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for node in self.stack:
            node['text'] += data

    def _name(self, node):
        attrs = node['attrs']
        if attrs.get('aria-label', '').strip():
            return True
        labelled_by = attrs.get('aria-labelledby', '').split()
        if labelled_by and any(self.ids.get(item, {}).get('text', '').strip() for item in labelled_by):
            return True
        if node['tag'] in {'input', 'select', 'textarea'}:
            element_id = attrs.get('id')
            if element_id and self.wrapped_labels.get(element_id, {}).get('text', '').strip():
                return True
            if element_id and any(label['attrs'].get('for') == element_id and label['text'].strip()
                                  for label in self.elements if label['tag'] == 'label'):
                return True
        if node['tag'] == 'img':
            return 'alt' in attrs
        return bool(node['text'].strip())

    def problems(self):
        problems = []
        html = next((node for node in self.elements if node['tag'] == 'html'), None)
        if not html or not html['attrs'].get('lang'):
            problems.append('document has no language')
        if len([node for node in self.elements if node['tag'] == 'main']) != 1:
            problems.append('document must have exactly one main landmark')
        if self.duplicate_ids:
            problems.append(f'duplicate IDs: {sorted(self.duplicate_ids)}')
        for node in self.elements:
            attrs = node['attrs']
            for attribute in ('aria-labelledby', 'aria-describedby', 'aria-controls', 'aria-errormessage'):
                missing = [reference for reference in attrs.get(attribute, '').split() if reference not in self.ids]
                if missing:
                    problems.append(f'{attribute} references missing IDs: {missing}')
            if node['tag'] == 'label' and attrs.get('for'):
                target = self.ids.get(attrs['for'])
                if not target or target['tag'] not in {'input', 'select', 'textarea'}:
                    problems.append(f'label target is missing or not a control: {attrs["for"]}')
            if node['tag'] == 'a' and attrs.get('href') and not self._name(node):
                problems.append(f'unnamed link: {attrs.get("href")}')
            if node['tag'] == 'img' and not self._name(node):
                problems.append(f'image lacks alt text: {attrs.get("src", "")}' )
            if node['tag'] in {'button', 'input', 'select', 'textarea'}:
                if node['tag'] == 'input' and attrs.get('type', '').lower() in {'hidden', 'submit', 'button', 'reset', 'image'}:
                    if node['tag'] != 'input' or attrs.get('type', '').lower() == 'image':
                        if not self._name(node):
                            problems.append(f'unnamed {node["tag"]}: {attrs}')
                    continue
                if not self._name(node):
                    problems.append(f'unnamed {node["tag"]}: {attrs}')
            if attrs.get('role') in {'timer', 'progressbar'} and not self._name(node):
                problems.append(f'unnamed {attrs["role"]} role')
            if attrs.get('href', '').startswith('#') and len(attrs['href']) > 1 and attrs['href'][1:] not in self.ids:
                problems.append(f'link target is missing: {attrs["href"]}')
        return problems


class ParticipantAccessibilityTests(SessionFixture):
    def _compressed_first_load_estimate(self, response):
        """Count compressed HTML, local static files/fonts, and same-origin images."""
        markup = _RenderedMarkupAudit(response.content.decode())
        static_prefix = '/' + settings.STATIC_URL.lstrip('/')
        pending = []
        for node in markup.elements:
            attrs = node['attrs']
            if node['tag'] == 'link' and 'stylesheet' in attrs.get('rel', '').split():
                pending.append(attrs.get('href', ''))
            elif node['tag'] == 'script' and attrs.get('src'):
                pending.append(attrs['src'])
            elif node['tag'] in {'img', 'source'} and attrs.get('src'):
                pending.append(attrs['src'])

        loaded = {}
        dynamic_bytes = 0
        while pending:
            url = pending.pop()
            if not url or url.startswith(('data:', '#')):
                continue
            resolved = urljoin('http://testserver/', url)
            parsed = urlsplit(resolved)
            self.assertIn(parsed.netloc, ('', 'testserver'),
                          f'participant page has a remote first-load asset: {url}')
            path = parsed.path
            if not path.startswith(static_prefix):
                asset_response = self.client.get(path)
                self.assertEqual(asset_response.status_code, 200,
                                 f'participant first-load asset failed: {path}')
                dynamic_bytes += len(asset_response.content)
                continue
            relative = path[len(static_prefix):]
            if relative in loaded:
                continue
            asset_path = finders.find(relative)
            self.assertTrue(asset_path, f'missing participant asset: {relative}')
            data = Path(asset_path).read_bytes()
            loaded[relative] = data
            if relative.endswith('.css'):
                css_url = resolved
                for reference in re.findall(r'url\([\"\']?([^\"\')]+)', data.decode()):
                    reference = reference.strip()
                    if reference.startswith(('data:', '#')) or '.woff2' not in reference:
                        continue
                    pending.append(urljoin(css_url, reference))

        html_bytes = len(gzip.compress(response.content, compresslevel=9, mtime=0))
        asset_bytes = sum(len(gzip.compress(data, compresslevel=9, mtime=0))
                          for data in loaded.values())
        return html_bytes + asset_bytes + dynamic_bytes

    @staticmethod
    def _contrast_ratio(foreground, background):
        def luminance(color):
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                      for value in channels]
            return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    def test_join_form_has_zoom_support_associated_label_and_field_error(self):
        page = self.client.get(reverse('student_join', args=[self.session.uuid]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '<html lang="en" data-livegrade-participant>', html=False)
        self.assertContains(page, 'content="width=device-width, initial-scale=1.0"', html=False)
        self.assertNotContains(page, 'maximum-scale')
        self.assertContains(page, '<label class="form-label fw-600 s-label" for="id_identifier">', html=False)
        self.assertContains(page, 'id="identifier-help"')

        invalid = self.client.post(reverse('student_join', args=[self.session.uuid]), {'identifier': ''})
        self.assertContains(invalid, 'aria-invalid="true"')
        self.assertContains(invalid, 'aria-describedby="identifier-help identifier-errors"')
        self.assertContains(invalid, 'id="identifier-errors" role="alert" aria-live="assertive"', html=False)

    def test_scoring_controls_have_contextual_names_error_links_and_progress_semantics(self):
        self.session.status = 'live'
        self.session.save(update_fields=['status'])
        PresentationTurn.objects.create(
            assessment_session=self.session, group=self.red, status=PresentationTurn.Status.ACTIVE,
            voting_ends_at=timezone.now() + timedelta(minutes=1))
        participant = Client()
        join_as(participant, self.session.uuid, self.cat.student_id)
        page = participant.get(reverse('student_evaluate', args=[self.session.uuid]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'<label class="category-name" for="group_{self.quality.pk}">', html=False)
        self.assertContains(page, f'aria-label="Adjust {self.quality.name} score for {self.red.name} with slider"', html=False)
        self.assertContains(page, f'id="group-error-{self.quality.pk}" role="status" aria-live="polite"', html=False)
        self.assertContains(page, f'aria-label="Adjust {self.teamwork.name} score for {self.ann.full_name} with slider"', html=False)
        self.assertContains(page, 'role="progressbar" aria-label="Scoring progress"')
        self.assertContains(page, 'role="timer" aria-label="Voting closes in" aria-live="off"', html=False)
        self.assertContains(page, 'id="timer-live-status" class="visually-hidden" role="status" aria-live="polite"', html=False)

    def test_shared_participant_shell_supports_reduced_motion_dark_mode_and_large_targets(self):
        page = self.client.get(reverse('student_join', args=[self.session.uuid]))
        self.assertContains(page, '@media (prefers-reduced-motion:reduce)', html=False)
        self.assertContains(page, '@media (prefers-color-scheme:dark)', html=False)
        self.assertContains(page, 'min-height:44px', html=False)

    def test_participant_pages_use_pinned_local_framework_assets(self):
        local_assets = (
            'vendor/bootstrap/5.3.0/css/bootstrap.min.css',
            'vendor/bootstrap/5.3.0/js/bootstrap.bundle.min.js',
            'vendor/bootstrap-icons/1.11.0/font/bootstrap-icons.css',
            'vendor/bootstrap-icons/1.11.0/font/fonts/bootstrap-icons.woff2',
            'vendor/bootstrap-icons/1.11.0/font/fonts/bootstrap-icons.woff',
        )
        for asset in local_assets:
            with self.subTest(asset=asset):
                self.assertTrue(finders.find(asset), f'missing local static asset: {asset}')

        pages = (
            self.client.get(reverse('student_join', args=[self.session.uuid])),
            self.client.get(reverse('assessment_join_screen', args=[self.session.uuid])),
        )
        for page in pages:
            markup = page.content.decode()
            self.assertIn('/static/vendor/bootstrap/5.3.0/css/bootstrap.min.css', markup)
            self.assertIn('/static/vendor/bootstrap-icons/1.11.0/font/bootstrap-icons.css', markup)
            self.assertNotIn('cdn.jsdelivr.net/npm/bootstrap@', markup)
            self.assertNotIn('cdn.jsdelivr.net/npm/bootstrap-icons@', markup)

        css_path = finders.find(local_assets[2])
        css = Path(css_path).read_text()
        self.assertIn('url("./fonts/bootstrap-icons.woff2?', css)

    def test_participant_first_load_gzip_estimate_stays_under_500_kb(self):
        join_url = reverse('student_join', args=[self.session.uuid])
        room_url = reverse('assessment_join_screen', args=[self.session.uuid])
        pages = [self.client.get(join_url), self.client.get(room_url)]

        self.session.status = 'live'
        self.session.save(update_fields=['status'])
        turn = PresentationTurn.objects.create(
            assessment_session=self.session, group=self.red,
            status=PresentationTurn.Status.ACTIVE, opened_at=timezone.now(),
            voting_opened_at=timezone.now(), voting_ends_at=timezone.now() + timedelta(minutes=1),
        )
        PrivacyNotice.objects.filter(service='classroom').update(is_active=False)
        PrivacyNotice.objects.create(
            service='classroom', version='budget-1', title='Privacy notice',
            body='How participant data is used.', is_active=True,
        )
        participant = Client()
        participant.get(join_url)
        participant.post(join_url, {'identifier': self.cat.student_id})
        notice_url = reverse('student_privacy_notice', args=[self.session.uuid])
        pages.append(participant.get(notice_url))
        participant.post(notice_url, {'action': 'continue'})
        scoring_url = reverse('student_evaluate', args=[self.session.uuid])
        pages.append(participant.get(scoring_url))
        receipt = participant.post(scoring_url, {
            'turn_id': str(turn.pk),
            f'group_{self.quality.pk}': '30',
            f'ind_{self.ann.pk}_{self.teamwork.pk}': '20',
            f'ind_{self.bob.pk}_{self.teamwork.pk}': '20',
        }, follow=True)
        self.assertEqual(receipt.status_code, 200)
        pages.append(receipt)

        for page in pages:
            with self.subTest(path=page.request['PATH_INFO']):
                self.assertEqual(page.status_code, 200)
                self.assertLess(
                    self._compressed_first_load_estimate(page), 500_000,
                    'participant HTML and its local compressed CSS/JS/WOFF2 resources exceed 500 KB',
                )

    def test_participant_shell_has_skip_link_and_unobscured_main_landmark(self):
        page = self.client.get(reverse('student_join', args=[self.session.uuid]))
        self.assertContains(page, '<a class="participant-skip-link" href="#participant-main">Skip to main content</a>', html=False)
        self.assertContains(page, '<main class="student-shell" id="participant-main" tabindex="-1">', html=False)

    def test_small_white_text_uses_opaque_contrast_safe_surfaces(self):
        page = self.client.get(reverse('student_join', args=[self.session.uuid]))
        self.assertEqual(page.status_code, 200)
        badge = re.search(r'\.status-badge\.on-hero\{background:(#[0-9a-fA-F]{6});color:#fff', page.content.decode())
        self.assertIsNotNone(badge)
        self.assertGreaterEqual(self._contrast_ratio('#ffffff', badge.group(1)), 4.5)

        room = self.client.get(reverse('assessment_join_screen', args=[self.session.uuid]))
        room_css = room.content.decode()
        self.assertContains(room, '<a class="join-skip-link" href="#public-room-main">Skip to main content</a>', html=False)
        self.assertContains(room, '<main class="join-shell" id="public-room-main" tabindex="-1">', html=False)
        self.assertIn('.join-eyebrow{color:#fff;', room_css)
        self.assertIn('.join-footer{text-align:center;color:#fff;', room_css)
        for brand_color in ('#1d68b1', '#2b75bb'):
            self.assertGreaterEqual(self._contrast_ratio('#ffffff', brand_color), 4.5)

    def test_projected_join_screen_has_accessible_timer_and_participant_theme(self):
        self.session.status = 'live'
        self.session.save(update_fields=['status'])
        page = self.client.get(reverse('assessment_join_screen', args=[self.session.uuid]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '<html lang="en" data-livegrade-participant>', html=False)
        self.assertContains(page, 'id="public-countdown" class="room-timer" role="timer" aria-label="Class progress timer" aria-live="off"', html=False)
        self.assertContains(page, 'id="room-timer-live-status" class="visually-hidden" role="status" aria-live="polite"', html=False)
        self.assertIn('.room-leaderboard{margin:1rem auto 0;padding:1.5rem;background:var(--surface);', page.content.decode())
        self.assertIn('.room-leaderboard thead{position:sticky;top:0;background:var(--surface)}', page.content.decode())

    def test_rendered_participant_pages_pass_accessibility_smoke_audit(self):
        join_url = reverse('student_join', args=[self.session.uuid])
        room_url = reverse('assessment_join_screen', args=[self.session.uuid])
        pages = [self.client.get(join_url), self.client.get(room_url)]
        invalid_join = self.client.post(join_url, {'identifier': 'not-on-the-roster'})
        self.assertEqual(invalid_join.status_code, 200)
        pages.append(invalid_join)

        participant = Client()
        participant.get(join_url)
        participant.post(join_url, {'identifier': self.cat.student_id})
        pages.append(participant.get(reverse('student_privacy_notice', args=[self.session.uuid])))
        participant.post(reverse('student_privacy_notice', args=[self.session.uuid]), {'action': 'continue'})

        self.session.status = 'live'
        self.session.save(update_fields=['status'])
        PresentationTurn.objects.create(
            assessment_session=self.session, group=self.red, status=PresentationTurn.Status.ACTIVE,
            voting_ends_at=timezone.now() + timedelta(minutes=1))
        pages.append(self.client.get(room_url))
        self.session.joining_locked = True
        self.session.save(update_fields=['joining_locked'])
        locked_join = Client().get(join_url)
        self.assertContains(locked_join, 'Joining is closed')
        self.assertNotContains(locked_join, 'id="join-form"')
        pages.extend((locked_join, self.client.get(room_url)))
        self.session.joining_locked = False
        self.session.save(update_fields=['joining_locked'])

        scoring_page = participant.get(reverse('student_evaluate', args=[self.session.uuid]))
        self.assertEqual(scoring_page.status_code, 200)
        pages.append(scoring_page)
        submitted = participant.post(reverse('student_evaluate', args=[self.session.uuid]), {
            f'group_{self.quality.pk}': '30',
            f'ind_{self.ann.pk}_{self.teamwork.pk}': '20',
            f'ind_{self.bob.pk}_{self.teamwork.pk}': '20',
        }, follow=True)
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(submitted.redirect_chain[-1][0], reverse('student_submitted', args=[self.session.uuid]))
        pages.append(submitted)
        self.session.status = 'closed'
        self.session.save(update_fields=['status'])
        pages.extend((participant.get(reverse('student_evaluate', args=[self.session.uuid])),
                      self.client.get(room_url)))

        for page in pages:
            self.assertEqual(page.status_code, 200)
            audit = _RenderedMarkupAudit(page.content.decode())
            self.assertEqual(audit.problems(), [])
