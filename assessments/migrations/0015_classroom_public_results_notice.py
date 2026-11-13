from django.db import migrations
from django.db import models


NOTICE_BODY = (
    'LiveGrade checks the name or student ID you enter against this session’s roster. '
    'It records which presentations you score to prevent duplicate and self-grading, '
    'and uses your submitted scores to calculate session results. The session owner '
    'can see roster and participation information and the resulting score aggregates, '
    'but cannot see which learner submitted each score. After a session closes, '
    'the owner may choose to release presentation-group aggregate scores on the '
    'public room screen; individual learner marks and evaluator identities are not '
    'shown there. Ask the session owner about this session’s roster or data handling.'
)


def publish_notice_v2(apps, schema_editor):
    PrivacyNotice = apps.get_model('assessments', 'PrivacyNotice')
    notices = PrivacyNotice.objects.using(schema_editor.connection.alias)
    notices.filter(service='classroom', is_active=True).update(is_active=False)
    notices.update_or_create(
        service='classroom', version='2',
        defaults={
            'title': 'How your information is used',
            'body': NOTICE_BODY,
            'is_active': True,
        },
    )


def restore_notice_v1(apps, schema_editor):
    PrivacyNotice = apps.get_model('assessments', 'PrivacyNotice')
    notices = PrivacyNotice.objects.using(schema_editor.connection.alias)
    notices.filter(service='classroom', version='2').update(is_active=False)
    notices.filter(service='classroom', version='1').update(is_active=True)


class Migration(migrations.Migration):
    dependencies = [
        ('assessments', '0014_assessmentsession_public_results_released_at_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='privacynotice',
            name='version',
            field=models.CharField(default='2', max_length=40),
        ),
        migrations.AlterField(
            model_name='privacynotice',
            name='body',
            field=models.TextField(default=NOTICE_BODY),
        ),
        migrations.RunPython(publish_notice_v2, restore_notice_v1),
    ]
