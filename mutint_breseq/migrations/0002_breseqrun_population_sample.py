from django.db import migrations, models


class Migration(migrations.Migration):

    # No core dependency: this adds a plain column to this plugin's own table and touches
    # nothing of mutint-core's, so there is no `__first__` sentinel to carry forward here --
    # see 0001_initial for why that one has them.
    dependencies = [
        ('mutint_breseq', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='breseqrun',
            name='population_sample',
            field=models.BooleanField(default=False),
        ),
    ]
