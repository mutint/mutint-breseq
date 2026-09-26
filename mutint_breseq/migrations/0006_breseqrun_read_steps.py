"""`trim_reads` becomes one entry of `read_steps`.

Trimming is a step registered through `mutint_common.read_step_registry` now, beside any other
component's, so a run records the names of the steps it was launched with. A run that trimmed
records `["trim"]`; one that did not records nothing -- which is also the truth about what
every other step did to it, since none existed.
"""

from django.db import migrations, models


def trim_reads_to_steps(apps, schema_editor):
    BreseqRun = apps.get_model("mutint_breseq", "BreseqRun")
    BreseqRun.objects.filter(trim_reads=True).update(read_steps=["trim"])


def steps_to_trim_reads(apps, schema_editor):
    BreseqRun = apps.get_model("mutint_breseq", "BreseqRun")
    for run in BreseqRun.objects.all():
        run.trim_reads = "trim" in (run.read_steps or [])
        run.save(update_fields=["trim_reads"])


class Migration(migrations.Migration):

    dependencies = [
        ('mutint_breseq', '0005_breseqrun_accessions'),
    ]

    operations = [
        migrations.AddField(
            model_name='breseqrun',
            name='read_steps',
            field=models.JSONField(default=list),
        ),
        migrations.RunPython(trim_reads_to_steps, steps_to_trim_reads),
        migrations.RemoveField(
            model_name='breseqrun',
            name='trim_reads',
        ),
    ]
