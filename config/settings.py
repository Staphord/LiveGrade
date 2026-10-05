"""LiveGrade settings.

LiveGrade is its own service. It keeps no passwords: people sign in at DevPerf
(OpenID Connect) and arrive here with a signed token naming them and the
organizations they may run LiveGrade for. See README.md.
"""
from pathlib import Path
import os
import sys

from dotenv import load_dotenv

from config.netutils import detect_lan_ip

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')

INSECURE_DEFAULT_SECRET_KEY = 'django-insecure-livegrade-change-in-production'
SECRET_KEY = os.getenv('SECRET_KEY', INSECURE_DEFAULT_SECRET_KEY)
DEBUG = os.getenv('DEBUG', 'True') == 'True'

ALLOWED_HOSTS = [h for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h]
CSRF_TRUSTED_ORIGINS = [o for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o]

# Development only. Students join by scanning a QR code with a phone, so the
# dev server must accept the address a phone can reach and the tunnelled hosts
# used to demo it (ngrok's free plan reassigns the domain on every restart, so
# the whole suffix is allowed rather than one name).
if DEBUG:
    ALLOWED_HOSTS += ['.ngrok-free.app', '.ngrok-free.dev', '.ngrok.io']
    # Auto-detected, because assessments/qr.py rewrites the join URL to this same
    # address: whatever network this machine is on today, the phone is allowed
    # in, rather than relying on a hardcoded address staying correct.
    _lan_ip = detect_lan_ip()
    if _lan_ip not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_lan_ip)
    # Django 4+ requires the https origin to be trusted for any POST made
    # through the tunnel.
    CSRF_TRUSTED_ORIGINS += ['https://*.ngrok-free.app', 'https://*.ngrok-free.dev']

# Where LiveGrade and DevPerf live. Placeholders until the real domain is
# chosen: the production values are set in the environment.
LIVEGRADE_BASE_URL = os.getenv('LIVEGRADE_BASE_URL', 'http://localhost:8001').rstrip('/')
DEVPERF_URL = os.getenv('DEVPERF_URL', 'http://localhost:8000').rstrip('/')

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

    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    CSRF_COOKIE_SAMESITE = 'Lax'
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'True') == 'True'
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', str(30 * 24 * 60 * 60)))
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
    X_FRAME_OPTIONS = 'DENY'
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

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
    'assessments',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

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
    # Reuse the database connection between requests instead of opening a new one
    # for each (a handshake on every page and every live-state poll). The health
    # check replaces a connection the server dropped rather than failing a request.
    DATABASES['default']['CONN_MAX_AGE'] = int(os.getenv('DB_CONN_MAX_AGE', '60'))
    DATABASES['default']['CONN_HEALTH_CHECKS'] = True

# Live updates and the turn timers. Redis database 1, not DevPerf's 0, so the
# two services sharing one Redis server can never read each other's messages or
# jobs.
REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/1')

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

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
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
