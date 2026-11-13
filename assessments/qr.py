"""QR code generation for a session's public join link."""

from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

import qrcode
from django.conf import settings

from config.netutils import detect_lan_ip

# Addresses that only ever mean "this device" — useless to a phone scanning
# a QR code, which needs an address reachable over the room's Wi-Fi instead.
# These are addresses to REPLACE in a QR link; nothing is ever bound to them.
LOCAL_HOSTNAMES = {'localhost', '127.0.0.1', '0.0.0.0'}  # nosec B104


def join_url(request, assessment_session):
    return public_url(request, f'/assess/{assessment_session.uuid}/join/')


def public_url(request, path):
    """Build a share URL, preferring the explicitly configured public origin."""
    configured_base = settings.LIVEGRADE_PUBLIC_BASE_URL
    if configured_base:
        base_parts = urlsplit(configured_base)
        path_parts = urlsplit(path)
        return urlunsplit((base_parts.scheme, base_parts.netloc, path_parts.path,
                           path_parts.query, path_parts.fragment))

    raw = request.build_absolute_uri(path)
    parts = urlsplit(raw)
    if parts.hostname not in LOCAL_HOSTNAMES:
        return raw  # already a real host (LAN IP, ngrok domain, production) — leave it alone

    lan_ip = detect_lan_ip()
    port = f':{parts.port}' if parts.port else ''
    netloc = f'{lan_ip}{port}'
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def qr_png(url):
    """Returns PNG bytes for the given URL."""
    img = qrcode.make(url, box_size=10, border=2)
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return buffer.getvalue()
