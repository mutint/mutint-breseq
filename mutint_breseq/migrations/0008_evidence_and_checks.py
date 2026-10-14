"""Unassigned evidence per sample, and curation checks as a kind of run.

`Evidence` keeps what breseq's index page lists as unassigned evidence -- and, after a
curation check, the residual mutations the applied curation did not account for -- so a
curator can see and resolve it. `BreseqRun.kind` tells a check from an analysis, and
`check_summary` is what the check was handed and found.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mutint_breseq', '0007_breseqrun_sample_details'),
        ('mutint_sample', '__first__'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='breseqrun',
            name='kind',
            field=models.CharField(
                choices=[('analysis', 'Analysis'), ('check', 'Curation check')],
                default='analysis', max_length=8),
        ),
        migrations.AddField(
            model_name='breseqrun',
            name='check_summary',
            field=models.JSONField(default=dict),
        ),
        migrations.AlterField(
            model_name='breseqrun',
            name='status',
            field=models.CharField(
                choices=[('queued', 'Queued'), ('running', 'Running'),
                         ('imported', 'Imported'), ('checked', 'Checked'),
                         ('failed', 'Failed'), ('cancelled', 'Cancelled')],
                default='queued', max_length=16),
        ),
        migrations.CreateModel(
            name='Evidence',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('evidence_type', models.CharField(max_length=3)),
                ('seq_id', models.CharField(max_length=200)),
                ('position', models.IntegerField()),
                ('end', models.IntegerField(blank=True, null=True)),
                ('seq_id_2', models.CharField(blank=True, default='', max_length=200)),
                ('position_2', models.IntegerField(blank=True, null=True)),
                ('source_id', models.IntegerField(blank=True, null=True)),
                ('origin', models.CharField(
                    choices=[('import', 'Import'), ('check', 'Curation check')],
                    default='import', max_length=8)),
                ('record', models.JSONField(default=dict)),
                ('dismissed_at', models.DateTimeField(blank=True, null=True)),
                ('dismiss_note', models.TextField(blank=True, default='')),
                ('dismissed_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+', to=settings.AUTH_USER_MODEL)),
                ('resolved_mutation', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+', to='mutint_sample.mutation')),
                ('sample', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='breseq_evidence', to='mutint_sample.sample')),
            ],
            options={
                'verbose_name_plural': 'evidence',
                'ordering': ['seq_id', 'position', 'pk'],
            },
        ),
    ]
