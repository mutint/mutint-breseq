from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mutint_breseq', '0004_breseqrun_notes'),
    ]

    operations = [
        migrations.AddField(
            model_name='breseqrun',
            name='accessions',
            field=models.JSONField(default=list),
        ),
    ]
