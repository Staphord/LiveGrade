"""The settings file under different environments.

``config/settings.py`` is executed from scratch with a controlled environment,
because what it decides depends entirely on where it runs, and getting
production wrong is a security failure rather than a cosmetic one.
"""
import os
import runpy
import sys
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

BASE_DIR = Path(settings.BASE_DIR)
SETTINGS = BASE_DIR / 'config' / 'settings.py'
RUNSERVER = ['manage.py', 'runserver']


def load(env, argv=RUNSERVER, lan_ip='10.9.8.7'):
    """The settings namespace ``env`` and ``argv`` would produce. The real
    environment is replaced, ``.env`` is not read, and the machine's LAN address
    is a fixed one so nothing depends on the network the tests run on."""
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(sys, 'argv', list(argv)), \
            mock.patch('dotenv.load_dotenv'), \
            mock.patch('config.netutils.detect_lan_ip', return_value=lan_ip):
        return runpy.run_path(str(SETTINGS))


def production(**overrides):
    env = {'DEBUG': 'False', 'SECRET_KEY': 'k' * 50, 'ALLOWED_HOSTS': 'livegrade.example.com',
           'OIDC_RP_CLIENT_ID': 'id', 'OIDC_RP_CLIENT_SECRET': 'secret'}
    env.update(overrides)
    return env


