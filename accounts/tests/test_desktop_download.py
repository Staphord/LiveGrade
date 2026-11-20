"""Tests for the desktop download page and GitHub release metadata service."""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from portal.releases import (
    GITHUB_RELEASES_PAGE,
    RELEASE_CACHE_KEY,
    RELEASE_STALE_KEY,
    detect_os_from_user_agent,
    format_bytes,
    get_latest_desktop_release,
    parse_release_payload,
)


SAMPLE_PAYLOAD = {
    'tag_name': 'v1.2.3',
    'html_url': 'https://github.com/Staphord/LiveGrade/releases/tag/v1.2.3',
    'published_at': '2026-11-20T12:00:00Z',
    'body': '## Highlights\n- Universal macOS build\n- Faster desktop handoff',
    'assets': [
        {
            'name': 'LiveGrade-1.2.3-universal.dmg',
            'browser_download_url': 'https://github.com/Staphord/LiveGrade/releases/download/v1.2.3/LiveGrade-1.2.3-universal.dmg',
            'size': 150000000,
        },
        {
            'name': 'LiveGrade-1.2.3-universal.dmg.blockmap',
            'browser_download_url': 'https://github.com/Staphord/LiveGrade/releases/download/v1.2.3/LiveGrade-1.2.3-universal.dmg.blockmap',
            'size': 1024,
        },
        {
            'name': 'LiveGrade-1.2.3-universal-mac.zip',
            'browser_download_url': 'https://github.com/Staphord/LiveGrade/releases/download/v1.2.3/LiveGrade-1.2.3-universal-mac.zip',
            'size': 142000000,
        },
        {
            'name': 'LiveGrade Setup 1.2.3.exe',
            'browser_download_url': 'https://github.com/Staphord/LiveGrade/releases/download/v1.2.3/LiveGrade.Setup.1.2.3.exe',
            'size': 95000000,
        },
        {
            'name': 'LiveGrade-1.2.3.AppImage',
            'browser_download_url': 'https://github.com/Staphord/LiveGrade/releases/download/v1.2.3/LiveGrade-1.2.3.AppImage',
            'size': 110000000,
        },
        {
            'name': 'livegrade_1.2.3_amd64.deb',
            'browser_download_url': 'https://github.com/Staphord/LiveGrade/releases/download/v1.2.3/livegrade_1.2.3_amd64.deb',
            'size': 82000000,
        },
    ],
}


class DesktopReleaseServiceTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_detect_os_from_user_agent(self):
        self.assertEqual(detect_os_from_user_agent('Mozilla/5.0 (Windows NT 10.0; Win64; x64)'), 'win')
        self.assertEqual(detect_os_from_user_agent('Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)'), 'mac')
        self.assertEqual(detect_os_from_user_agent('Mozilla/5.0 (X11; Ubuntu; Linux x86_64)'), 'linux')
        self.assertEqual(detect_os_from_user_agent('Mozilla/5.0 (Linux; Android 14; Pixel 8)'), 'mac')
        self.assertEqual(detect_os_from_user_agent(''), 'mac')
        self.assertEqual(detect_os_from_user_agent(None), 'mac')

    def test_format_bytes(self):
        self.assertEqual(format_bytes(10485760), '10.0 MB')
        self.assertEqual(format_bytes(512000), '500 KB')
        self.assertEqual(format_bytes(0), '')
        self.assertEqual(format_bytes(-5), '')
        self.assertEqual(format_bytes(None), '')

    def test_parse_release_payload_matches_assets_and_ignores_blockmap(self):
        parsed = parse_release_payload(SAMPLE_PAYLOAD)
        self.assertEqual(parsed['version'], '1.2.3')
        self.assertEqual(parsed['published_date'], '2026-11-20')
        self.assertTrue(parsed['has_assets'])
        self.assertTrue(parsed['assets']['mac_dmg']['url'].endswith('.dmg'))
        self.assertTrue(parsed['assets']['mac_zip']['url'].endswith('-mac.zip'))
        self.assertTrue(parsed['assets']['win_exe']['url'].endswith('.exe'))
        self.assertTrue(parsed['assets']['linux_appimage']['url'].endswith('.AppImage'))
        self.assertTrue(parsed['assets']['linux_deb']['url'].endswith('.deb'))

    def test_parse_release_payload_rejects_non_https_urls(self):
        bad_payload = {
            'tag_name': 'v1.0.0',
            'html_url': 'javascript:alert(1)',
            'assets': [{'name': 'app.exe', 'browser_download_url': 'http://insecure.example/app.exe', 'size': 100}],
        }
        parsed = parse_release_payload(bad_payload)
        self.assertEqual(parsed['release_url'], GITHUB_RELEASES_PAGE)
        self.assertFalse(parsed['assets']['win_exe']['available'])
        self.assertEqual(parsed['assets']['win_exe']['url'], GITHUB_RELEASES_PAGE)

    def test_get_latest_desktop_release_uses_cache_and_stale_fallback(self):
        with mock.patch('portal.releases.fetch_latest_release_payload', return_value=SAMPLE_PAYLOAD) as fetch_mock:
            first = get_latest_desktop_release()
            second = get_latest_desktop_release()
        self.assertEqual(first['version'], '1.2.3')
        self.assertEqual(second['version'], '1.2.3')
        fetch_mock.assert_called_once()

        cache.delete(RELEASE_CACHE_KEY)
        with mock.patch('portal.releases.fetch_latest_release_payload', side_effect=OSError('offline')):
            stale = get_latest_desktop_release()
        self.assertEqual(stale['version'], '1.2.3')

        cache.delete(RELEASE_STALE_KEY)
        with mock.patch('portal.releases.fetch_latest_release_payload', side_effect=OSError('offline')):
            fallback = get_latest_desktop_release()
        self.assertEqual(fallback['version'], '1.0.0')
        self.assertFalse(fallback['has_assets'])
        self.assertEqual(fallback['release_url'], GITHUB_RELEASES_PAGE)


class DesktopDownloadViewTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_download_page_highlights_windows_for_windows_user_agent(self):
        with mock.patch('portal.releases.fetch_latest_release_payload', return_value=SAMPLE_PAYLOAD):
            resp = self.client.get(
                reverse('desktop_download'),
                HTTP_USER_AGENT='Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'LiveGrade for Windows')
        self.assertContains(resp, 'Download (.exe)')
        self.assertContains(resp, 'Version 1.2.3')
        self.assertContains(resp, 'href="#release-notes"')
        self.assertContains(resp, 'Universal macOS build')
        self.assertNotContains(resp, 'All assets on GitHub')

    def test_download_page_highlights_macos_for_mac_user_agent(self):
        with mock.patch('portal.releases.fetch_latest_release_payload', return_value=SAMPLE_PAYLOAD):
            resp = self.client.get(
                reverse('desktop_download'),
                HTTP_USER_AGENT='Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)',
            )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'LiveGrade for macOS')
        self.assertContains(resp, 'Download (.dmg)')

    def test_download_page_highlights_linux_for_linux_user_agent(self):
        with mock.patch('portal.releases.fetch_latest_release_payload', return_value=SAMPLE_PAYLOAD):
            resp = self.client.get(
                reverse('desktop_download'),
                HTTP_USER_AGENT='Mozilla/5.0 (X11; Linux x86_64)',
            )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'LiveGrade for Linux')
        self.assertContains(resp, 'Download (.AppImage)')

    def test_landing_hides_desktop_app_link_inside_desktop_client(self):
        browser_resp = self.client.get(
            reverse('home'),
            HTTP_USER_AGENT='Mozilla/5.0 Chrome/130.0.0.0',
        )
        self.assertContains(browser_resp, reverse('desktop_download'))

        desktop_resp = self.client.get(
            reverse('home'),
            HTTP_USER_AGENT='Mozilla/5.0 LiveGradeDesktop/1.0.0',
        )
        self.assertNotContains(desktop_resp, reverse('desktop_download'))

