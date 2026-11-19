from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('service_domains', '0071_competition_live_pitch_deadline'),
    ]

    operations = [
        migrations.DeleteModel(name='CoAssessmentScoreValue'),
        migrations.DeleteModel(name='CoAssessmentScorecard'),
        migrations.DeleteModel(name='CoAssessmentModeration'),
        migrations.DeleteModel(name='CoAssessmentTurn'),
        migrations.DeleteModel(name='CoAssessmentLevelDescriptor'),
        migrations.DeleteModel(name='CoAssessmentCriterion'),
        migrations.DeleteModel(name='CoAssessmentParticipant'),
        migrations.DeleteModel(name='CoAssessmentGroup'),
        migrations.DeleteModel(name='CoAssessmentExaminer'),
        migrations.DeleteModel(name='CoAssessmentSession'),
    ]
