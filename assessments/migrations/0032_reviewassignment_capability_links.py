import hashlib
import secrets
from datetime import timedelta

from django.db import migrations, models
from django.utils import timezone


def populate_review_capabilities(apps, schema_editor):
    database = schema_editor.connection.alias
    assignment_model = apps.get_model('assessments', 'ReviewAssignment')
    expires_at = timezone.now() + timedelta(days=14)
    for assignment in assignment_model.objects.using(database).all().iterator():
        token = secrets.token_urlsafe(32)
        assignment_model.objects.using(database).filter(pk=assignment.pk).update(
            invite_token=token,
            token_digest=hashlib.sha256(token.encode('utf-8')).hexdigest(),
            expires_at=expires_at,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('assessments', '0031_panelexaminer_delivery_error_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='reviewassignment',
            name='invite_token',
            field=models.CharField(blank=True, default='', editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name='reviewassignment',
            name='token_digest',
            field=models.CharField(blank=True, default='', editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name='reviewassignment',
            name='expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(populate_review_capabilities, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='reviewassignment',
            name='invite_token',
            field=models.CharField(blank=True, default='', editable=False, max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name='reviewassignment',
            name='token_digest',
            field=models.CharField(blank=True, default='', editable=False, max_length=64, unique=True),
        ),
    ]
