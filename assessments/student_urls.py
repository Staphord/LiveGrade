from django.urls import path

from . import views

urlpatterns = [
    path('<uuid:session_uuid>/join/', views.join, name='student_join'),
    path('<uuid:session_uuid>/evaluate/', views.evaluate, name='student_evaluate'),
    path('<uuid:session_uuid>/submitted/', views.submitted, name='student_submitted'),
    path('<uuid:session_uuid>/check-expiry/', views.check_turn_expiry, name='student_check_expiry'),
    path('<uuid:session_uuid>/grading-ping/', views.grading_ping, name='student_grading_ping'),
    path('<uuid:session_uuid>/join-screen/', views.join_screen, name='assessment_join_screen'),
    path('<uuid:session_uuid>/join-screen/state/', views.join_screen_state, name='assessment_join_screen_state'),
    path('<uuid:session_uuid>/join-screen/qr.png', views.join_screen_qr, name='assessment_join_screen_qr'),
]
