import uuid

from django.db import migrations, models


def populate_event_ids(apps, schema_editor):
    Event = apps.get_model('service_domains', 'ShowcaseEvent')
    alias = schema_editor.connection.alias
    for event in Event.objects.using(alias).filter(public_id__isnull=True).iterator():
        Event.objects.using(alias).filter(pk=event.pk).update(public_id=uuid.uuid4())


class Migration(migrations.Migration):
    dependencies = [('service_domains', '0002_allow_anonymous_service_records')]

    operations = [
        migrations.AddField(
            model_name='showcaseevent',
            name='public_id',
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(populate_event_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='showcaseevent',
            name='public_id',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
