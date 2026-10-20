"""Content-Security-Policy and Permissions-Policy for every response.

The policy is what limits the damage of a cross-site-scripting mistake: even if markup
reaches a page unescaped, the browser refuses to run a script that does not carry this
response's nonce, refuses scripts from hosts that are not listed, and refuses to be framed
or to send a form anywhere unexpected.

Inline scripts in the templates carry ``nonce="{{ csp_nonce }}"`` (a test enforces it).
Style *blocks* carry the nonce too (``style-src-elem``). Style *attributes* (``style="..."``,
about fifty in the templates) cannot carry a nonce, so only ``style-src-attr`` allows them
inline; that is the narrowest way to keep them without letting ``<style>`` blocks in from
anywhere. Styles cannot run code, so this is the usual trade-off. ``style-src`` repeats the
element rule for browsers that predate the two newer directives.

``CSP_ENFORCE`` (settings) switches between ``Content-Security-Policy``, which the browser obeys, and
``Content-Security-Policy-Report-Only``, where it only logs violations to its console. It is on whenever
DEBUG is off and off while developing; setting it to False in production is the emergency way back to
report-only if a page is ever found to depend on something the policy forbids.
"""
import secrets

from django.conf import settings
from django.core.exceptions import DisallowedHost

#: Where scripts, styles and fonts may come from besides this site. Matches base.html. The Inter
#: font is served from static/fonts, so no font host is needed.
CDN = 'https://cdn.jsdelivr.net'

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
        ("style-src", f"'self' 'nonce-{nonce}' {CDN}"),
        ("style-src-elem", f"'self' 'nonce-{nonce}' {CDN}"),
        ("style-src-attr", "'unsafe-inline'"),
        ("font-src", f"'self' {CDN}"),
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
