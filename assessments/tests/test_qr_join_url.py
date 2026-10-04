"""A QR code generated while browsing the admin at 127.0.0.1/localhost must
encode an address a phone on the same network can actually reach — not the
loopback address, which only ever means "this device"."""

from unittest.mock import patch

from django.test import RequestFactory, TestCase

from accounts.testing import make_org
from assessments.qr import join_url
from assessments.models import AssessmentSession


class JoinUrlLanRewriteTests(TestCase):
    def setUp(self):
        self.org = make_org('Test Uni')
        self.session = AssessmentSession.objects.create(organization_id=self.org.pk, name='FYP 2026')
        self.factory = RequestFactory()

    @patch('assessments.qr.detect_lan_ip', return_value='192.168.1.42')
    def test_127_0_0_1_is_rewritten_to_lan_ip(self, mock_detect):
        request = self.factory.get('/', SERVER_NAME='127.0.0.1', SERVER_PORT='8000')
        url = join_url(request, self.session)
        self.assertTrue(url.startswith('http://192.168.1.42:8000/'))
        self.assertIn(f'/assess/{self.session.uuid}/join/', url)
        mock_detect.assert_called_once()

    @patch('assessments.qr.detect_lan_ip', return_value='192.168.1.42')
    def test_localhost_is_rewritten_to_lan_ip(self, mock_detect):
        request = self.factory.get('/', SERVER_NAME='localhost', SERVER_PORT='8000')
        url = join_url(request, self.session)
        self.assertTrue(url.startswith('http://192.168.1.42:8000/'))

    @patch('assessments.qr.detect_lan_ip')
    def test_real_lan_host_is_left_alone(self, mock_detect):
        # Already a reachable address (must be allowed by ALLOWED_HOSTS to
        # even reach join_url in real request handling) — must not call the
        # detector at all.
        with self.settings(ALLOWED_HOSTS=['192.168.1.99']):
            request = self.factory.get('/', SERVER_NAME='192.168.1.99', SERVER_PORT='8000')
            url = join_url(request, self.session)
        self.assertTrue(url.startswith('http://192.168.1.99:8000/'))
        mock_detect.assert_not_called()

    @patch('assessments.qr.detect_lan_ip')
    def test_ngrok_host_is_left_alone(self, mock_detect):
        with self.settings(ALLOWED_HOSTS=['.ngrok-free.app']):
            request = self.factory.get('/', SERVER_NAME='abcd1234.ngrok-free.app', SERVER_PORT='443')
            request.META['wsgi.url_scheme'] = 'https'
            url = join_url(request, self.session)
        self.assertTrue(url.startswith('https://abcd1234.ngrok-free.app'))
        mock_detect.assert_not_called()
