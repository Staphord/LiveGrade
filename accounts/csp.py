"""Content-Security-Policy and Permissions-Policy for every response.

The policy is what limits the damage of a cross-site-scripting mistake: even if markup
reaches a page unescaped, the browser refuses to run a script that does not carry this
response's nonce, refuses scripts from hosts that are not listed, and refuses to be framed
or to send a form anywhere unexpected.

Inline scripts in the templates carry ``nonce="{{ csp_nonce }}"`` (a test enforces it).
Inline *styles* are still allowed (``style-src 'unsafe-inline'``): the templates use style
attributes in many places and those cannot carry a nonce. Styles cannot run code, so this is
the usual trade-off.

``CSP_ENFORCE`` (settings) switches between ``Content-Security-Policy-Report-Only``, where the
browser only logs violations to its console, and enforcement. It defaults to report-only so a
page that was missed is visible in the console instead of broken in front of a class; turn
enforcement on once the pages have been opened with the console clear.
"""
import secrets

from django.conf import settings
from django.core.exceptions import DisallowedHost

#: Where scripts, styles and fonts may come from besides this site. Matches base.html.
CDN = 'https://cdn.jsdelivr.net'
FONT_STYLES = 'https://fonts.googleapis.com'
FONT_FILES = 'https://fonts.gstatic.com'

PERMISSIONS_POLICY = ('accelerometer=(), camera=(), geolocation=(), gyroscope=(), '
                      'magnetometer=(), microphone=(), payment=(), usb=()')


def build_policy(nonce, host, secure):
    """The policy text for one response.

    ``host`` is the host the page was served from: the live-update WebSocket connects back to
    it, and Safari does not treat ``'self'`` as covering ``ws:``/``wss:``.
    """
    socket = f"{'wss' if secure else 'ws'}://{host}" if host else ''
    directives = [
        ("default-src", "'self'"),
        ("script-src", f"'self' 'nonce-{nonce}' {CDN}"),
        ("style-src", f"'self' 'unsafe-inline' {CDN} {FONT_STYLES}"),
        ("font-src", f"'self' {CDN} {FONT_FILES}"),
        ("img-src", "'self' data: blob:"),
        ("connect-src", f"'self' {socket}".rstrip()),
        # Signing out is a form post that is redirected on to DevPerf; Chrome applies
        # form-action to that redirect too.
        ("form-action", f"'self' {settings.DEVPERF_URL}"),
        ("frame-ancestors", "'none'"),
        ("base-uri", "'self'"),
        ("object-src", "'none'"),
    ]
    return '; '.join(f'{name} {value}' for name, value in directives)


class ContentSecurityPolicyMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # A fresh, unguessable value for every response; templates read it as {{ csp_nonce }}.
        request.csp_nonce = secrets.token_urlsafe(16)
        response = self.get_response(request)
        header = ('Content-Security-Policy' if settings.CSP_ENFORCE
                  else 'Content-Security-Policy-Report-Only')
        if header not in response:
            try:
                host = request.get_host()
            except DisallowedHost:
                host = ''   # the request is being refused anyway; no socket to allow
            response[header] = build_policy(request.csp_nonce, host, request.is_secure())
        if 'Permissions-Policy' not in response:
            response['Permissions-Policy'] = PERMISSIONS_POLICY
        return response
