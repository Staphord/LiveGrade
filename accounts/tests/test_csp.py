"""The Content-Security-Policy, the nonce on inline scripts, and SRI on third-party files."""
import re
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings

from accounts.context import livegrade
from accounts.csp import PERMISSIONS_POLICY, ContentSecurityPolicyMiddleware, build_policy

BASE = Path(settings.BASE_DIR)
REPORT_ONLY, ENFORCED = 'Content-Security-Policy-Report-Only', 'Content-Security-Policy'
PAGE = '/'   # the sign-in landing page: it has inline scripts of its own


def directive(policy, name):
    return next(part.split(' ', 1)[1] for part in policy.split('; ') if part.startswith(name + ' '))


class PolicyHeaderTests(TestCase):

    def test_it_is_report_only_unless_enforcement_is_switched_on(self):
        response = Client().get(PAGE)
        self.assertIn(REPORT_ONLY, response)
        self.assertNotIn(ENFORCED, response)

    @override_settings(CSP_ENFORCE=True)
    def test_it_is_enforced_when_switched_on(self):
        response = Client().get(PAGE)
        self.assertIn(ENFORCED, response)
        self.assertNotIn(REPORT_ONLY, response)

    def test_every_response_carries_it_not_only_pages(self):
        for url in ('/healthz', '/definitely/not/a/page/'):
            with self.subTest(url=url):
                self.assertIn(REPORT_ONLY, Client().get(url))

    def test_scripts_may_only_come_from_this_site_the_cdn_and_this_response_nonce(self):
        response = Client().get(PAGE)
        nonce = response.wsgi_request.csp_nonce
        scripts = directive(response[REPORT_ONLY], 'script-src')
        self.assertEqual(scripts, f"'self' 'nonce-{nonce}' https://cdn.jsdelivr.net")

    def test_nothing_may_run_inline_or_through_eval(self):
        policy = Client().get(PAGE)[REPORT_ONLY]
        self.assertNotIn("'unsafe-eval'", policy)
        self.assertNotIn("'unsafe-inline'", directive(policy, 'script-src'))
        self.assertNotIn("'unsafe-inline'", directive(policy, 'default-src'))

    def test_style_blocks_need_the_nonce_and_only_style_attributes_may_be_inline(self):
        response = Client().get(PAGE)
        nonce = response.wsgi_request.csp_nonce
        policy = response[REPORT_ONLY]
        for name in ('style-src', 'style-src-elem'):
            with self.subTest(directive=name):
                self.assertNotIn("'unsafe-inline'", directive(policy, name))
                self.assertIn(f"'nonce-{nonce}'", directive(policy, name))
        self.assertEqual(directive(policy, 'style-src-attr'), "'unsafe-inline'")

    def test_no_font_or_style_host_is_allowed_beyond_the_cdn(self):
        policy = Client().get(PAGE)[REPORT_ONLY]
        self.assertNotIn('google', policy)
        self.assertEqual(directive(policy, 'font-src'), "'self' https://cdn.jsdelivr.net")

    def test_the_page_cannot_be_framed_or_given_a_plugin_or_a_new_base(self):
        policy = Client().get(PAGE)[REPORT_ONLY]
        self.assertEqual(directive(policy, 'frame-ancestors'), "'none'")
        self.assertEqual(directive(policy, 'object-src'), "'none'")
        self.assertEqual(directive(policy, 'base-uri'), "'self'")
        self.assertEqual(directive(policy, 'default-src'), "'self'")

    def test_forms_may_only_go_to_this_site_and_devperf_for_signing_out(self):
        policy = Client().get(PAGE)[REPORT_ONLY]
        self.assertEqual(directive(policy, 'form-action'), f"'self' {settings.DEVPERF_URL}")

    def test_the_live_socket_is_allowed_back_to_the_host_that_served_the_page(self):
        plain = directive(Client().get(PAGE)[REPORT_ONLY], 'connect-src')
        secure = directive(Client().get(PAGE, secure=True)[REPORT_ONLY], 'connect-src')
        self.assertEqual(plain, "'self' ws://testserver")
        self.assertEqual(secure, "'self' wss://testserver")

    def test_a_refused_host_still_gets_a_response_with_the_policy(self):
        response = Client().get(PAGE, HTTP_HOST='evil.example')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(directive(response[REPORT_ONLY], 'connect-src'), "'self'")

    def test_browser_features_the_app_never_uses_are_switched_off(self):
        response = Client().get(PAGE)
        self.assertEqual(response['Permissions-Policy'], PERMISSIONS_POLICY)
        self.assertIn('camera=()', PERMISSIONS_POLICY)
        self.assertIn('microphone=()', PERMISSIONS_POLICY)

    def test_a_policy_a_view_set_itself_is_left_alone(self):
        def view(request):
            response = HttpResponse('ok')
            response[REPORT_ONLY] = 'default-src none'
            response['Permissions-Policy'] = 'fullscreen=()'
            return response

        response = ContentSecurityPolicyMiddleware(view)(RequestFactory().get('/'))
        self.assertEqual(response[REPORT_ONLY], 'default-src none')
        self.assertEqual(response['Permissions-Policy'], 'fullscreen=()')

    def test_build_policy_without_a_host_names_no_socket(self):
        self.assertIn("connect-src 'self';", build_policy('n', '', True))


