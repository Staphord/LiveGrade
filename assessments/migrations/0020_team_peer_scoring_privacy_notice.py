from django.db import migrations


NOTICE_TITLE = 'How your Team demo information is used'
NOTICE_BODY = (
    'LiveGrade checks the student ID or name you enter against this demo’s roster. '
    'When you join, it records your participation and which presentations you score '
    'to prevent duplicate and self-grading. Your scores are used to calculate the '
    'demo’s peer results. The organizer can see the roster, who joined, and aggregate '
    'results, but cannot see which learner submitted a particular peer score. The '
    'trainer may separately record named competency assessments and evidence in the '
    'Team and Training workspace; those are separate from anonymous peer scores. '
    'Ask the session owner or trainer about this demo’s roster or data handling.'
)


def create_team_notice(apps, schema_editor):
    PrivacyNotice = apps.get_model('assessments', 'PrivacyNotice')
    notices = PrivacyNotice.objects.using(schema_editor.connection.alias)
    if notices.filter(service='team', is_active=True).exists():
        return

    version = '1'
    suffix = 2
    while notices.filter(service='team', version=version).exists():
        version = f'1.{suffix}'
        suffix += 1

    notices.create(service='team', version=version, title=NOTICE_TITLE,
        body=NOTICE_BODY, is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ('assessments', '0019_alter_assessmentsession_default_presentation_seconds_and_more'),
    ]

    operations = [
        migrations.RunPython(create_team_notice, migrations.RunPython.noop),
    ]
