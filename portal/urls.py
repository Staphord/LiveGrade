from django.urls import path, register_converter

from . import views
from .converters import ServiceSlugConverter
from accounts.views import switch_organization

register_converter(ServiceSlugConverter, 'service')

urlpatterns = [
    path('<service:slug>/', views.entry, name='service_entry'),
    path('<service:slug>/notify/', views.service_interest, name='service_interest'),
    path('<service:slug>/switch-organization/', switch_organization, name='service_switch_organization'),
]
