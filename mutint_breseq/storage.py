"""How much disk this plugin's run directories take, per experiment.

Registered with `mutint_common.storage_registry` from `apps.py` as a kind that is **measured
and not clearable**. A finished run's directory is nearly empty -- `cleanup_after_import`
removes the reads, the trimmed reads and breseq's output once the importer has copied what
matters under the sample -- so what this counts is mostly failed runs, which keep everything
on purpose because the reads are exactly what somebody diagnosing one needs. Deleting the
run on the Run breseq page is how those bytes are freed, and that is the one path, so a
second one here would be a Clear button that does what Delete already does with less to say
about it.

Walks the directories the experiment's `BreseqRun` rows point at, never `components/
mutint_breseq/` itself: a directory no row owns is the dashboard's unattributed line.
"""

from mutint_common.storage_registry import directory_bytes

KIND = 'breseq_runs'


def measure_runs(experiment):
    from mutint_breseq.models import BreseqRun

    return sum(directory_bytes(run.directory())
               for run in BreseqRun.objects.filter(experiment=experiment).only('id'))
