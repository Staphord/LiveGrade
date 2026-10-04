from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('no-access/', views.no_access, name='no_access'),
    path('switch-account/', views.switch_account, name='switch_account'),
    path('switch-organization/', views.switch_organization, name='switch_organization'),
    path('healthz', views.health, name='health'),
]
