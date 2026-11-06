"""LiveGrade settings.

LiveGrade is its own service. It keeps no passwords: people sign in at DevPerf
(OpenID Connect) and arrive here with a signed token naming them and the
organizations they may run LiveGrade for. See README.md.
"""
from pathlib import Path
import os
import sys
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from config.netutils import detect_lan_ip

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')

INSECURE_DEFAULT_SECRET_KEY = 'django-insecure-livegrade-change-in-production'
SECRET_KEY = os.getenv('SECRET_KEY', INSECURE_DEFAULT_SECRET_KEY)
DEBUG = os.getenv('DEBUG', 'True') == 'True'

# Where LiveGrade and DevPerf live. Placeholders until the domain is
# chosen: the production values are set in the environment.
_default_livegrade_base_url = 'http://localhost:8001' if DEBUG else 'https://localhost:8001'
LIVEGRADE_BASE_URL = os.getenv('LIVEGRADE_BASE_URL', _default_livegrade_base_url).rstrip('/')
DEVPERF_URL = os.getenv('DEVPERF_URL', 'http://localhost:8000').rstrip('/')
_livegrade_url = urlsplit(LIVEGRADE_BASE_URL)
_local_http_development = (
    _livegrade_url.scheme == 'http'
    and _livegrade_url.hostname in {'localhost', '127.0.0.1', '::1'}
)

ALLOWED_HOSTS = [h for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,192.168.2.114').split(',') if h]
CSRF_TRUSTED_ORIGINS = [o for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o]
LIVEGRADE_PUBLIC_BASE_URL = os.getenv('LIVEGRADE_PUBLIC_BASE_URL', '').strip().rstrip('/')
_public_url = None
if LIVEGRADE_PUBLIC_BASE_URL:
    try:
        _public_url = urlsplit(LIVEGRADE_PUBLIC_BASE_URL)
        _public_url.port  # Validate the optional port during startup.
    except ValueError as exc:
        raise ImproperlyConfigured('LIVEGRADE_PUBLIC_BASE_URL must be a valid HTTP(S) origin.') from exc
    if (_public_url.scheme not in {'http', 'https'} or not _public_url.hostname
            or _public_url.username or _public_url.password
            or _public_url.path not in {'', '/'} or _public_url.query or _public_url.fragment):
        raise ImproperlyConfigured('LIVEGRADE_PUBLIC_BASE_URL must be a valid HTTP(S) origin.')
    if not DEBUG and not _local_http_development and _public_url.scheme != 'https':
        raise ImproperlyConfigured('LIVEGRADE_PUBLIC_BASE_URL must use HTTPS when DEBUG is off.')
    if (DEBUG or _local_http_development) and _public_url.hostname not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_public_url.hostname)

# Development only. Students join by scanning a QR code with a phone, so the
# dev server must accept the address a phone can reach and the tunnelled hosts
# used to demo it (ngrok's free plan reassigns the domain on every restart, so
# the whole suffix is allowed rather than one name).
if DEBUG:
    ALLOWED_HOSTS += ['.ngrok-free.app', '.ngrok-free.dev', '.ngrok.io']
    # Keep the current best-effort route address allowed for LAN development.
    _lan_ip = detect_lan_ip()
    if _lan_ip not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_lan_ip)
    # Django 4+ requires the https origin to be trusted for any POST made
    # through the tunnel.
    CSRF_TRUSTED_ORIGINS += ['https://*.ngrok-free.app', 'https://*.ngrok-free.dev']

# Panel examiner invitations use Django's configured backend. Credentials stay
# in the deployment environment; local development may leave SMTP unset and
# use the one-time organizer copy fallback when mail cannot be delivered.
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = os.getenv('EMAIL_HOST', 'localhost')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '25'))
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'False') == 'True'
EMAIL_USE_SSL = os.getenv('EMAIL_USE_SSL', 'False') == 'True'
EMAIL_TIMEOUT = int(os.getenv('EMAIL_TIMEOUT', '10'))
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'webmaster@localhost')
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured('Configure SMTP with TLS or SSL, not both.')
if not DEBUG and (EMAIL_HOST_USER or EMAIL_HOST_PASSWORD) and not (EMAIL_USE_TLS or EMAIL_USE_SSL):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured('Authenticated SMTP must use TLS or SSL when DEBUG is off.')

