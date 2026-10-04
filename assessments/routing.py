from django.urls import path

from .consumers import AssessmentSessionConsumer

websocket_urlpatterns = [
    path('ws/assessments/<uuid:session_uuid>/', AssessmentSessionConsumer.as_asgi()),
]
