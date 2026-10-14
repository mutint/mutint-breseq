"""The reads a sample was analysed from, kept so a curation check can analyse them again.

A run launched through this plugin used to delete its reads at every ending -- what a
person reads afterwards is the log, and gigabytes per run nobody opened was the cost of
keeping them. A curation check changed the calculus: re-running breseq on the same reads is
the whole feature, and asking somebody to find and re-drop twenty files per sample to check
their curation is asking them not to. So a successful run moves its untrimmed reads under
the sample, and this is the storage kind that counts and clears them.

**Under the sample, not the run.** `run_delete` and the row's `post_delete` receiver rmtree
the run's directory, which is exactly when a later check still wants the reads; under
`store.sample_reads_dir` they are reaped by core with the BAM and the report when the sample
goes, and one copy serves every later check. The fact that they are there is recorded on
the sample's row -- `supplemental_data` under this component's key -- which is what the kind
measures from, per the registry's rule: rows, never a directory listing.
"""

import os
import shutil

from django.utils import timezone

from mutint_common import store
from mutint_common.storage_registry import directory_bytes, request_remeasure
from mutint_experiment import paths

from mutint_breseq.models import COMPONENT

KIND = "breseq_reads"
#: The record kind under `supplemental_data[COMPONENT]`.
RECORD = "reads"


def reads_record(sample):
    """`{kept, files, paired, kept_at}`, or `{}` for a sample nothing was kept for."""
    return sample.record(RECORD, component=COMPONENT)


def kept_reads(sample):
    """Absolute paths of the sample's kept read files, or `[]`.

    Both the record and the files: a record whose files have gone -- a worker killed between
    the move and the write, a directory removed by hand -- answers nothing rather than paths
    a check would fail on.
    """
    record = reads_record(sample)
    if not record.get("kept"):
        return []
    directory = store.sample_reads_dir(sample.pk)
    paths_found = [os.path.join(directory, name) for name in record.get("files") or []]
    return [path for path in paths_found if os.path.isfile(path)]


def record_kept(sample, names, paired):
    sample.set_record(COMPONENT, RECORD, {
        "kept": True, "files": sorted(names), "paired": bool(paired),
        "kept_at": timezone.now().isoformat()})
    request_remeasure(sample.experiment.id, "reads kept")


def _samples(experiment):
    from mutint_sample.models import Sample

    return Sample.objects.filter(**{paths.to_experiment(): experiment}).only(
        "id", "supplemental_data")


def measure_reads(experiment):
    return sum(directory_bytes(store.sample_reads_dir(sample.id))
               for sample in _samples(experiment) if reads_record(sample).get("kept"))


def clear_reads(experiment):
    for sample in _samples(experiment):
        record = reads_record(sample)
        if not record.get("kept"):
            continue
        shutil.rmtree(store.sample_reads_dir(sample.id), ignore_errors=True)
        # `files` stays, so the page can still say what was there and where it would come
        # from again.
        sample.set_record(COMPONENT, RECORD, dict(record, kept=False))