class DevelopmentTests(SimpleTestCase):

    def test_development_defaults_point_at_local_servers(self):
        namespace = load({})
        self.assertTrue(namespace['DEBUG'])
        self.assertEqual(namespace['LIVEGRADE_BASE_URL'], 'http://localhost:8001')
        self.assertEqual(namespace['DEVPERF_URL'], 'http://localhost:8000')
        self.assertEqual(namespace['OIDC_OP_AUTHORIZATION_ENDPOINT'], 'http://localhost:8000/o/authorize/')
        self.assertEqual(namespace['OIDC_OP_JWKS_ENDPOINT'], 'http://localhost:8000/o/.well-known/jwks.json')

    def test_trailing_slashes_on_the_addresses_are_dropped(self):
        namespace = load({'DEVPERF_URL': 'https://d.example.com/', 'LIVEGRADE_BASE_URL': 'https://l.example.com/'})
        self.assertEqual(namespace['DEVPERF_URL'], 'https://d.example.com')
        self.assertEqual(namespace['OIDC_OP_TOKEN_ENDPOINT'], 'https://d.example.com/o/token/')
        self.assertEqual(namespace['LIVEGRADE_BASE_URL'], 'https://l.example.com')

    def test_public_base_url_is_validated_and_added_to_development_hosts(self):
        namespace = load({'LIVEGRADE_PUBLIC_BASE_URL': 'http://192.168.2.114:8001/'})
        self.assertEqual(namespace['LIVEGRADE_PUBLIC_BASE_URL'], 'http://192.168.2.114:8001')
        self.assertIn('192.168.2.114', namespace['ALLOWED_HOSTS'])
        with self.assertRaises(ImproperlyConfigured):
            load({'LIVEGRADE_PUBLIC_BASE_URL': 'ftp://192.168.2.114'})
        with self.assertRaises(ImproperlyConfigured):
            load(production(LIVEGRADE_PUBLIC_BASE_URL='http://livegrade.example.test'))

    def test_the_session_cookie_is_hidden_from_script_in_every_environment(self):
        for env in ({}, production()):
            with self.subTest(debug=env.get('DEBUG', 'True')):
                namespace = load(env)
                self.assertTrue(namespace['SESSION_COOKIE_HTTPONLY'])
                self.assertEqual(namespace['SESSION_COOKIE_SAMESITE'], 'Lax')
                self.assertEqual(namespace['CSRF_COOKIE_SAMESITE'], 'Lax')

    def test_the_csrf_cookie_is_hidden_from_script_too(self):
        for env in ({}, production()):
            with self.subTest(debug=env.get('DEBUG', 'True')):
                self.assertTrue(load(env)['CSRF_COOKIE_HTTPONLY'])

    def test_no_page_script_reads_a_cookie(self):
        # With the CSRF cookie hidden, a script that read document.cookie would find nothing.
        reading = [str(path.relative_to(BASE_DIR)) for path in (BASE_DIR / 'templates').rglob('*.html')
                   if 'document.cookie' in path.read_text()]
        reading += [str(path.relative_to(BASE_DIR)) for path in (BASE_DIR / 'static').rglob('*.js')
                    if 'document.cookie' in path.read_text()]
        self.assertEqual(reading, [])

    def test_the_pages_that_send_the_token_from_script_carry_it_in_a_meta_tag(self):
        for name in ('templates/base.html', 'templates/assessments/student/_student_base.html'):
            with self.subTest(template=name):
                self.assertIn('<meta name="csrf-token" content="{{ csrf_token }}">', (BASE_DIR / name).read_text())

    def test_content_security_policy_is_report_only_while_developing_and_can_be_forced(self):
        self.assertFalse(load({})['CSP_ENFORCE'])
        self.assertTrue(load({'CSP_ENFORCE': 'True'})['CSP_ENFORCE'])

    def test_content_security_policy_is_enforced_in_production_unless_switched_off(self):
        self.assertTrue(load(production())['CSP_ENFORCE'])
        self.assertFalse(load(production(CSP_ENFORCE='False'))['CSP_ENFORCE'])

    def test_development_does_not_force_https(self):
        namespace = load({})
        for name in ('SECURE_SSL_REDIRECT', 'SESSION_COOKIE_SECURE', 'SECURE_HSTS_SECONDS'):
            self.assertNotIn(name, namespace)

    def test_the_cookies_are_livegrades_own_and_not_shared_with_devperf(self):
        namespace = load({})
        self.assertEqual(namespace['SESSION_COOKIE_NAME'], 'livegrade_sessionid')
        self.assertEqual(namespace['CSRF_COOKIE_NAME'], 'livegrade_csrftoken')
        self.assertNotIn('SESSION_COOKIE_DOMAIN', namespace)

    def test_a_sign_in_lasts_eight_hours_unless_told_otherwise(self):
        self.assertEqual(load({})['SESSION_COOKIE_AGE'], 8 * 60 * 60)
        self.assertEqual(load({'SESSION_COOKIE_AGE': '600'})['SESSION_COOKIE_AGE'], 600)

    def test_sign_in_uses_pkce_and_the_signed_token_algorithm(self):
        namespace = load({})
        self.assertTrue(namespace['OIDC_USE_PKCE'])
        self.assertEqual(namespace['OIDC_RP_SIGN_ALGO'], 'RS256')
        self.assertIn('livegrade', namespace['OIDC_RP_SCOPES'].split())

    def test_the_database_defaults_to_sqlite_and_can_be_pointed_elsewhere(self):
        self.assertIn('sqlite3', load({})['DATABASES']['default']['ENGINE'])
        namespace = load({'DB_ENGINE': 'django.db.backends.postgresql', 'DB_NAME': 'lg'})
        self.assertEqual(namespace['DATABASES']['default']['NAME'], 'lg')

    def test_no_admin_site_is_installed(self):
        self.assertNotIn('django.contrib.admin', load({})['INSTALLED_APPS'])

    def test_hosts_and_trusted_origins_come_from_the_environment(self):
        namespace = load({'ALLOWED_HOSTS': 'a.example.com,b.example.com',
                          'CSRF_TRUSTED_ORIGINS': 'https://a.example.com'})
        self.assertEqual(namespace['ALLOWED_HOSTS'][:2], ['a.example.com', 'b.example.com'])
        self.assertEqual(namespace['CSRF_TRUSTED_ORIGINS'][0], 'https://a.example.com')

    def test_tunnel_hosts_and_the_lan_address_are_allowed_in_development(self):
        namespace = load({})
        self.assertIn('.ngrok-free.app', namespace['ALLOWED_HOSTS'])
        self.assertIn('10.9.8.7', namespace['ALLOWED_HOSTS'])
        self.assertIn('https://*.ngrok-free.app', namespace['CSRF_TRUSTED_ORIGINS'])

    def test_a_lan_address_that_is_already_allowed_is_not_added_twice(self):
        namespace = load({'ALLOWED_HOSTS': 'localhost,10.9.8.7'})
        self.assertEqual(namespace['ALLOWED_HOSTS'].count('10.9.8.7'), 1)

    def test_the_sign_in_redirect_accepts_only_this_sites_hosts(self):
        namespace = load({})
        self.assertEqual(namespace['OIDC_REDIRECT_ALLOWED_HOSTS'], namespace['ALLOWED_HOSTS'])


