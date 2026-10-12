"""Checks a deployed LiveGrade's security headers and session cookie flags.

Usage:  python security/check_headers.py https://livegrade.example.com
Exit 1 when a required header or cookie flag is missing. CSP is reported as a
warning only: settings.py does not set one yet.
"""
import sys
import urllib.error
import urllib.request

REQUIRED = {
    'Strict-Transport-Security': lambda v: 'max-age=' in v.lower(),
    'X-Content-Type-Options': lambda v: v.lower() == 'nosniff',
    'X-Frame-Options': lambda v: v.upper() in ('DENY', 'SAMEORIGIN'),
    'Referrer-Policy': lambda v: bool(v),
}
RECOMMENDED = ['Content-Security-Policy', 'Permissions-Policy']


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        return opener.open(url, timeout=20)
    except urllib.error.HTTPError as err:  # 3xx/4xx still carry the headers we want
        return err


def main(base):
    base = base.rstrip('/')
    failures = 0
    # http -> https redirect
    plain = fetch(base.replace('https://', 'http://', 1) + '/')
    location = plain.headers.get('Location', '')
    if plain.status in (301, 302, 307, 308) and location.startswith('https://'):
        print('ok    http redirects to https')
    else:
        print(f'FAIL  http does not redirect to https (status {plain.status})')
        failures += 1

    response = fetch(base + '/')
    for name, ok in REQUIRED.items():
        value = response.headers.get(name)
        if value and ok(value):
            print(f'ok    {name}: {value}')
        else:
            print(f'FAIL  {name} missing or weak (got {value!r})')
            failures += 1
    for name in RECOMMENDED:
        print(f'ok    {name} present' if response.headers.get(name)
              else f'WARN  {name} not set')

    for cookie in response.headers.get_all('Set-Cookie') or []:
        flags = cookie.lower()
        for flag in ('secure', 'samesite='):
            if flag not in flags:
                print(f'FAIL  cookie {cookie.split("=")[0]} lacks {flag}')
                failures += 1
        if cookie.lower().startswith('livegrade_sessionid') and 'httponly' not in flags:
            print('FAIL  session cookie lacks HttpOnly')
            failures += 1
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
