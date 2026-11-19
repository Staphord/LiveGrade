from django.db import migrations


def forwards(apps, schema_editor):
    database = schema_editor.connection.alias
    for model_name in ('AssessmentSession', 'SessionTemplate', 'PrivacyNotice'):
        Model = apps.get_model('assessments', model_name)
        Model.objects.using(database).filter(service='classroom').update(service='assessment')


def backwards(apps, schema_editor):
    database = schema_editor.connection.alias
    for model_name in ('AssessmentSession', 'SessionTemplate', 'PrivacyNotice'):
        Model = apps.get_model('assessments', model_name)
        Model.objects.using(database).filter(service='assessment').update(service='classroom')


class Migration(migrations.Migration):

    dependencies = [
        ('assessments', '0026_assessmentsession_avoid_reciprocal_pairs_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
