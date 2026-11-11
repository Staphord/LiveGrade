from django.urls import path

from .team_reports import training_learner_report

from .views import (training_anonymous_feedback, training_series_list,
                    training_series_table, training_series_create,
                    training_series_delete, training_series_bulk_delete,
                    training_series_delete_export, training_series_bulk_delete_export,
                    training_series_manage, training_series_trends,
                    training_series_export, training_certificate_verify,
                    training_series_roster_import, training_series_roster_preview,
                    training_series_roster_sample)

urlpatterns = [
    path('results/<str:token>/', training_learner_report, name='training_learner_report'),
    path('certificates/<uuid:public_id>/', training_certificate_verify,
         name='training_certificate_verify'),
    path('feedback/<uuid:feedback_public_id>/', training_anonymous_feedback,
         name='training_anonymous_feedback'),
    path('series/', training_series_list, name='training_series_list'),
    path('series/all/', training_series_table, name='training_series_table'),
    path('series/new/', training_series_create, name='training_series_create'),
    path('series/delete/export/', training_series_bulk_delete_export,
         name='training_series_bulk_delete_export'),
    path('series/delete/', training_series_bulk_delete, name='training_series_bulk_delete'),
    path('series/<int:series_id>/delete/export/', training_series_delete_export,
         name='training_series_delete_export'),
    path('series/<int:series_id>/delete/', training_series_delete, name='training_series_delete'),
    path('series/<int:series_id>/', training_series_manage, name='training_series_manage'),
    path('series/<int:series_id>/trends/', training_series_trends, name='training_series_trends'),
    path('series/<int:series_id>/outcomes.csv', training_series_export,
         name='training_series_export'),
    path('series/<int:series_id>/roster/import/', training_series_roster_import,
         name='training_series_roster_import'),
    path('series/<int:series_id>/roster/preview/', training_series_roster_preview,
         name='training_series_roster_preview'),
    path('series/<int:series_id>/roster/sample.xlsx', training_series_roster_sample,
         name='training_series_roster_sample'),
]
