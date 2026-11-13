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
import json
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


def location(path):
    """(status, Location header) of a GET that is not followed."""
    try:
        response = urllib.request.build_opener(NoRedirect).open(urllib.request.Request(BASE + path), timeout=30)
        return response.status, response.headers.get('Location', '')
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get('Location', '')


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


def html_parts(value):
    """Every string inside a parsed JSON value that is HTML the page will inject (a key ending in
    ``_html``). Other strings are data: the page's scripts put them in with textContent or hand
    them to a chart, so a ``<script>`` in a group name is only text there, not markup."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith('_html') and isinstance(item, str):
                yield item
            else:
                yield from html_parts(item)
    elif isinstance(value, list):
        for item in value:
            yield from html_parts(item)


def raw_hit(body):
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        body = '\n'.join(html_parts(parsed))
    return next((raw for raw in RAW if raw in body), None)


def check_escaped(label, body, status):
    hit = raw_hit(body)
    report(hit is None, f'{label} [{status}]' + (f' RAW PAYLOAD: {hit}' if hit else ''))


# 1. Stored XSS on every lecturer page that renders seeded data, plus the JSON/HTML fragments
# the front end injects with innerHTML/outerHTML.
for page in ['/classroom/app/', f'/classroom/app/{A}/', f'/classroom/app/{A}/edit/',
             f'/classroom/app/{A}/setup/', f'/classroom/app/{A}/roster/', f'/classroom/app/{A}/rubric/',
             f'/classroom/app/{A}/rubric/table/', f'/classroom/app/{A}/groups/',
             f'/classroom/app/{A}/groups/{GROUP_A}/members/', f'/classroom/app/{A}/participation/',
             f'/classroom/app/{A}/participation/refresh/', f'/classroom/app/{A}/results/',
             f'/classroom/app/{A}/results/refresh/', f'/classroom/app/{A}/live-state/',
             f'/classroom/app/{A}/transfer/', f'/assess/{UUID_A}/join-screen/',
             f'/assess/{UUID_A}/join-screen/state/']:
    status, body = get(page, env['ORG_A_COOKIE'])
    check_escaped(f'stored XSS {page}', body, status)

# 2. Reflected XSS through query-string inputs.
quoted = urllib.parse.quote(PAYLOAD)
for page, param in ((f'/classroom/app/{A}/roster/search/', 'q'), (f'/classroom/app/{A}/results/', 'sort'),
                    (f'/classroom/app/{A}/results/', 'dir'), (f'/classroom/app/{A}/results/', 'status'),
                    ('/classroom/app/', 'scope'), ('/classroom/app/', 'page'), ('/classroom/app/', 'per_page'),
                    (f'/classroom/app/{A}/setup/', 'step')):
    status, body = get(f'{page}?{param}={quoted}', env['ORG_A_COOKIE'])
    check_escaped(f'reflected XSS {page}?{param}=', body, status)

# 3. Cross-organization access: the other org's cookie must get 403/404, never data.
for page in (f'/classroom/app/{A}/', f'/classroom/app/{A}/setup/', f'/classroom/app/{A}/roster/',
             f'/classroom/app/{A}/results/', f'/classroom/app/{A}/results/export/',
             f'/classroom/app/{A}/live-state/', f'/classroom/app/{A}/qr.png',
             f'/classroom/app/{A}/participation/'):
    status, _ = get(page, env['ORG_B_COOKIE'])
    report(status in (403, 404), f'cross-org B->A GET {page} -> {status} (want 403/404)')


def post_as(cookie, path, fields):
    """POST with a real CSRF token, so a refusal is the view's and not the CSRF check's."""
    token_request = urllib.request.Request(BASE + '/classroom/app/new/', headers={'Cookie': f'{COOKIE}={cookie}'})
    try:
        response = urllib.request.build_opener(NoRedirect).open(token_request, timeout=30)
    except urllib.error.HTTPError as err:
        response = err
    token = next((c.split(';')[0].split('=', 1)[1] for c in response.headers.get_all('Set-Cookie') or []
                  if c.startswith('livegrade_csrftoken=')), '')
    if not token:
        return None
    body = urllib.parse.urlencode({**fields, 'csrfmiddlewaretoken': token}).encode()
    request = urllib.request.Request(
        BASE + path, data=body, method='POST',
        headers={'Cookie': f'{COOKIE}={cookie}; livegrade_csrftoken={token}', 'Referer': BASE + '/'})
    try:
        return urllib.request.build_opener(NoRedirect).open(request, timeout=30).status
    except urllib.error.HTTPError as err:
        return err.code


# These two only accept POST, so a GET is always 405; the real question is a POST from org B.
for page, fields in ((f'/classroom/app/{A}/groups/{GROUP_A}/members/', {'action': 'add', 'student_id': STUDENT_A}),
                     (f'/classroom/app/{A}/roster/{STUDENT_A}/edit/', {'full_name': 'Hijacked', 'student_id': 'X'})):
    status = post_as(env['ORG_B_COOKIE'], page, fields)
    if status is None:
        report(False, f'cross-org B->A POST {page}: could not get a CSRF token to test with', warn=True)
    else:
        report(status in (403, 404), f'cross-org B->A POST {page} -> {status} (want 403/404)')
for page in (f'/classroom/app/{B}/', f'/classroom/app/{B}/results/'):
    status, _ = get(page, env['ORG_A_COOKIE'])
    report(status in (403, 404), f'cross-org A->B GET {page} -> {status} (want 403/404)')

# 4. Anonymous access to lecturer URLs.
for page in ('/classroom/app/', f'/classroom/app/{A}/', f'/classroom/app/{A}/results/export/',
             f'/classroom/app/{A}/qr.png', f'/classroom/app/{A}/live-state/'):
    status, _ = get(page)
    report(status in (302, 403), f'anonymous GET {page} -> {status} (want redirect/403)')

# 4b. Service entry addresses: the destination never comes from the request, an unopened
# service shows its coming-soon page, and anything else is not a route.
status, where = location('/classroom/')
report(status == 302 and where == '/oidc/authenticate/',
       f'service entry /classroom/ sends a visitor to sign in -> {status} {where}')
for hostile in ('https://evil.example/', '//evil.example/', 'javascript:alert(1)'):
    status, where = location('/classroom/?next=' + urllib.parse.quote(hostile))
    report(status == 302 and 'evil' not in where and 'javascript' not in where,
           f'service entry ignores a hostile next={hostile} -> {status} {where}')
status, body = get('/panel/')
report(status == 200 and 'coming soon' in body, f'service entry /panel/ shows coming soon -> {status}')
status, _ = get('/nope/')
report(status == 404, f'service entry /nope/ is not a route -> {status} (want 404)')
status, _ = get('/classroom/', method='POST', data=b'')
report(status in (403, 405), f'service entry refuses a POST -> {status} (want 403/405)')

# 5. Student flow: no lecturer cookie. Join, then render pages that show teammates' data.
# The app under test runs with DEBUG off, so its cookies are marked Secure, and Python's cookie jar
# refuses to send a Secure cookie over plain http: the student would never "join". Allowing http
# here is what makes the checks below test the real flow instead of the join page every time.
jar = http.cookiejar.CookieJar(http.cookiejar.DefaultCookiePolicy(secure_protocols=('http', 'https')))
student = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
status, body = get(f'/assess/{UUID_A}/join/', opener=student)
check_escaped('stored XSS student join page', body, status)
token = next((c.value for c in jar if c.name == 'livegrade_csrftoken'), '')
join = urllib.parse.urlencode({'identifier': env['ORG_A_GRADER_NAME'],
                               'csrfmiddlewaretoken': token}).encode()
status, _ = get(f'/assess/{UUID_A}/join/', method='POST', data=join, opener=student)
report(status == 302, f'student can join the session [{status}] (want a redirect to the privacy notice)')
status, body = get(f'/assess/{UUID_A}/privacy/', opener=student)
report(status == 200, f'student sees the privacy notice before scoring [{status}]')
check_escaped('stored XSS student privacy notice', body, status)
notice_token = next((c.value for c in jar if c.name == 'livegrade_csrftoken'), '')
continue_notice = urllib.parse.urlencode({
    'action': 'continue', 'csrfmiddlewaretoken': notice_token,
}).encode()
status, _ = get(f'/assess/{UUID_A}/privacy/', cookie=None, method='POST', data=continue_notice, opener=student)
report(status == 302, f'student records notice acknowledgement and continues [{status}]')
status, body = get(f'/assess/{UUID_A}/evaluate/', opener=student)
report(status == 200, f'student who joined and viewed the notice can open the evaluate page [{status}]')
check_escaped('stored XSS student evaluate page (teammate names, topic, location)', body, status)
# A second, new visitor: one who already joined is redirected before the form is read.
fresh_jar = http.cookiejar.CookieJar(http.cookiejar.DefaultCookiePolicy(secure_protocols=('http', 'https')))
fresh = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(fresh_jar), NoRedirect)
get(f'/assess/{UUID_A}/join/', opener=fresh)
fresh_token = next((c.value for c in fresh_jar if c.name == 'livegrade_csrftoken'), '')
hostile = urllib.parse.urlencode({'identifier': PAYLOAD, 'csrfmiddlewaretoken': fresh_token}).encode()
status, body = get(f'/assess/{UUID_A}/join/', method='POST', data=hostile, opener=fresh)
report(status == 200, f'hostile join attempt gets the join form back [{status}] (a 403 or 302 would mean the check never ran)')
check_escaped('reflected XSS student join identifier', body, status)
request = urllib.request.Request(BASE + f'/assess/{UUID_B}/evaluate/')
try:
    response = student.open(request, timeout=30)
    status, where = response.status, ''
except urllib.error.HTTPError as err:
    status, where = err.code, err.headers.get('Location', '')
report((status == 302 and f'/assess/{UUID_B}/join/' in where) or status in (403, 404),
       f'student pass from session A used on session B -> {status} {where} (want the join page, 403 or 404)')

# 6. WebSocket origin, authorization, and public-event confidentiality.
try:
    import asyncio
    import websockets

    def websocket_url(path):
        return BASE.replace('http://', 'ws://', 1).replace('https://', 'wss://', 1) + path

    async def connection_accepted(path, *, origin=BASE, cookie=None):
        headers = {'Cookie': f'{COOKIE}={cookie}'} if cookie else None
        try:
            async with websockets.connect(websocket_url(path), origin=origin,
                                          additional_headers=headers):
                return True
        except Exception:
            return False

    async def public_grading_event_is_redacted():
        path = f'/ws/assessments/{UUID_A}/'
        try:
            async with websockets.connect(websocket_url(path), origin=BASE) as socket:
                token = next((c.value for c in jar if c.name == 'livegrade_csrftoken'), '')
                fields = urllib.parse.urlencode({
                    'csrfmiddlewaretoken': token, 'answered': '1', 'total': '2',
                }).encode()
                status, _ = await asyncio.to_thread(
                    get, f'/assess/{UUID_A}/grading-ping/', method='POST', data=fields, opener=student)
                if status != 200:
                    return False, f'grading-ping returned {status}'
                message = await asyncio.wait_for(socket.recv(), timeout=5)
                event = json.loads(message)
                details = f"type={event.get('type')}, data-fields={sorted(event.get('data', {}))}"
                return event == {'type': 'activity.updated', 'data': {}}, details
        except Exception as error:
            return False, str(error)

    public_path = f'/ws/assessments/{UUID_A}/'
    staff_path = public_path[:-1] + '/staff/'
    report(asyncio.run(connection_accepted(public_path)),
           'participant WebSocket accepts a same-origin unauthenticated connection')
    report(not asyncio.run(connection_accepted(public_path, origin='https://evil.example')),
           'WebSocket refuses a foreign Origin')
    report(not asyncio.run(connection_accepted(staff_path)),
           'staff WebSocket refuses an anonymous connection')
    report(not asyncio.run(connection_accepted(staff_path, cookie=env['ORG_B_COOKIE'])),
           'staff WebSocket refuses a different organization')
    report(asyncio.run(connection_accepted(staff_path, cookie=env['ORG_A_COOKIE'])),
           'session owner can use the staff WebSocket')
    redacted, detail = asyncio.run(public_grading_event_is_redacted())
    report(redacted, f'public grading-progress event is redacted ({detail})')
except ImportError:
    print('skip  WebSocket checks (pip install websockets)')

print(f'\n{fails} FAIL, {warns} WARN')
sys.exit(1 if fails else 0)
