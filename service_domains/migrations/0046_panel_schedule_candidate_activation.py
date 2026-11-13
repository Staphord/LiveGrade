# Generated for chair-controlled, candidate-by-candidate Panel scoring.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('service_domains', '0045_competition_public_leaderboard_enabled_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='panelhearing',
            name='hearing_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='panelhearing',
            name='venue',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='panelhearing',
            name='active_candidate',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='active_for_hearings', to='service_domains.panelcandidate'),
        ),
        migrations.AddField(
            model_name='panelcandidate',
            name='scheduled_time',
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='panelauditevent',
            name='action',
            field=models.CharField(max_length=24, choices=[
                ('hearing_created', 'Hearing created'),
                ('hearing_opened', 'Hearing opened'),
                ('candidate_added', 'Candidate added'),
                ('candidate_activated', 'Candidate activated for scoring'),
                ('candidate_deactivated', 'Candidate scoring ended'),
                ('criterion_added', 'Criterion added'),
                ('panel_added', 'Scoring panel added'),
                ('panel_updated', 'Scoring panel updated'),
                ('results_compiled', 'Results compiled'),
                ('results_exported', 'Results exported'),
                ('result_adjusted', 'Result adjusted'),
                ('invitation_issued', 'Invitation issued'),
                ('invitation_resent', 'Invitation resent'),
                ('invitation_revoked', 'Invitation revoked'),
                ('device_bound', 'Examiner invitation bound to a browser'),
                ('conflict_declared', 'Conflict declared'),
                ('score_abstained', 'Criterion score abstained'),
                ('score_submitted', 'Score submitted'),
                ('comment_released', 'Examiner comment approved for entrant release'),
                ('results_signed_off', 'Results signed off'),
                ('results_released', 'Results released'),
                ('result_link_issued', 'Entrant result link issued'),
                ('result_link_revoked', 'Entrant result link revoked'),
                ('comment_redacted', 'Examiner comment redacted'),
            ]),
        ),
    ]