# The services that are open to the public (portal/services.py). Any other service is shown
# as coming soon and cannot be entered. Names that are not in the registry are ignored.
LIVEGRADE_LIVE_SERVICES = [s.strip() for s in os.getenv('LIVEGRADE_LIVE_SERVICES', 'classroom,polls').split(',') if s.strip()]
if 'test' in sys.argv:
    LIVEGRADE_LIVE_SERVICES = ['classroom', 'polls']

# Production hardening, as in DevPerf: refuse to start weak rather than start
# with a published key. Never during `manage.py test`.
if not DEBUG and 'test' not in sys.argv:
    from django.core.exceptions import ImproperlyConfigured

    if SECRET_KEY == INSECURE_DEFAULT_SECRET_KEY or len(SECRET_KEY) < 32:
        raise ImproperlyConfigured(
            'SECRET_KEY must be set to a unique value of at least 32 '
            'characters when DEBUG is off.')
    if not os.getenv('ALLOWED_HOSTS'):
        raise ImproperlyConfigured('ALLOWED_HOSTS must be set explicitly when DEBUG is off.')
    if not (os.getenv('OIDC_RP_CLIENT_ID') and os.getenv('OIDC_RP_CLIENT_SECRET')):
        raise ImproperlyConfigured(
            'OIDC_RP_CLIENT_ID and OIDC_RP_CLIENT_SECRET must be set when '
            'DEBUG is off (see `manage.py register_livegrade_client` in DevPerf).')

    # Local HTTP is useful when DEBUG is disabled to exercise the production
    # error pages and service configuration. Secure cookies cannot be sent by
    # an HTTP localhost origin, so limit this exception to an explicitly
    # configured loopback base URL; every non-local deployment stays HTTPS-only.
    SESSION_COOKIE_SECURE = not _local_http_development
    CSRF_COOKIE_SECURE = not _local_http_development
    SECURE_SSL_REDIRECT = os.getenv(
        'SECURE_SSL_REDIRECT', 'False' if _local_http_development else 'True') == 'True'
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', str(30 * 24 * 60 * 60)))
    # Both are off until every subdomain of the site is known to serve HTTPS: HSTS with
    # includeSubDomains makes browsers refuse plain HTTP on all of them for SECURE_HSTS_SECONDS,
    # and preload is very hard to undo. Set them to True in the environment once that is true.
    SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv('SECURE_HSTS_INCLUDE_SUBDOMAINS', 'False') == 'True'
    SECURE_HSTS_PRELOAD = os.getenv('SECURE_HSTS_PRELOAD', 'False') == 'True'
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
    X_FRAME_OPTIONS = 'DENY'
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Cookie flags that hold in every environment (the Secure flag is added in production above).
# Neither cookie is readable by script. Pages that send the token from script (the student
# pages have no form to carry it) read it from <meta name="csrf-token"> in the page head and
# send it as X-CSRFToken; they never touch the cookie.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = 'Lax'

# Content-Security-Policy (accounts/csp.py): enforced wherever DEBUG is off, report-only while
# developing (so a page you are editing is not blocked halfway). CSP_ENFORCE=False is the
# emergency switch back to report-only; it is not meant to stay off.
CSP_ENFORCE = os.getenv('CSP_ENFORCE', 'False' if DEBUG else 'True') == 'True'

INSTALLED_APPS = [
    'daphne',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',
    'mozilla_django_oidc',
    'accounts',
    'portal',
    'assessments',
    'service_domains',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'accounts.csp.ContentSecurityPolicyMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'portal.middleware.ServiceMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# Production static assets are collected into STATIC_ROOT and served by
# WhiteNoise with content-hashed names plus precompressed gzip/Brotli variants.
# Keep normal development/test storage so local source files work without a
# collectstatic run.
if not DEBUG and 'test' not in sys.argv:
    MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.debug',
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
        'accounts.context.livegrade',
    ]},
}]

DATABASES = {
    'default': {
        'ENGINE': os.getenv('DB_ENGINE', 'django.db.backends.sqlite3'),
        'NAME': os.getenv('DB_NAME', str(BASE_DIR / 'db.sqlite3')),
        'USER': os.getenv('DB_USER', ''),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', ''),
        'PORT': os.getenv('DB_PORT', ''),
    }
}
if DATABASES['default']['ENGINE'] != 'django.db.backends.sqlite3':  # pragma: no cover - server databases only; tests and dev run on SQLite
    # ASGI workers must not retain per-thread persistent connections. Configure
    # database-side pooling and its connection budget in the deployment environment.
    DATABASES['default']['CONN_MAX_AGE'] = int(os.getenv('DB_CONN_MAX_AGE', '0'))
    DATABASES['default']['CONN_HEALTH_CHECKS'] = True
    if DATABASES['default']['ENGINE'].endswith('postgresql'):
        DATABASES['default']['OPTIONS'] = {'connect_timeout': int(os.getenv('DB_CONNECT_TIMEOUT', '5'))}

