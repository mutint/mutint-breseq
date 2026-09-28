"""A run carries what its `metadata.csv` row said about the sample beyond its coordinate.

The treatment, description and flags a row set used to stop at the launcher: the name and
the sample type reached the sample, the rest did not, because the sample exists hours after
the CSV was read. The row holds them now and the task applies them after the import.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mutint_breseq', '0006_breseqrun_read_steps'),
    ]

    operations = [
        migrations.AddField(
            model_name='breseqrun',
            name='sample_details',
            field=models.JSONField(default=dict),
        ),
    ]
