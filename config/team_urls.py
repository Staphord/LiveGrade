"""Team-specific URL root sharing the assessment engine under Team paths."""
from django.urls import include, path

from .urls import urlpatterns as platform_patterns


urlpatterns = [
    path('team/app/', include('assessments.urls')),
    *platform_patterns,
]
