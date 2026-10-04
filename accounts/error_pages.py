"""Friendly 404 and 500 pages, even while DEBUG is on.

With DEBUG on Django ignores ``404.html`` and ``500.html`` and shows its technical
pages: a list of every URL pattern, the settings, the traceback. Useful to the
person writing the code, baffling to anybody else who mistypes an address on a
development or demo server. These replace the two technical responses with our
own pages. The traceback is not lost: Django has already written it to the log by
the time the response is built, so the terminal still shows what went wrong.

Turn it off (``FRIENDLY_ERROR_PAGES=False``) to get Django's technical pages back
while debugging.
"""
from django.views import debug
from django.views.defaults import page_not_found, server_error


def friendly_404(request, exception):
    return page_not_found(request, exception)


def friendly_500(request, exc_type, exc_value, tb):
    return server_error(request)


def install():
    debug.technical_404_response = friendly_404
    debug.technical_500_response = friendly_500
