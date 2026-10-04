from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def broadcast_session_event(assessment_session, event_type, data=None):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    async_to_sync(channel_layer.group_send)(
        f'assessment_session_{assessment_session.uuid}',
        {'type': 'assessment_update', 'event_type': event_type, 'data': data or {}},
    )
