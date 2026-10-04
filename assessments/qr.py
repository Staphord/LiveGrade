"""QR code generation for a session's public join link."""

from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

import qrcode

from config.netutils import detect_lan_ip

# Addresses that only ever mean "this device" — useless to a phone scanning
# a QR code, which needs an address reachable over the room's Wi-Fi instead.
LOCAL_HOSTNAMES = {'localhost', '127.0.0.1', '0.0.0.0'}


def join_url(request, assessment_session):
    raw = request.build_absolute_uri(f'/assess/{assessment_session.uuid}/join/')
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
