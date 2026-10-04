"""Properties of the whole service that must stay true as it grows."""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import URLResolver, get_resolver

BASE = Path(settings.BASE_DIR)


def every_pattern(resolver=None):
    for pattern in (resolver or get_resolver()).url_patterns:
        if isinstance(pattern, URLResolver):
            yield from every_pattern(pattern)
        else:
            yield pattern


class CsrfTests(SimpleTestCase):

    def test_nothing_is_exempt_from_csrf(self):
        exempt = sorted(str(p.pattern) for p in every_pattern()
                        if getattr(p.callback, 'csrf_exempt', False))
        self.assertEqual(exempt, [])

    def test_the_csrf_middleware_is_installed(self):
        self.assertIn('django.middleware.csrf.CsrfViewMiddleware', settings.MIDDLEWARE)


class TemplateSafetyTests(SimpleTestCase):

    def template_files(self):
        return sorted((BASE / 'templates').rglob('*.html'))

    def test_the_safe_filter_is_used_nowhere(self):
        uses = [str(p.relative_to(BASE)) for p in self.template_files()
                if re.search(r'\|\s*safe\b', p.read_text())]
        self.assertEqual(uses, [])

    def test_data_goes_into_scripts_as_json_never_as_text(self):
        for path in self.template_files():
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if re.search(r'=\s*\{\{[^}]*\|\s*safe', line):
                    self.fail(f'{path.relative_to(BASE)}:{number} puts data in a script with |safe')

    def test_a_group_name_is_escaped_before_it_is_put_in_the_page_by_script(self):
        template = (BASE / 'templates/assessments/_partials/live_groups.html').read_text()
        self.assertIn('safeName.textContent = name', template)
        self.assertNotIn("+ name +", template)

    def test_pages_forbid_framing_and_sniffing(self):
        response = self.client.get('/no-access/')
        self.assertEqual(response['X-Frame-Options'], 'DENY')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

    def test_the_session_cookie_cannot_be_read_by_scripts(self):
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
