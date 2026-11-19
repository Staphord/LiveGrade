"""Team-specific URL root sharing the assessment engine under Team paths."""
from django.urls import include, path

from .urls import urlpatterns as platform_patterns


urlpatterns = [
    *platform_patterns,
    # Keep the shared assessment route names bound to their Team paths inside
    # this URLconf, rather than the later-added generic Assessment paths.
    path('team/app/', include('assessments.urls')),
]
