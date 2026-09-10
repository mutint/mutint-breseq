from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mutint_breseq', '0002_breseqrun_population_sample'),
    ]

    operations = [
        migrations.AddField(
            model_name='breseqrun',
            name='coverage_limit',
            field=models.FloatField(blank=True, null=True),
        ),
    ]
