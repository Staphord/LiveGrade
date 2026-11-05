from django.urls import path

from . import views
from .views import poll_views

urlpatterns = [
    path('polls/<uuid:poll_uuid>/', poll_views.public_poll, name='classroom_poll'),
    path('polls/<uuid:poll_uuid>/results/', poll_views.public_poll_results, name='classroom_poll_results'),
    path('polls/<uuid:poll_uuid>/qr.png', poll_views.poll_qr, name='classroom_poll_qr'),
    path('poll-invitation/<uuid:token>/', poll_views.poll_invitation, name='classroom_poll_invitation'),
    path('<uuid:session_uuid>/join/', views.join, name='student_join'),
    path('<uuid:session_uuid>/privacy/', views.privacy_notice, name='student_privacy_notice'),
    path('<uuid:session_uuid>/evaluate/', views.evaluate, name='student_evaluate'),
    path('<uuid:session_uuid>/submitted/', views.submitted, name='student_submitted'),
    path('<uuid:session_uuid>/check-expiry/', views.check_turn_expiry, name='student_check_expiry'),
    path('<uuid:session_uuid>/grading-ping/', views.grading_ping, name='student_grading_ping'),
    path('<uuid:session_uuid>/join-screen/', views.join_screen, name='assessment_join_screen'),
    path('<uuid:session_uuid>/join-screen/state/', views.join_screen_state, name='assessment_join_screen_state'),
    path('<uuid:session_uuid>/join-screen/qr.png', views.join_screen_qr, name='assessment_join_screen_qr'),
]
