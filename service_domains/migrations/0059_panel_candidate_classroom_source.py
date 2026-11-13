from django.db import migrations, models
import django.db.models.deletion


def snapshot_existing_group_memberships(apps, schema_editor):
    Mapping = apps.get_model('service_domains', 'CombinedPresentationMapping')
    Membership = apps.get_model('assessments', 'GroupMembership')
    for mapping in Mapping.objects.all().iterator():
        mapping.group_member_ids_snapshot = sorted(Membership.objects.filter(
            group_id=mapping.group_id).values_list('student_id', flat=True))
        mapping.save(update_fields=['group_member_ids_snapshot'])


class Migration(migrations.Migration):
    dependencies = [
        ('assessments', '0020_team_peer_scoring_privacy_notice'),
        ('service_domains', '0058_alter_showcaseevent_minimum_judges_per_exhibit'),
    ]

    operations = [
        migrations.AddField(
            model_name='panelcandidate',
            name='source_group',
            field=models.ForeignKey(blank=True, help_text='Optional Classroom group this Panel entry represents.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='panel_candidates', to='assessments.presentationgroup'),
        ),
        migrations.AddConstraint(
            model_name='panelcandidate',
            constraint=models.UniqueConstraint(condition=models.Q(('source_group__isnull', False)), fields=('hearing', 'source_group'), name='panel_candidate_source_group_once'),
        ),
        migrations.AddField(
            model_name='combinedpresentationmapping',
            name='group_member_ids_snapshot',
            field=models.JSONField(blank=True, default=list, help_text='Classroom student IDs captured when this entry mapping is confirmed.'),
        ),
        migrations.RunPython(snapshot_existing_group_memberships, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='panelauditevent',
            name='action',
            field=models.CharField(max_length=24, choices=[
                ('hearing_created', 'Hearing created'), ('hearing_opened', 'Hearing opened'),
                ('candidate_added', 'Candidate added'),
                ('candidate_linked', 'Candidate linked to Classroom group'),
                ('candidate_activated', 'Candidate activated for scoring'),
                ('candidate_deactivated', 'Candidate scoring ended'),
                ('criterion_added', 'Criterion added'), ('panel_added', 'Scoring panel added'),
                ('panel_updated', 'Scoring panel updated'), ('results_compiled', 'Results compiled'),
                ('results_exported', 'Results exported'), ('result_adjusted', 'Result adjusted'),
                ('invitation_issued', 'Invitation issued'), ('invitation_resent', 'Invitation resent'),
                ('invitation_revoked', 'Invitation revoked'),
                ('device_bound', 'Examiner invitation bound to a browser'),
                ('conflict_declared', 'Conflict declared'),
                ('score_abstained', 'Criterion score abstained'), ('score_submitted', 'Score submitted'),
                ('review_submitted', 'Written review submitted'), ('vote_submitted', 'Private vote submitted'),
                ('decision_recorded', 'Chair outcome recorded'),
                ('comment_released', 'Examiner comment approved for entrant release'),
                ('results_signed_off', 'Results signed off'), ('results_released', 'Results released'),
                ('result_link_issued', 'Entrant result link issued'),
                ('result_link_revoked', 'Entrant result link revoked'),
                ('comment_redacted', 'Examiner comment redacted'),
            ]),
        ),
    ]