# Live updates and the turn timers. Redis database 1, not DevPerf's 0, so the
# two services sharing one Redis server can never read each other's messages or
# jobs.
REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/1')

# Shared across web workers, including the coming-soon signup abuse limit.
CACHES = {'default': {
    'BACKEND': 'django.core.cache.backends.redis.RedisCache',
    'LOCATION': REDIS_URL,
    'KEY_PREFIX': 'livegrade',
    'OPTIONS': {'socket_connect_timeout': 2, 'socket_timeout': 2},
}}
LIVEGRADE_INTEREST_RATE_LIMIT = int(os.getenv('LIVEGRADE_INTEREST_RATE_LIMIT', '5'))
LIVEGRADE_INTEREST_RATE_WINDOW = int(os.getenv('LIVEGRADE_INTEREST_RATE_WINDOW', '600'))
if LIVEGRADE_INTEREST_RATE_LIMIT < 1 or LIVEGRADE_INTEREST_RATE_WINDOW < 1:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured('Service interest rate limit and window must be positive.')
# Per-event source budgets allow shared classroom and event Wi-Fi addresses.
COMPETITION_JOIN_RATE_LIMIT = int(os.getenv('COMPETITION_JOIN_RATE_LIMIT', '6000'))
if COMPETITION_JOIN_RATE_LIMIT < 1:
    raise ImproperlyConfigured('Competition join request budget must be positive.')
POLL_PUBLIC_READ_RATE_LIMIT = int(os.getenv('POLL_PUBLIC_READ_RATE_LIMIT', '60000'))
POLL_PUBLIC_SUBMISSION_RATE_LIMIT = int(os.getenv('POLL_PUBLIC_SUBMISSION_RATE_LIMIT', '6000'))
if min(POLL_PUBLIC_READ_RATE_LIMIT, POLL_PUBLIC_SUBMISSION_RATE_LIMIT) < 1:
    raise ImproperlyConfigured('Poll request budgets must be positive.')

PANEL_EXAMINER_RATE_LIMIT = int(os.getenv('PANEL_EXAMINER_RATE_LIMIT', '120'))
PANEL_EXAMINER_RATE_WINDOW = int(os.getenv('PANEL_EXAMINER_RATE_WINDOW', '60'))
if PANEL_EXAMINER_RATE_LIMIT < 1 or PANEL_EXAMINER_RATE_WINDOW < 1:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured('Panel examiner rate limit and window must be positive.')
PUBLIC_BEARER_UNKNOWN_RATE_LIMIT = int(os.getenv('PUBLIC_BEARER_UNKNOWN_RATE_LIMIT', '300'))
PUBLIC_BEARER_UNKNOWN_RATE_WINDOW = int(os.getenv('PUBLIC_BEARER_UNKNOWN_RATE_WINDOW', '60'))
if PUBLIC_BEARER_UNKNOWN_RATE_LIMIT < 1 or PUBLIC_BEARER_UNKNOWN_RATE_WINDOW < 1:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured('Unknown bearer-link rate limit and window must be positive.')
SHOWCASE_PUBLIC_RATE_LIMIT = int(os.getenv('SHOWCASE_PUBLIC_RATE_LIMIT', '2000'))
SHOWCASE_PUBLIC_RATE_WINDOW = int(os.getenv('SHOWCASE_PUBLIC_RATE_WINDOW', '60'))
if SHOWCASE_PUBLIC_RATE_LIMIT < 1 or SHOWCASE_PUBLIC_RATE_WINDOW < 1:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured('Showcase public rate limit and window must be positive.')

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {'hosts': [{'address': REDIS_URL, 'socket_timeout': 20}]},
    },
}

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ['application/json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes hard limit
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# DevPerf's database, for the one-off `manage.py import_from_devperf` copy of the
# sessions LiveGrade used to keep there. It is only ever read from. Absent unless
# configured, so nothing here can reach DevPerf's data by accident.
if os.getenv('DEVPERF_DB_NAME'):
    DATABASES['devperf'] = {
        'ENGINE': os.getenv('DEVPERF_DB_ENGINE', 'django.db.backends.postgresql'),
        'NAME': os.environ['DEVPERF_DB_NAME'],
        'USER': os.getenv('DEVPERF_DB_USER', ''),
        'PASSWORD': os.getenv('DEVPERF_DB_PASSWORD', ''),
        'HOST': os.getenv('DEVPERF_DB_HOST', ''),
        'PORT': os.getenv('DEVPERF_DB_PORT', ''),
    }

AUTH_USER_MODEL = 'accounts.User'
AUTHENTICATION_BACKENDS = ['accounts.oidc.DevPerfBackend']

if 'test' in sys.argv:
    # The suite needs no Redis: in-memory channels keep an external service off
    # the test path, and a fast hasher costs nothing in what is under test.
    PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
    CHANNEL_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}
    # Jobs queued by tests (turn timers) must not land in a real Redis, where a
    # developer's worker would pick them up: keep the broker in memory too.
    CELERY_BROKER_URL = 'memory://'
    CELERY_RESULT_BACKEND = 'cache+memory://'
    # A stand-in for DevPerf's database, so the importer can be tested.
    DATABASES['devperf'] = {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                          'LOCATION': 'livegrade-tests'}}

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = Path(os.getenv('STATIC_ROOT', BASE_DIR / 'staticfiles'))
STATICFILES_DIRS = [BASE_DIR / 'static']
if not DEBUG and 'test' not in sys.argv:
    STORAGES = {
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
    }
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Our own 404 and 500 pages even while DEBUG is on (see accounts/error_pages.py). Set
# FRIENDLY_ERROR_PAGES=False to get Django's technical pages back while debugging.
FRIENDLY_ERROR_PAGES = os.getenv('FRIENDLY_ERROR_PAGES', 'True') == 'True'