class NonceTests(TestCase):

    def test_the_inline_scripts_of_a_page_carry_the_nonce_in_the_header(self):
        response = Client().get(PAGE)
        nonce = response.wsgi_request.csp_nonce
        html = response.content.decode()
        inline = re.findall(r'<script(?![^>]*\bsrc=)([^>]*)>', html, re.IGNORECASE)
        self.assertTrue(inline, 'the landing page should have an inline script')
        for attributes in inline:
            self.assertIn(f'nonce="{nonce}"', attributes)
        self.assertIn(f"'nonce-{nonce}'", response[REPORT_ONLY])

    def test_every_response_gets_a_new_nonce(self):
        nonces = {Client().get(PAGE).wsgi_request.csp_nonce for _ in range(5)}
        self.assertEqual(len(nonces), 5)
        self.assertTrue(all(len(nonce) >= 16 for nonce in nonces))

    def test_the_context_processor_is_empty_when_the_middleware_has_not_run(self):
        request = RequestFactory().get('/')
        request.session = {}
        self.assertEqual(livegrade(request)['csp_nonce'], '')


class TemplateRulesTests(SimpleTestCase):
    """Rules the policy depends on; a template that breaks one is blocked by the browser."""

    def templates(self):
        return sorted((BASE / 'templates').rglob('*.html'))

    def test_every_inline_script_has_the_nonce(self):
        missing = []
        for path in self.templates():
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                for tag in re.findall(r'<script\b[^>]*>', line, re.IGNORECASE):
                    if 'src=' not in tag and 'nonce="{{ csp_nonce }}"' not in tag:
                        missing.append(f'{path.relative_to(BASE)}:{number}')
        self.assertEqual(missing, [])

    def test_every_style_block_has_the_nonce(self):
        missing = [f'{path.relative_to(BASE)}:{number}'
                   for path in self.templates()
                   for number, line in enumerate(path.read_text().splitlines(), start=1)
                   for tag in re.findall(r'<style\b[^>]*>', line, re.IGNORECASE)
                   if 'nonce="{{ csp_nonce }}"' not in tag]
        self.assertEqual(missing, [])

    def test_no_template_uses_an_inline_event_handler(self):
        # onclick="..." and friends are inline script; the policy would refuse them.
        used = [f'{path.relative_to(BASE)}:{number}'
                for path in self.templates()
                for number, line in enumerate(path.read_text().splitlines(), start=1)
                if re.search(r'\son[a-z]+\s*=\s*["\']', line)]
        self.assertEqual(used, [])

    def test_no_template_links_to_a_javascript_url(self):
        used = [str(path.relative_to(BASE)) for path in self.templates()
                if re.search(r'(href|src|action)\s*=\s*["\']\s*javascript:', path.read_text(), re.I)]
        self.assertEqual(used, [])

    def test_every_file_loaded_from_the_cdn_is_pinned_with_an_integrity_hash(self):
        unpinned = []
        for path in self.templates():
            for tag in re.findall(r'<(?:script|link)\b[^>]*cdn\.jsdelivr\.net[^>]*>', path.read_text(), re.IGNORECASE):
                if not re.search(r'integrity="sha384-[A-Za-z0-9+/]{64}"', tag) \
                        or 'crossorigin="anonymous"' not in tag \
                        or not re.search(r'@\d+\.\d+\.\d+/', tag):
                    unpinned.append(f'{path.relative_to(BASE)}: {tag}')
        self.assertEqual(unpinned, [])

    def test_only_the_cdn_is_ever_loaded_from_elsewhere(self):
        allowed = ('https://cdn.jsdelivr.net',)
        outside = []
        for path in self.templates():
            for tag in re.findall(r'<(?:script|link|img|iframe)\b[^>]*(?:src|href)="https?://[^"]+"[^>]*>', path.read_text(), re.IGNORECASE):
                url = re.search(r'(?:src|href)="(https?://[^"]+)"', tag).group(1)
                if not url.startswith(allowed):
                    outside.append(f'{path.relative_to(BASE)}: {url}')
        self.assertEqual(outside, [])
