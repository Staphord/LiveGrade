"""Compatibility for Classroom owner bookmarks and forms from before the move."""
from django.http import HttpResponsePermanentRedirect
from django.urls import resolve, reverse


def legacy_assessments_redirect(request, legacy_path=''):
    # Resolve against the old route table, then generate the canonical new URL.
    # Arbitrary suffixes (including traversal) cannot choose a redirect target.
    match = resolve('/' + legacy_path, urlconf='assessments.urls')
    destination = reverse(match.url_name, kwargs=match.kwargs)
    query = request.META.get('QUERY_STRING', '')
    if query:
        destination += '?' + query
    return HttpResponsePermanentRedirect(destination, preserve_request=True)
