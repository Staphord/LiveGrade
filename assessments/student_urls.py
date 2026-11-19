from django.urls import path

from . import views
from .views import poll_views, unified_views

urlpatterns = [
    path('polls/<uuid:poll_uuid>/', poll_views.public_poll, name='classroom_poll'),
    path('polls/<uuid:poll_uuid>/results/', poll_views.public_poll_results, name='classroom_poll_results'),
    path('polls/<uuid:poll_uuid>/qr-screen/', poll_views.poll_qr_screen, name='classroom_poll_qr_screen'),
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
    path('examiner/<str:token>/', unified_views.examiner_portal, name='assessment_examiner_portal'),
    path('examiner/<str:token>/state/', unified_views.examiner_portal_state, name='assessment_examiner_portal_state'),
    path('<uuid:session_uuid>/submit/<int:group_pk>/', unified_views.async_submit_entry, name='assessment_async_submit_entry'),
    path('review/<int:assignment_id>/', unified_views.assignment_review, name='assessment_assignment_review'),
    path('review/<int:assignment_id>/draft/', unified_views.assignment_draft, name='assessment_assignment_draft'),
    path('review/<int:assignment_id>/submit/', unified_views.assignment_submit, name='assessment_assignment_submit'),
    path('review/<int:assignment_id>/recuse/', unified_views.assignment_recuse, name='assessment_assignment_recuse'),
]
