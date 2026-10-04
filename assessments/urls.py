from django.urls import path

from . import views

urlpatterns = [
    path('', views.assessment_session_list, name='assessment_session_list'),
    path('new/', views.assessment_session_create, name='assessment_session_create'),
    path('<int:pk>/', views.assessment_session_detail, name='assessment_session_detail'),
    path('<int:pk>/edit/', views.assessment_session_edit, name='assessment_session_edit'),
    path('<int:pk>/live-state/', views.assessment_live_state, name='assessment_live_state'),
    path('<int:pk>/delete/', views.assessment_session_delete, name='assessment_session_delete'),

    path('<int:pk>/roster/', views.roster, name='assessment_roster'),
    path('<int:pk>/roster/search/', views.roster_search, name='assessment_roster_search'),
    path('<int:pk>/roster/import/', views.roster_import_view, name='assessment_roster_import'),
    path('<int:pk>/roster/<int:student_pk>/edit/', views.roster_edit, name='assessment_roster_edit'),
    path('<int:pk>/roster/<int:student_pk>/delete/', views.roster_delete, name='assessment_roster_delete'),

    path('<int:pk>/rubric/', views.rubric, name='assessment_rubric'),
    path('<int:pk>/rubric/<int:category_pk>/edit/', views.rubric_edit, name='assessment_rubric_edit'),
    path('<int:pk>/rubric/<int:category_pk>/delete/', views.rubric_delete, name='assessment_rubric_delete'),

    path('<int:pk>/groups/', views.groups, name='assessment_groups'),
    path('<int:pk>/groups/import/', views.group_import_view, name='assessment_group_import'),
    path('<int:pk>/groups/<int:group_pk>/edit/', views.group_edit, name='assessment_group_edit'),
    path('<int:pk>/groups/<int:group_pk>/members/', views.group_members, name='assessment_group_members'),
    path('<int:pk>/groups/<int:group_pk>/delete/', views.group_delete, name='assessment_group_delete'),

    path('<int:pk>/go-live/', views.go_live, name='assessment_go_live'),
    path('<int:pk>/activate/<int:group_pk>/', views.activate_group, name='assessment_activate_group'),
    path('<int:pk>/set-gap/', views.set_transition_gap, name='assessment_set_transition_gap'),
    path('<int:pk>/set-timer/', views.set_default_turn_seconds, name='assessment_set_default_turn_seconds'),
    path('<int:pk>/set-presentation-timer/', views.set_default_presentation_seconds, name='assessment_set_presentation_timer'),
    path('<int:pk>/set-next-group/', views.set_designated_next_group, name='assessment_set_next_group'),
    path('<int:pk>/pause/', views.toggle_pause, name='assessment_toggle_pause'),
    path('<int:pk>/joining/', views.toggle_joining, name='assessment_toggle_joining'),
    path('<int:pk>/adjust-timer/', views.adjust_turn_timer, name='assessment_adjust_timer'),
    path('<int:pk>/close-turn/', views.close_turn, name='assessment_close_turn'),
    path('<int:pk>/close/', views.close_session, name='assessment_close_session'),

    path('<int:pk>/participation/', views.participation, name='assessment_participation'),
    path('<int:pk>/participation/refresh/', views.participation_refresh, name='assessment_participation_refresh'),
    path('<int:pk>/results/', views.results, name='assessment_results'),
    path('<int:pk>/results/refresh/', views.results_refresh, name='assessment_results_refresh'),
    path('<int:pk>/results/export/', views.results_export, name='assessment_results_export'),
    path('<int:pk>/qr.png', views.session_qr, name='assessment_qr'),
]
