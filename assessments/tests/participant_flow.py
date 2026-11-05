"""Participant test helpers that follow the real roster and notice flow."""

from django.urls import reverse


def join_as(client, session_uuid, identifier):
    """Join through the public form, view the current notice, then continue."""
    join_url = reverse('student_join', args=[session_uuid])
    client.get(join_url)
    response = client.post(join_url, {'identifier': identifier})
    if response.status_code == 302 and response.url == reverse('student_evaluate', args=[session_uuid]):
        notice_url = reverse('student_privacy_notice', args=[session_uuid])
        notice = client.get(notice_url)
        if notice.status_code == 200 and b'name="action" value="continue"' in notice.content:
            client.post(notice_url, {'action': 'continue'})
    return response
