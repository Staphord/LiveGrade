from django.urls import path
from mozilla_django_oidc.views import OIDCAuthenticationRequestView, OIDCLogoutView

from .oidc import CallbackView

urlpatterns = [
    path('callback/', CallbackView.as_view(), name='oidc_authentication_callback'),
    path('authenticate/', OIDCAuthenticationRequestView.as_view(), name='oidc_authentication_init'),
    path('logout/', OIDCLogoutView.as_view(), name='oidc_logout'),
]
