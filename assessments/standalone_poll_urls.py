from django.urls import path
from .views import standalone_poll_views as views

urlpatterns = [
    path('app/', views.standalone_poll_list, name='standalone_poll_list'),
    path('app/delete/', views.standalone_poll_delete, name='standalone_poll_delete'),
    path('questions/sample.xlsx', views.standalone_poll_sample, name='standalone_poll_sample'),
    path('state/<uuid:poll_uuid>/', views.standalone_poll_state, name='standalone_poll_state'),
    path('live/<uuid:poll_uuid>/', views.standalone_poll_live_results, name='standalone_poll_live_results'),
    path('live/<uuid:poll_uuid>/state/', views.standalone_poll_live_state, name='standalone_poll_live_state'),
    path('display/<uuid:poll_uuid>/', views.standalone_poll_qr_screen, name='standalone_poll_qr_screen'),
    path('qr/<uuid:poll_uuid>/', views.standalone_poll_qr, name='standalone_poll_qr'),
    path('vote/<uuid:poll_uuid>/', views.standalone_poll_public, name='standalone_poll_public'),
]