class ServiceSettingsTests(SimpleTestCase):

    def test_smtp_settings_are_environment_configurable(self):
        defaults = load({})
        self.assertEqual(defaults['EMAIL_HOST'], 'localhost')
        self.assertEqual(defaults['EMAIL_PORT'], 25)
        self.assertEqual(defaults['DEFAULT_FROM_EMAIL'], 'webmaster@localhost')
        configured = load({
            'EMAIL_HOST': 'smtp.example.test', 'EMAIL_PORT': '587',
            'EMAIL_HOST_USER': 'mailer', 'EMAIL_HOST_PASSWORD': 'secret',
            'EMAIL_USE_TLS': 'True', 'EMAIL_TIMEOUT': '20',
            'DEFAULT_FROM_EMAIL': 'LiveGrade <noreply@example.test>',
        })
        self.assertEqual(configured['EMAIL_HOST'], 'smtp.example.test')
        self.assertEqual(configured['EMAIL_PORT'], 587)
        self.assertEqual(configured['EMAIL_HOST_USER'], 'mailer')
        self.assertEqual(configured['EMAIL_HOST_PASSWORD'], 'secret')
        self.assertTrue(configured['EMAIL_USE_TLS'])
        self.assertEqual(configured['EMAIL_TIMEOUT'], 20)
        self.assertEqual(configured['DEFAULT_FROM_EMAIL'], 'LiveGrade <noreply@example.test>')

    def test_authenticated_production_smtp_requires_encryption(self):
        with self.assertRaises(ImproperlyConfigured):
            load(production(EMAIL_HOST_PASSWORD='secret'))
        with self.assertRaises(ImproperlyConfigured):
            load(production(EMAIL_HOST_USER='mailer'))
        secure = load(production(EMAIL_HOST_PASSWORD='secret', EMAIL_USE_TLS='True'))
        self.assertTrue(secure['EMAIL_USE_TLS'])
        with self.assertRaises(ImproperlyConfigured):
            load({'EMAIL_USE_TLS': 'True', 'EMAIL_USE_SSL': 'True'})

    def test_interest_limits_have_configurable_positive_defaults(self):
        namespace = load({})
        self.assertEqual(namespace['LIVEGRADE_INTEREST_RATE_LIMIT'], 5)
        self.assertEqual(namespace['LIVEGRADE_INTEREST_RATE_WINDOW'], 600)
        self.assertEqual(load({'LIVEGRADE_INTEREST_RATE_LIMIT': '2'})['LIVEGRADE_INTEREST_RATE_LIMIT'], 2)
        for env in ({'LIVEGRADE_INTEREST_RATE_LIMIT': '0'}, {'LIVEGRADE_INTEREST_RATE_WINDOW': '-1'}):
            with self.subTest(env=env), self.assertRaises(ImproperlyConfigured):
                load(env)

    def test_panel_examiner_rate_limit_is_configurable_and_positive(self):
        defaults = load({})
        self.assertEqual(defaults['PANEL_EXAMINER_RATE_LIMIT'], 120)
        self.assertEqual(defaults['PANEL_EXAMINER_RATE_WINDOW'], 60)
        configured = load({'PANEL_EXAMINER_RATE_LIMIT': '12',
                           'PANEL_EXAMINER_RATE_WINDOW': '90'})
        self.assertEqual(configured['PANEL_EXAMINER_RATE_LIMIT'], 12)
        self.assertEqual(configured['PANEL_EXAMINER_RATE_WINDOW'], 90)
        for env in ({'PANEL_EXAMINER_RATE_LIMIT': '0'},
                    {'PANEL_EXAMINER_RATE_WINDOW': '-1'}):
            with self.subTest(env=env), self.assertRaises(ImproperlyConfigured):
                load(env)

    def test_unknown_bearer_rate_limit_is_configurable_and_positive(self):
        defaults = load({})
        self.assertEqual(defaults['PUBLIC_BEARER_UNKNOWN_RATE_LIMIT'], 300)
        self.assertEqual(defaults['PUBLIC_BEARER_UNKNOWN_RATE_WINDOW'], 60)
        configured = load({'PUBLIC_BEARER_UNKNOWN_RATE_LIMIT': '20',
                           'PUBLIC_BEARER_UNKNOWN_RATE_WINDOW': '90'})
        self.assertEqual(configured['PUBLIC_BEARER_UNKNOWN_RATE_LIMIT'], 20)
        self.assertEqual(configured['PUBLIC_BEARER_UNKNOWN_RATE_WINDOW'], 90)
        for env in ({'PUBLIC_BEARER_UNKNOWN_RATE_LIMIT': '0'},
                    {'PUBLIC_BEARER_UNKNOWN_RATE_WINDOW': '-1'}):
            with self.subTest(env=env), self.assertRaises(ImproperlyConfigured):
                load(env)

    def test_showcase_visitor_rate_limit_allows_configured_crowd_bursts(self):
        defaults = load({})
        self.assertEqual(defaults['SHOWCASE_PUBLIC_RATE_LIMIT'], 2000)
        self.assertEqual(defaults['SHOWCASE_PUBLIC_RATE_WINDOW'], 60)
        configured = load({'SHOWCASE_PUBLIC_RATE_LIMIT': '2400',
                           'SHOWCASE_PUBLIC_RATE_WINDOW': '45'})
        self.assertEqual(configured['SHOWCASE_PUBLIC_RATE_LIMIT'], 2400)
        self.assertEqual(configured['SHOWCASE_PUBLIC_RATE_WINDOW'], 45)
        for env in ({'SHOWCASE_PUBLIC_RATE_LIMIT': '0'},
                    {'SHOWCASE_PUBLIC_RATE_WINDOW': '-1'}):
            with self.subTest(env=env), self.assertRaises(ImproperlyConfigured):
                load(env)

    def test_interest_limits_use_shared_redis_and_tests_need_no_redis(self):
        cache = load({'REDIS_URL': 'redis://cache:6379/5'})['CACHES']['default']
        self.assertEqual(cache['BACKEND'], 'django.core.cache.backends.redis.RedisCache')
        self.assertEqual(cache['LOCATION'], 'redis://cache:6379/5')
        self.assertIn('locmem', load({}, argv=['manage.py', 'test'])['CACHES']['default']['BACKEND'])

    def test_only_classroom_is_open_unless_told_otherwise(self):
        self.assertEqual(load({})['LIVEGRADE_LIVE_SERVICES'], ['classroom', 'polls'])
        self.assertEqual(load(production())['LIVEGRADE_LIVE_SERVICES'], ['classroom', 'polls'])

    def test_the_open_services_come_from_the_environment(self):
        namespace = load({'LIVEGRADE_LIVE_SERVICES': 'classroom, panel ,'})
        self.assertEqual(namespace['LIVEGRADE_LIVE_SERVICES'], ['classroom', 'panel'])

    def test_every_service_can_be_switched_off(self):
        self.assertEqual(load({'LIVEGRADE_LIVE_SERVICES': ''})['LIVEGRADE_LIVE_SERVICES'], [])

    def test_test_runner_ignores_local_service_switches(self):
        namespace = load({'LIVEGRADE_LIVE_SERVICES': 'classroom,panel,competition,team,showcase'},
                         argv=['manage.py', 'test'])
        self.assertEqual(namespace['LIVEGRADE_LIVE_SERVICES'], ['classroom', 'polls'])

    def test_the_portal_app_is_installed(self):
        self.assertIn('portal', load({})['INSTALLED_APPS'])

    def test_the_service_is_worked_out_after_the_person_is_known_and_before_any_view(self):
        middleware = load({})['MIDDLEWARE']
        position = middleware.index('portal.middleware.ServiceMiddleware')
        self.assertGreater(position, middleware.index('django.contrib.auth.middleware.AuthenticationMiddleware'))
        self.assertGreater(position, middleware.index('django.contrib.sessions.middleware.SessionMiddleware'))


