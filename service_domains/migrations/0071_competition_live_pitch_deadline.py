from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('service_domains', '0070_remove_coassessmentscorevalue_coassessment_one_score_per_criterion_card_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='competitionlivestate',
            name='pitch_timer_duration_seconds',
            field=models.PositiveIntegerField(default=180),
        ),
        migrations.AddField(
            model_name='competitionlivestate',
            name='pitch_timer_ends_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
