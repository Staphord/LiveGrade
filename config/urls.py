from django.urls import include, path

from service_domains.views import training_session_competencies

# No Django admin: LiveGrade has no passwords, and a staff login of its own
# would be a second way in that DevPerf's single sign-in cannot govern.
urlpatterns = [
    path('oidc/', include('accounts.oidc_urls')),
    path('assessment/app/', include('assessments.urls')),
    path('polls/', include('assessments.standalone_poll_urls')),
    path('assess/', include('assessments.student_urls')),
    # Event-scoped Showcase participant links do not require owner sign-in.
    path('showcase/', include('service_domains.urls')),
    path('competition/', include('service_domains.competition_urls')),
    path('team/app/<int:pk>/competencies/', training_session_competencies,
         name='training_session_competencies'),
    path('team/', include('service_domains.team_urls')),
    path('', include('accounts.urls')),
    # Last: the service entry addresses (/assessment/ ...) match only registered slugs, and
    # nothing above can be shadowed by them.
    path('', include('portal.urls')),
]

# Django would use its own 500 view, which renders with no request and so no CSP nonce.
handler500 = 'accounts.error_pages.server_error'
