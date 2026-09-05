from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mutint_breseq', '0003_remove_breseqrun_report_stored'),
    ]

    operations = [
        # Two steps on purpose. Rows that exist already were never trimmed, so the column
        # arrives as False for them and the page says so rather than claiming a trim that did
        # not happen; the model's own default is then True, so a new run trims unless somebody
        # unticks the box. Django keeps no default in the database, so the second step changes
        # state only.
        migrations.AddField(
            model_name='breseqrun',
            name='trim_reads',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='breseqrun',
            name='trim_reads',
            field=models.BooleanField(default=True),
        ),
    ]
