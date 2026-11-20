from django.urls import path

from .oidc import CallbackView, DesktopAwareLogoutView, SignInStartView

urlpatterns = [
    path('callback/', CallbackView.as_view(), name='oidc_authentication_callback'),
    path('authenticate/', SignInStartView.as_view(), name='oidc_authentication_init'),
    path('logout/', DesktopAwareLogoutView.as_view(), name='oidc_logout'),
]
