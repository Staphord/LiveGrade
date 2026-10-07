import decimal

import django.core.validators
from django.db import migrations, models
from django.db.models import OuterRef, Subquery


def snapshot_scale(apps, schema_editor):
    """Each existing score was given out of its category's max_points: keep that as
    the score's own scale so every past result comes out the same."""
    EvaluationScore = apps.get_model('assessments', 'EvaluationScore')
    RubricCategory = apps.get_model('assessments', 'RubricCategory')
    alias = schema_editor.connection.alias
    EvaluationScore.objects.using(alias).update(max_value=Subquery(
        RubricCategory.objects.using(alias).filter(pk=OuterRef('rubric_category_id')).values('max_points')[:1]))


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('assessments', '0003_group_topic_location_session_change'),
    ]

    operations = [
        migrations.AddField(
            model_name='assessmentsession',
            name='ungraded_penalty',
            field=models.DecimalField(
                decimal_places=2, default=decimal.Decimal('1.00'), max_digits=5,
                validators=[django.core.validators.MinValueValidator(decimal.Decimal('0')),
                            django.core.validators.MaxValueValidator(decimal.Decimal('100'))],
                help_text="Points taken off a student's final score for every other group they did not grade. 0 turns the penalty off."),
        ),
        migrations.AddField(
            model_name='evaluationscore',
            name='max_value',
            field=models.DecimalField(decimal_places=2, max_digits=6, null=True),
        ),
        migrations.RunPython(snapshot_scale, noop),
        migrations.AlterField(
            model_name='evaluationscore',
            name='max_value',
            field=models.DecimalField(
                decimal_places=2, max_digits=6,
                help_text="The category's weight when this score was given: ``value`` is out of this. Results use value / max_value against the category's current weight, so a weight edited later rescales past scores instead of invalidating them."),
        ),
        migrations.RemoveField(model_name='rubriccategory', name='max_points'),
        migrations.RemoveField(model_name='assessmentsession', name='group_weight_percent'),
        migrations.RemoveField(model_name='assessmentsession', name='individual_weight_percent'),
        migrations.AlterField(
            model_name='rubriccategory',
            name='weight',
            field=models.DecimalField(
                decimal_places=2, default=decimal.Decimal('0.00'), max_digits=5,
                help_text="This category's real share of the 100-point final score. All weights in a session, group and individual together, add up to at most 100 (exactly 100 to go live)."),
        ),
    ]
