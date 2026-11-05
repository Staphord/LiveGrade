from django.urls import path

from .consumers import AssessmentSessionConsumer, AssessmentStaffSessionConsumer

websocket_urlpatterns = [
    path('ws/assessments/<uuid:session_uuid>/staff/', AssessmentStaffSessionConsumer.as_asgi()),
    path('ws/assessments/<uuid:session_uuid>/', AssessmentSessionConsumer.as_asgi()),
]