class TestRunnerAndInfrastructureTests(SimpleTestCase):

    def test_the_test_runner_gets_a_fast_hasher_and_no_redis(self):
        namespace = load({}, argv=['manage.py', 'test'])
        self.assertEqual(namespace['PASSWORD_HASHERS'], ['django.contrib.auth.hashers.MD5PasswordHasher'])
        self.assertEqual(namespace['CHANNEL_LAYERS']['default']['BACKEND'],
                         'channels.layers.InMemoryChannelLayer')

    def test_the_test_runner_queues_jobs_in_memory_never_in_redis(self):
        namespace = load({}, argv=['manage.py', 'test'])
        self.assertEqual(namespace['CELERY_BROKER_URL'], 'memory://')
        self.assertEqual(namespace['CELERY_RESULT_BACKEND'], 'cache+memory://')

    def test_any_other_command_keeps_the_real_hashers_and_redis(self):
        namespace = load({})
        self.assertNotIn('PASSWORD_HASHERS', namespace)
        self.assertEqual(namespace['CHANNEL_LAYERS']['default']['BACKEND'],
                         'channels_redis.core.RedisChannelLayer')

    def test_redis_defaults_to_database_one_so_devperfs_is_never_shared(self):
        self.assertTrue(load({})['REDIS_URL'].endswith('/1'))
        namespace = load({'REDIS_URL': 'redis://cache:6379/5'})
        self.assertEqual(namespace['CELERY_BROKER_URL'], 'redis://cache:6379/5')
        self.assertEqual(namespace['CHANNEL_LAYERS']['default']['CONFIG']['hosts'][0]['address'],
                         'redis://cache:6379/5')

    def test_celery_accepts_only_json(self):
        self.assertEqual(settings.CELERY_ACCEPT_CONTENT, ['application/json'])
        self.assertEqual(settings.CELERY_TASK_SERIALIZER, 'json')
        self.assertEqual(settings.CELERY_RESULT_SERIALIZER, 'json')

    def test_static_compression_is_enabled_only_for_a_non_test_production_run(self):
        production_namespace = load(production())
        middleware = production_namespace['MIDDLEWARE']
        self.assertEqual(middleware[1], 'whitenoise.middleware.WhiteNoiseMiddleware')
        self.assertEqual(
            production_namespace['STORAGES']['staticfiles']['BACKEND'],
            'whitenoise.storage.CompressedManifestStaticFilesStorage',
        )

        test_namespace = load(production(), argv=['manage.py', 'test'])
        self.assertNotIn('whitenoise.middleware.WhiteNoiseMiddleware', test_namespace['MIDDLEWARE'])
        self.assertNotIn('STORAGES', test_namespace)


