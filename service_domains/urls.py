from django.urls import path

from .views import (
    rate_showcase_exhibit, showcase_exhibit_label, showcase_exhibit_qr,
    showcase_event_labels,
    showcase_public_results, showcase_event_list, showcase_event_create, showcase_event_manage,
    showcase_exhibit_import, showcase_exhibit_import_sample,
    showcase_event_transition, showcase_certificate_verify,
    showcase_judge_workspace, showcase_live_monitor, showcase_live_activity_view,
)


urlpatterns = [
    path('organizer/', showcase_event_list, name='showcase_event_list'),
    path('organizer/events/new/', showcase_event_create, name='showcase_event_create'),
    path('organizer/events/<int:event_id>/', showcase_event_manage, name='showcase_event_manage'),
    path('organizer/events/<int:event_id>/live/', showcase_live_monitor,
         name='showcase_live_monitor'),
    path('organizer/events/<int:event_id>/live/activity/', showcase_live_activity_view,
         name='showcase_live_activity'),
    path('organizer/events/<int:event_id>/transition/', showcase_event_transition,
         name='showcase_event_transition'),
    path('organizer/events/<int:event_id>/import/', showcase_exhibit_import, name='showcase_exhibit_import'),
    path('organizer/events/<int:event_id>/import/sample.xlsx', showcase_exhibit_import_sample,
         name='showcase_exhibit_import_sample'),
    path('organizer/events/<int:event_id>/labels/', showcase_event_labels,
         name='showcase_event_labels'),
    path('judge/<str:token>/', showcase_judge_workspace, name='showcase_judge_workspace'),
    path('exhibits/<uuid:exhibit_id>/rate/', rate_showcase_exhibit, name='showcase_rate_exhibit'),
    path('exhibits/<uuid:exhibit_id>/label/', showcase_exhibit_label, name='showcase_exhibit_label'),
    path('exhibits/<uuid:exhibit_id>/qr.png', showcase_exhibit_qr, name='showcase_exhibit_qr'),
    path('events/<uuid:public_id>/results/', showcase_public_results, name='showcase_public_results'),
    path('certificates/<uuid:public_id>/', showcase_certificate_verify,
         name='showcase_certificate_verify'),
]
