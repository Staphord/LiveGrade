from django.urls import path

from .consumers import AssessmentSessionConsumer, AssessmentStaffSessionConsumer
from service_domains.consumers import (
    CompetitionLiveConsumer,
    CompetitionLeaderboardConsumer,
    ShowcaseDisplayConsumer,
    ShowcaseJudgeConsumer,
    ShowcaseLiveConsumer,
    TrainingSessionLiveConsumer,
)

websocket_urlpatterns = [
    path('ws/competition/<int:competition_id>/live/',
         CompetitionLiveConsumer.as_asgi()),
    path('ws/competition/<uuid:competition_id>/leaderboard/',
         CompetitionLeaderboardConsumer.as_asgi()),
    path('ws/assessments/<uuid:session_uuid>/staff/', AssessmentStaffSessionConsumer.as_asgi()),
    path('ws/assessments/<uuid:session_uuid>/', AssessmentSessionConsumer.as_asgi()),
    path('ws/showcase/event/<int:event_id>/live/', ShowcaseLiveConsumer.as_asgi()),
    path('ws/showcase/judge/<str:token>/', ShowcaseJudgeConsumer.as_asgi()),
    path('ws/showcase/display/<uuid:public_id>/', ShowcaseDisplayConsumer.as_asgi()),
    path('ws/training/session/<int:session_id>/live/', TrainingSessionLiveConsumer.as_asgi()),
]
