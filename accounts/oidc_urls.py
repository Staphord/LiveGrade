from django.urls import path
from mozilla_django_oidc.views import OIDCLogoutView

from .oidc import CallbackView, SignInStartView

urlpatterns = [
    path('callback/', CallbackView.as_view(), name='oidc_authentication_callback'),
    path('authenticate/', SignInStartView.as_view(), name='oidc_authentication_init'),
    path('logout/', OIDCLogoutView.as_view(), name='oidc_logout'),
]
