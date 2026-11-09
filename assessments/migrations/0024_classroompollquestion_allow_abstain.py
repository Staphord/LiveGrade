from django.db import migrations, models


def copy_poll_abstain_setting(apps, schema_editor):
    Poll = apps.get_model('assessments', 'ClassroomPoll')
    Question = apps.get_model('assessments', 'ClassroomPollQuestion')
    alias = schema_editor.connection.alias
    for poll in Poll.objects.using(alias).only('pk', 'allow_abstain').iterator(chunk_size=500):
        Question.objects.using(alias).filter(poll_id=poll.pk).update(allow_abstain=poll.allow_abstain)


class Migration(migrations.Migration):
    dependencies = [('assessments', '0023_classroompoll_result_counts')]

    operations = [
        migrations.AddField(
            model_name='classroompollquestion',
            name='allow_abstain',
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(copy_poll_abstain_setting, migrations.RunPython.noop),
    ]
