from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mutint_breseq', '0003_breseqrun_coverage_limit'),
    ]

    operations = [
        migrations.AddField(
            model_name='breseqrun',
            name='notes',
            field=models.JSONField(default=list),
        ),
    ]
