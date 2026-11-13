from django.db import migrations, models
import django.db.models.deletion


def create_first_question(apps, schema_editor):
    Poll = apps.get_model('assessments', 'ClassroomPoll')
    Question = apps.get_model('assessments', 'ClassroomPollQuestion')
    Option = apps.get_model('assessments', 'ClassroomPollOption')
    Response = apps.get_model('assessments', 'ClassroomPollResponse')
    for poll in Poll.objects.all().iterator():
        question = Question.objects.create(
            poll_id=poll.pk, prompt=poll.question,
            allow_multiple=poll.allow_multiple, order=0)
        Option.objects.filter(poll_id=poll.pk).update(question_id=question.pk)
        for response in Response.objects.filter(voter__poll_id=poll.pk).iterator():
            choices = list(response.options.values_list('pk', flat=True))
            response.answers = {str(question.pk): None if response.abstained else choices}
            response.save(update_fields=['answers'])
        if poll.confirmed_option_id:
            question.confirmed_option_id = poll.confirmed_option_id
            question.decision_note = poll.decision_note
            question.save(update_fields=['confirmed_option', 'decision_note'])


class Migration(migrations.Migration):
    dependencies = [('assessments', '0017_live_rankings_privacy_notice')]

    operations = [
        migrations.CreateModel(
            name='ClassroomPollQuestion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('prompt', models.CharField(max_length=250)),
                ('allow_multiple', models.BooleanField(default=False)),
                ('order', models.PositiveIntegerField(default=0)),
                ('decision_note', models.TextField(blank=True, max_length=2000)),
                ('confirmed_option', models.ForeignKey(blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='confirmed_for_questions', to='assessments.classroompolloption')),
                ('poll', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name='questions', to='assessments.classroompoll')),
            ],
            options={'ordering': ['order', 'pk']},
        ),
        migrations.AddField(
            model_name='classroompolloption', name='question',
            field=models.ForeignKey(blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='options', to='assessments.classroompollquestion'),
        ),
        migrations.AddField(
            model_name='classroompollresponse', name='answers',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(create_first_question, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name='classroompolloption', name='classroom_poll_option_label_once'),
        migrations.AddConstraint(
            model_name='classroompolloption',
            constraint=models.UniqueConstraint(fields=('question', 'label'),
                condition=models.Q(('question__isnull', False)),
                name='classroom_poll_question_option_once'),
        ),
        migrations.AddConstraint(
            model_name='classroompolloption',
            constraint=models.UniqueConstraint(fields=('poll', 'label'),
                condition=models.Q(('question__isnull', True)),
                name='classroom_poll_legacy_option_once'),
        ),
    ]
