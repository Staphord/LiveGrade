"""Request-aware URL helpers for shared service views."""

from django.shortcuts import redirect
from django.urls import reverse


def request_reverse(request, viewname, *route_args, args=None, kwargs=None, current_app=None, **route_kwargs):
    """Reverse a route using the URL configuration selected for this request."""
    reverse_kwargs = dict(kwargs or {})
    reverse_kwargs.update(route_kwargs)
    return reverse(
        viewname,
        args=args if args is not None else (route_args or None),
        kwargs=reverse_kwargs or None,
        current_app=current_app,
        urlconf=getattr(request, 'urlconf', None),
    )


def request_redirect(request, viewname, *args, **kwargs):
    """Redirect to a named route within the current service URL space."""
    return redirect(request_reverse(request, viewname, *args, **kwargs))
