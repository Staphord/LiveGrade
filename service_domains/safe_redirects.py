"""Redirects that can only stay on this site, whatever a form posted."""
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme


def safe_redirect(url, fallback='/'):
    """Redirect to ``url`` only when it is a path on this site; otherwise go to ``fallback``."""
    if not url_has_allowed_host_and_scheme(url, allowed_hosts=None):
        url = fallback
    return redirect(url)


def user_message(exc, fallback='This action could not be completed.'):
    """What to tell the person about a refused action: our own validation text, never the raw exception."""
    return '; '.join(exc.messages) if isinstance(exc, ValidationError) else fallback