class DevperfSourceDatabaseTests(SimpleTestCase):

    def test_no_devperf_database_exists_unless_configured(self):
        self.assertNotIn('devperf', load({})['DATABASES'])

    def test_it_is_reached_through_the_devperf_db_variables(self):
        databases = load({'DEVPERF_DB_NAME': 'devperf', 'DEVPERF_DB_USER': 'reader',
                          'DEVPERF_DB_PASSWORD': 'pw', 'DEVPERF_DB_HOST': 'db.internal',
                          'DEVPERF_DB_PORT': '5432'})['DATABASES']
        self.assertEqual(databases['devperf']['NAME'], 'devperf')
        self.assertEqual(databases['devperf']['USER'], 'reader')
        self.assertEqual(databases['devperf']['HOST'], 'db.internal')
        self.assertIn('postgresql', databases['devperf']['ENGINE'])
        self.assertIn('sqlite3', databases['default']['ENGINE'])

    def test_the_test_runner_gets_an_in_memory_stand_in(self):
        databases = load({}, argv=['manage.py', 'test'])['DATABASES']
        self.assertEqual(databases['devperf']['NAME'], ':memory:')


class ProductionTests(SimpleTestCase):

    def test_a_correct_deployment_gets_every_protection(self):
        namespace = load(production())
        self.assertTrue(namespace['SESSION_COOKIE_SECURE'])
        self.assertTrue(namespace['CSRF_COOKIE_SECURE'])
        self.assertTrue(namespace['SESSION_COOKIE_HTTPONLY'])
        self.assertEqual(namespace['SESSION_COOKIE_SAMESITE'], 'Lax')
        self.assertTrue(namespace['SECURE_SSL_REDIRECT'])
        self.assertEqual(namespace['SECURE_HSTS_SECONDS'], 30 * 24 * 60 * 60)
        self.assertEqual(namespace['X_FRAME_OPTIONS'], 'DENY')
        self.assertEqual(namespace['ALLOWED_HOSTS'], ['livegrade.example.com'])

    def test_hsts_does_not_cover_subdomains_or_preload_until_it_is_asked_to(self):
        namespace = load(production())
        self.assertFalse(namespace['SECURE_HSTS_INCLUDE_SUBDOMAINS'])
        self.assertFalse(namespace['SECURE_HSTS_PRELOAD'])

    def test_hsts_can_be_extended_to_subdomains_and_preload_from_the_environment(self):
        namespace = load(production(SECURE_HSTS_INCLUDE_SUBDOMAINS='True', SECURE_HSTS_PRELOAD='True',
                                    SECURE_HSTS_SECONDS='31536000'))
        self.assertTrue(namespace['SECURE_HSTS_INCLUDE_SUBDOMAINS'])
        self.assertTrue(namespace['SECURE_HSTS_PRELOAD'])
        self.assertEqual(namespace['SECURE_HSTS_SECONDS'], 31536000)

    def test_the_https_redirect_can_be_switched_off_behind_a_terminating_proxy(self):
        self.assertFalse(load(production(SECURE_SSL_REDIRECT='False'))['SECURE_SSL_REDIRECT'])

    def test_the_published_default_secret_key_is_refused(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'SECRET_KEY'):
            load(production(SECRET_KEY='django-insecure-livegrade-change-in-production'))

    def test_a_short_secret_key_is_refused(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'SECRET_KEY'):
            load(production(SECRET_KEY='short'))

    def test_missing_allowed_hosts_is_refused(self):
        env = production()
        del env['ALLOWED_HOSTS']
        with self.assertRaisesMessage(ImproperlyConfigured, 'ALLOWED_HOSTS'):
            load(env)

    def test_missing_sign_in_credentials_are_refused(self):
        for missing in ('OIDC_RP_CLIENT_ID', 'OIDC_RP_CLIENT_SECRET'):
            env = production()
            del env[missing]
            with self.subTest(missing=missing), self.assertRaisesMessage(ImproperlyConfigured, 'OIDC_RP_CLIENT_ID'):
                load(env)

    def test_the_test_runner_is_not_forced_onto_https(self):
        namespace = load(production(), argv=['manage.py', 'test'])
        self.assertNotIn('SECURE_SSL_REDIRECT', namespace)


