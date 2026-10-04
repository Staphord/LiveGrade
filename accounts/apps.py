from django.apps import AppConfig
from django.conf import settings


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'accounts'

    def ready(self):
        if settings.FRIENDLY_ERROR_PAGES:
            from accounts import error_pages

            error_pages.install()
