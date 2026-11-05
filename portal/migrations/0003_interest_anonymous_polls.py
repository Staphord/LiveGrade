from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0002_serviceinterest')]
    operations = [
        migrations.AlterField(
            model_name='serviceinterest', name='service',
            field=models.CharField(choices=[('classroom', 'classroom'), ('panel', 'panel'),
                ('showcase', 'showcase'), ('team', 'team'), ('competition', 'competition'),
                ('polls', 'polls')], max_length=20)),
    ]
