from django.urls import path

from .competition_views import (competition_audience_join, competition_entry_import,
    competition_entry_import_sample, competition_list, competition_manage,
    competition_public_leaderboard, competition_results_export)
from .views import competition_audience_vote
from .competition_judge_views import competition_judge_scorecard


urlpatterns = [
    path('app/', competition_list, name='competition_list'),
    path('app/<int:competition_id>/results.csv', competition_results_export, name='competition_results_export'),
    path('app/<int:competition_id>/import/', competition_entry_import, name='competition_entry_import'),
    path('app/<int:competition_id>/import/sample.xlsx', competition_entry_import_sample,
         name='competition_entry_import_sample'),
    path('app/<int:competition_id>/', competition_manage, name='competition_manage'),
    path('results/<uuid:competition_id>/', competition_public_leaderboard,
         name='competition_public_leaderboard'),
    path('join/', competition_audience_join, name='competition_audience_join'),
    path('audience/<uuid:competition_id>/<int:round_id>/<int:track_id>/vote/',
         competition_audience_vote, name='competition_audience_vote'),
    path('judge/<str:token>/', competition_judge_scorecard,
         name='competition_judge_scorecard'),
]
