"""Team-specific URL root sharing the assessment engine under Team paths."""
from django.urls import include, path

from .urls import urlpatterns as platform_patterns


urlpatterns = []
for pattern in platform_patterns:
    # Keep the existing Classroom URL names and views but resolve them beneath
    # the Team origin for requests whose trusted service path is /team/.
    if str(pattern.pattern) == 'classroom/app/':
        urlpatterns.append(path('team/app/', include('assessments.urls')))
    else:
        urlpatterns.append(pattern)
