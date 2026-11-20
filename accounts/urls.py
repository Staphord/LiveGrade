from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('no-access/', views.no_access, name='no_access'),
    path('switch-account/', views.switch_account, name='switch_account'),
    path('switch-organization/', views.switch_organization, name='switch_organization'),
    path('accounts/desktop/login/', views.desktop_login, name='desktop_login'),
    path('accounts/desktop/handoff/', views.desktop_handoff, name='desktop_handoff'),
    path('accounts/desktop/exchange/', views.desktop_exchange, name='desktop_exchange'),
    path('download/', views.desktop_download, name='desktop_download'),
    path('readyz', views.readiness, name='readiness'),
    path('healthz', views.health, name='health'),
]
