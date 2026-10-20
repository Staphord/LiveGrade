from django.urls import include, path

# No Django admin: LiveGrade has no passwords, and a staff login of its own
# would be a second way in that DevPerf's single sign-in cannot govern.
urlpatterns = [
    path('oidc/', include('accounts.oidc_urls')),
    path('assessments/', include('assessments.urls')),
    path('assess/', include('assessments.student_urls')),
    path('', include('accounts.urls')),
]

# Django would use its own 500 view, which renders with no request and so no CSP nonce.
handler500 = 'accounts.error_pages.server_error'
