from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def broadcast_session_event(assessment_session, event_type, data=None):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    event = {'type': 'assessment_update', 'event_type': event_type, 'data': data or {}}
    # The UUID-only participant socket is deliberately public. Evaluator
    # identity/progress is useful to the owner, but must never be broadcast to
    # every participant who can scan the room QR code. Mirror the full event
    # only to the authenticated, organization-scoped staff socket.
    private_event = event_type in {'grading.progress', 'evaluation.submitted'}
    public_event = dict(event)
    if private_event:
        public_event.update(event_type='activity.updated', data={})
    async_to_sync(channel_layer.group_send)(
        f'assessment_session_{assessment_session.uuid}', public_event,
    )
    async_to_sync(channel_layer.group_send)(
        f'assessment_owner_session_{assessment_session.uuid}', event,
    )