# A readable page, not Django's yellow one, when a form's security token does not match.
CSRF_FAILURE_VIEW = 'accounts.views.csrf_failure'

# Cookie names are LiveGrade's own. Browsers share cookies across ports on one
# host, so in local development localhost:8000 (DevPerf) and localhost:8001
# (this) would otherwise overwrite each other's session and CSRF cookies and
# keep signing each other out. In production the subdomains are separate
# anyway; do not set SESSION_COOKIE_DOMAIN, which would share them.
SESSION_COOKIE_NAME = 'livegrade_sessionid'
CSRF_COOKIE_NAME = 'livegrade_csrftoken'
# A lecturer's access is read from DevPerf at sign-in and kept for the session,
# so the session is the longest a withdrawn permission can linger.
SESSION_COOKIE_AGE = int(os.getenv('SESSION_COOKIE_AGE', str(8 * 60 * 60)))

# Sign-in with DevPerf (OpenID Connect, authorization code with PKCE).
OIDC_RP_CLIENT_ID = os.getenv('OIDC_RP_CLIENT_ID', '')
OIDC_RP_CLIENT_SECRET = os.getenv('OIDC_RP_CLIENT_SECRET', '')
OIDC_RP_SIGN_ALGO = 'RS256'
OIDC_RP_SCOPES = 'openid profile email livegrade'
OIDC_USE_PKCE = True
OIDC_STORE_ID_TOKEN = True
OIDC_OP_AUTHORIZATION_ENDPOINT = f'{DEVPERF_URL}/o/authorize/'
OIDC_OP_TOKEN_ENDPOINT = f'{DEVPERF_URL}/o/token/'
OIDC_OP_USER_ENDPOINT = f'{DEVPERF_URL}/o/userinfo/'
OIDC_OP_JWKS_ENDPOINT = f'{DEVPERF_URL}/o/.well-known/jwks.json'
OIDC_OP_LOGOUT_ENDPOINT = f'{DEVPERF_URL}/o/logout/'
OIDC_OP_LOGOUT_URL_METHOD = 'accounts.oidc.devperf_logout_url'
OIDC_AUTHENTICATION_CALLBACK_URL = 'oidc_authentication_callback'
OIDC_CREATE_USER = True
OIDC_TIMEOUT = 10
# A page only ever redirects back into this site after sign-in.
OIDC_REDIRECT_ALLOWED_HOSTS = [h for h in ALLOWED_HOSTS if h]

LOGIN_URL = 'oidc_authentication_init'
LOGIN_REDIRECT_URL = '/'
LOGIN_REDIRECT_URL_FAILURE = '/no-access/'
LOGOUT_REDIRECT_URL = '/'
