"""Targeted security checks that a generic scanner cannot make, against a LOCAL/CI instance
seeded by security/seed_session.py. Standard library only (the WebSocket check needs
`pip install websockets` and is skipped without it).

  1. Stored XSS      every page that renders org A's payload-laden data must show it escaped.
  2. Reflected XSS   query-string inputs must not come back raw.
  3. Cross-org (IDOR) org B's cookie must not read org A's session, and vice versa.
  4. Anonymous       lecturer URLs must refuse a visitor with no session.
  5. Student flow    join as a student, then check the evaluate page escapes teammates' data.
  6. WebSocket       is a connection from a foreign Origin accepted? (reported as WARN)

Usage:  set -a; . security/.local/seed.env; set +a; python security/probe.py http://localhost:8001
Exit 1 if any FAIL. Never run against production.
"""
import http.cookiejar
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1].rstrip('/') if len(sys.argv) > 1 else 'http://localhost:8001'
env = os.environ
A, B = env['ORG_A_SESSION_PK'], env['ORG_B_SESSION_PK']
UUID_A, UUID_B = env['ORG_A_SESSION_UUID'], env['ORG_B_SESSION_UUID']
GROUP_A, STUDENT_A = env['ORG_A_GROUP_PK'], env['ORG_A_STUDENT_PK']
COOKIE = 'livegrade_sessionid'
# Raw (unescaped) forms of the seeded payloads. Correctly escaped output never contains these.
RAW = ['<script>alert(900', '<img src=x onerror=alert(900', "onfocus=alert(9003) x='",
       '</script><script>alert(9004)']
PAYLOAD = '"><script>alert(9001)</script>'
fails = warns = 0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def get(path, cookie=None, method='GET', data=None, opener=None):
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={'Referer': BASE + '/'})
    if cookie:
        request.add_header('Cookie', f'{COOKIE}={cookie}')
    opener = opener or urllib.request.build_opener(NoRedirect)
    try:
        response = opener.open(request, timeout=30)
        return response.status, response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode('utf-8', 'replace')


def report(ok, text, warn=False):
    global fails, warns
    if ok:
        print(f'ok    {text}')
    elif warn:
        warns += 1
        print(f'WARN  {text}')
    else:
        fails += 1
        print(f'FAIL  {text}')


def raw_hit(body):
    return next((raw for raw in RAW if raw in body), None)


def check_escaped(label, body, status):
    hit = raw_hit(body)
    report(hit is None, f'{label} [{status}]' + (f' RAW PAYLOAD: {hit}' if hit else ''))


# 1. Stored XSS on every lecturer page that renders seeded data, plus the JSON/HTML fragments
# the front end injects with innerHTML/outerHTML.
for page in ['/assessments/', f'/assessments/{A}/', f'/assessments/{A}/edit/',
             f'/assessments/{A}/setup/', f'/assessments/{A}/roster/', f'/assessments/{A}/rubric/',
             f'/assessments/{A}/rubric/table/', f'/assessments/{A}/groups/',
             f'/assessments/{A}/groups/{GROUP_A}/members/', f'/assessments/{A}/participation/',
             f'/assessments/{A}/participation/refresh/', f'/assessments/{A}/results/',
             f'/assessments/{A}/results/refresh/', f'/assessments/{A}/live-state/',
             f'/assessments/{A}/transfer/', f'/assess/{UUID_A}/join-screen/',
             f'/assess/{UUID_A}/join-screen/state/']:
    status, body = get(page, env['ORG_A_COOKIE'])
    check_escaped(f'stored XSS {page}', body, status)

# 2. Reflected XSS through query-string inputs.
quoted = urllib.parse.quote(PAYLOAD)
for page, param in ((f'/assessments/{A}/roster/search/', 'q'), (f'/assessments/{A}/results/', 'sort'),
                    (f'/assessments/{A}/results/', 'dir'), (f'/assessments/{A}/results/', 'status'),
                    ('/assessments/', 'scope'), ('/assessments/', 'page'), ('/assessments/', 'per_page'),
                    (f'/assessments/{A}/setup/', 'step')):
    status, body = get(f'{page}?{param}={quoted}', env['ORG_A_COOKIE'])
    check_escaped(f'reflected XSS {page}?{param}=', body, status)

# 3. Cross-organization access: the other org's cookie must get 403/404, never data.
for page in (f'/assessments/{A}/', f'/assessments/{A}/setup/', f'/assessments/{A}/roster/',
             f'/assessments/{A}/results/', f'/assessments/{A}/results/export/',
             f'/assessments/{A}/live-state/', f'/assessments/{A}/qr.png',
             f'/assessments/{A}/participation/', f'/assessments/{A}/groups/{GROUP_A}/members/',
             f'/assessments/{A}/roster/{STUDENT_A}/edit/'):
    status, _ = get(page, env['ORG_B_COOKIE'])
    report(status in (403, 404), f'cross-org B->A GET {page} -> {status} (want 403/404)')
for page in (f'/assessments/{B}/', f'/assessments/{B}/results/'):
    status, _ = get(page, env['ORG_A_COOKIE'])
    report(status in (403, 404), f'cross-org A->B GET {page} -> {status} (want 403/404)')

# 4. Anonymous access to lecturer URLs.
for page in ('/assessments/', f'/assessments/{A}/', f'/assessments/{A}/results/export/',
             f'/assessments/{A}/qr.png', f'/assessments/{A}/live-state/'):
    status, _ = get(page)
    report(status in (302, 403), f'anonymous GET {page} -> {status} (want redirect/403)')

# 5. Student flow: no lecturer cookie. Join, then render pages that show teammates' data.
jar = http.cookiejar.CookieJar()
student = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
status, body = get(f'/assess/{UUID_A}/join/', opener=student)
check_escaped('stored XSS student join page', body, status)
token = next((c.value for c in jar if c.name == 'livegrade_csrftoken'), '')
join = urllib.parse.urlencode({'identifier': env['ORG_A_GRADER_NAME'],
                               'csrfmiddlewaretoken': token}).encode()
get(f'/assess/{UUID_A}/join/', method='POST', data=join, opener=student)
status, body = get(f'/assess/{UUID_A}/evaluate/', opener=student)
report(status == 200, f'student joined and can open evaluate page [{status}]', warn=True)
check_escaped('stored XSS student evaluate page (teammate names, topic, location)', body, status)
hostile = urllib.parse.urlencode({'identifier': PAYLOAD, 'csrfmiddlewaretoken': token}).encode()
status, body = get(f'/assess/{UUID_A}/join/', method='POST', data=hostile, opener=student)
check_escaped('reflected XSS student join identifier', body, status)
status, _ = get(f'/assess/{UUID_B}/evaluate/', opener=student)
report(status in (302, 403, 404), f'student session cookie on another session -> {status} (want redirect/403/404)')

# 6. WebSocket origin handling.
try:
    import asyncio
    import websockets

    async def foreign_origin_accepted():
        url = BASE.replace('http', 'ws', 1) + f'/ws/assessments/{UUID_A}/'
        try:
            async with websockets.connect(url, origin='https://evil.example'):
                return True
        except Exception:
            return False

    report(not asyncio.run(foreign_origin_accepted()),
           'WebSocket refuses a foreign Origin (consider AllowedHostsOriginValidator)', warn=True)
except ImportError:
    print('skip  WebSocket check (pip install websockets)')

print(f'\n{fails} FAIL, {warns} WARN')
sys.exit(1 if fails else 0)
