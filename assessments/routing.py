from django.urls import path

from .consumers import AssessmentSessionConsumer, AssessmentStaffSessionConsumer
from service_domains.consumers import CompetitionLeaderboardConsumer

websocket_urlpatterns = [
    path('ws/competition/<uuid:competition_id>/leaderboard/',
         CompetitionLeaderboardConsumer.as_asgi()),
    path('ws/assessments/<uuid:session_uuid>/staff/', AssessmentStaffSessionConsumer.as_asgi()),
    path('ws/assessments/<uuid:session_uuid>/', AssessmentSessionConsumer.as_asgi()),
]
