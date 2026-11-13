"""Test helper: put an exhibit on the showcase stage the way the console does."""


def put_on_stage(event, exhibit):
    event.refresh_from_db()
    snapshot = dict(event.workflow_snapshot or {})
    stage = dict(snapshot.get('stage_state', {}))
    stage['active_exhibit_id'] = exhibit.pk
    snapshot['stage_state'] = stage
    event.workflow_snapshot = snapshot
    event.save(update_fields=('workflow_snapshot', 'updated_at'))
