"""Stdlib-only network helpers — importable from config/settings.py itself
(before Django apps are ready), so keep this free of Django imports.
"""

import socket


def detect_lan_ip():
    """Best-effort LAN IP for this machine.

    Used so a QR code generated while browsing the admin at 127.0.0.1 (or
    localhost) still encodes an address a phone on the same Wi-Fi can
    actually reach — 127.0.0.1 only ever means "this device", so a phone
    scanning that QR would be asking itself for the page, not this machine.

    Opens a UDP socket "connected" to a public address without sending
    anything (UDP connect() just records where a packet *would* go), purely
    to ask the OS which local interface it would route through — the
    standard no-dependency trick for this. Falls back to 127.0.0.1 if the
    machine has no network path at all (e.g. fully offline dev).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('8.8.8.8', 80))
        return sock.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        sock.close()
