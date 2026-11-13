import logging

from django.apps import AppConfig
from django.conf import settings


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'accounts'

    def ready(self):
        from accounts.logging import BearerUrlRedactionFilter

        for logger_name in ('django.request', 'django.server', 'daphne.access', 'daphne.server'):
            logger = logging.getLogger(logger_name)
            if not any(isinstance(item, BearerUrlRedactionFilter)
                       for item in logger.filters):
                logger.addFilter(BearerUrlRedactionFilter())

        if settings.FRIENDLY_ERROR_PAGES:
            from accounts import error_pages

            error_pages.install()
