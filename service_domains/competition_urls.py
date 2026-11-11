from django.urls import path

from .competition_views import (
    competition_audience_join, competition_bulk_delete, competition_create,
    competition_delete, competition_entry_import,
    competition_entry_import_sample, competition_list, competition_manage,
    competition_public_leaderboard, competition_results_export, competition_table,
    competition_live_control, competition_live_state, competition_live_action,
    competition_live_projector_toggle,
)
from .views import competition_audience_vote
from .competition_judge_views import competition_judge_scorecard


urlpatterns = [
    path('app/', competition_list, name='competition_list'),
    path('app/all/', competition_table, name='competition_table'),
    path('app/new/', competition_create, name='competition_create'),
    path('app/delete/', competition_bulk_delete, name='competition_bulk_delete'),
    path('app/<int:competition_id>/delete/', competition_delete, name='competition_delete'),
    path('app/<int:competition_id>/results.csv', competition_results_export, name='competition_results_export'),
    path('app/<int:competition_id>/import/', competition_entry_import, name='competition_entry_import'),
    path('app/<int:competition_id>/import/sample.xlsx', competition_entry_import_sample,
         name='competition_entry_import_sample'),
    path('app/<int:competition_id>/live/', competition_live_control,
         name='competition_live_control'),
    path('app/<int:competition_id>/live-state/', competition_live_state,
         name='competition_live_state'),
    path('app/<int:competition_id>/live-action/', competition_live_action,
         name='competition_live_action'),
    path('app/<int:competition_id>/live-projector/', competition_live_projector_toggle,
         name='competition_live_projector_toggle'),
    path('app/<int:competition_id>/', competition_manage, name='competition_manage'),
    path('results/<uuid:competition_id>/', competition_public_leaderboard,
         name='competition_public_leaderboard'),
    path('join/', competition_audience_join, name='competition_audience_join'),
    path('audience/<uuid:competition_id>/<int:round_id>/<int:track_id>/vote/',
         competition_audience_vote, name='competition_audience_vote'),
    path('judge/<str:token>/', competition_judge_scorecard,
         name='competition_judge_scorecard'),
]
