import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


def backfill_owner_scope(apps, schema_editor):
    Poll = apps.get_model('assessments', 'ClassroomPoll')
    Session = apps.get_model('assessments', 'AssessmentSession')
    for poll in Poll.objects.select_related('assessment_session').iterator():
        session = poll.assessment_session
        poll.organization_id = session.organization_id
        poll.created_by_id = session.created_by_id
        poll.save(update_fields=['organization_id', 'created_by'])


class Migration(migrations.Migration):
    dependencies = [
        ('assessments', '0020_team_peer_scoring_privacy_notice'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AddField('classroompoll', 'organization_id', models.BigIntegerField(blank=True, db_index=True, null=True)),
        migrations.AddField('classroompoll', 'created_by', models.ForeignKey(blank=True, null=True,
            on_delete=django.db.models.deletion.SET_NULL, related_name='standalone_polls_created', to=settings.AUTH_USER_MODEL)),
        migrations.AddField('classroompoll', 'standalone', models.BooleanField(db_index=True, default=False)),
        migrations.RunPython(backfill_owner_scope, migrations.RunPython.noop),
        migrations.AlterField('classroompoll', 'assessment_session', models.ForeignKey(blank=True, null=True,
            on_delete=django.db.models.deletion.CASCADE, related_name='polls', to='assessments.assessmentsession')),
        migrations.AddConstraint('classroompoll', models.CheckConstraint(
            condition=(models.Q(('standalone', True), ('assessment_session__isnull', True), ('organization_id__isnull', False))
                       | models.Q(('standalone', False), ('assessment_session__isnull', False))),
            name='classroom_poll_session_or_standalone_scope')),
        migrations.CreateModel(name='AnonymousClassroomPollResponse', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('token', models.UUIDField(default=uuid.uuid4, editable=False)),
            ('answers', models.JSONField(blank=True, default=dict)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('updated_at', models.DateTimeField(auto_now=True)),
            ('poll', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                related_name='anonymous_responses', to='assessments.classroompoll')),
        ], options={'constraints': [models.UniqueConstraint(fields=('poll', 'token'), name='anonymous_poll_browser_once')]}),
        migrations.CreateModel(name='StandalonePollAudit', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('summary', models.CharField(max_length=255)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('by_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='+', to=settings.AUTH_USER_MODEL)),
            ('poll', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                related_name='standalone_audit', to='assessments.classroompoll')),
        ], options={'ordering': ['-created_at', '-pk']}),
    ]