class NetutilsTests(SimpleTestCase):

    def test_an_offline_machine_falls_back_to_loopback(self):
        from config import netutils

        with mock.patch('config.netutils.socket.socket') as factory:
            factory.return_value.connect.side_effect = OSError('no route')
            self.assertEqual(netutils.detect_lan_ip(), '127.0.0.1')
            factory.return_value.close.assert_called_once()

    def test_a_connected_machine_reports_its_own_address(self):
        from config import netutils

        with mock.patch('config.netutils.socket.socket') as factory:
            factory.return_value.getsockname.return_value = ('192.168.1.20', 0)
            self.assertEqual(netutils.detect_lan_ip(), '192.168.1.20')


class WiringTests(SimpleTestCase):

    def test_the_celery_app_is_the_one_django_loads(self):
        from config import celery_app

        self.assertEqual(celery_app.main, 'livegrade')

    def test_the_asgi_application_routes_http_and_websockets(self):
        from config.asgi import application

        self.assertEqual(set(application.application_mapping), {'http', 'websocket'})

    def test_the_wsgi_application_is_importable(self):
        from config.wsgi import application
        self.assertTrue(callable(application))

    def test_manage_py_runs_a_command(self):
        manage = runpy.run_path(str(Path(settings.BASE_DIR) / 'manage.py'))
        with mock.patch('django.core.management.execute_from_command_line') as execute:
            with mock.patch.object(sys, 'argv', ['manage.py', 'check']):
                manage['main']()
        execute.assert_called_once_with(['manage.py', 'check'])
