"""The run directories are counted as stored data, and never offered for clearing."""

import os

from mutint_common import store
from mutint_common.rebuild_registry import is_stale, run_rebuilds
from mutint_common.storage_registry import (
    STORAGE_REBUILD, get_storage_kind, is_clearable, usage_for,
)

from mutint_breseq.models import STATUS_FAILED, STATUS_IMPORTED, BreseqRun
from mutint_breseq.storage import KIND, measure_runs
from mutint_breseq.tests.test_run import RunTestCase


class StorageKindTestCase(RunTestCase):

    def test_the_kind_is_registered_and_measured_only(self):
        self.assertEqual("mutint_breseq", get_storage_kind(KIND)["app"])
        self.assertFalse(is_clearable(KIND))

    def test_a_failed_run_s_directory_is_counted(self):
        run = BreseqRun.objects.create(experiment=self.experiment, sample_name="kept",
                                       created_by=self.owner, status=STATUS_FAILED)
        reads = store.ensure_dir(run.reads_dir())
        with open(os.path.join(reads, "kept_R1.fastq"), "wb") as handle:
            handle.write(b"@r\nACGT\n+\nIIII\n" * 100)
        self.assertEqual(1500, measure_runs(self.experiment))

        run_rebuilds(self.experiment.id, only=[STORAGE_REBUILD])
        mine = {u["key"]: u for u in usage_for(self.experiment)}
        self.assertEqual(1500, mine[KIND]["bytes"])
        self.assertFalse(mine[KIND]["clearable"])

    def test_deleting_the_run_frees_it_and_says_so(self):
        run = BreseqRun.objects.create(experiment=self.experiment, sample_name="kept",
                                       created_by=self.owner, status=STATUS_FAILED)
        reads = store.ensure_dir(run.reads_dir())
        with open(os.path.join(reads, "x"), "wb") as handle:
            handle.write(b"x" * 10)
        run_rebuilds(self.experiment.id, only=[STORAGE_REBUILD])
        self.assertFalse(is_stale(STORAGE_REBUILD, self.experiment.id))

        response = self.client.post("/breseq/run/%d/delete" % run.pk)
        self.assertEqual(200, response.status_code, response.content)
        self.assertEqual(0, measure_runs(self.experiment))
        self.assertTrue(is_stale(STORAGE_REBUILD, self.experiment.id))

    def test_a_successful_run_remeasures_after_its_cleanup(self):
        self._launch()
        run = BreseqRun.objects.get()
        self.assertEqual(STATUS_IMPORTED, run.status)
        # The import's rebuild ran before the cleanup; the task marked it again afterwards,
        # so the stored size is not the pre-cleanup one.
        self.assertTrue(is_stale(STORAGE_REBUILD, self.experiment.id))
        run_rebuilds(self.experiment.id, only=[STORAGE_REBUILD])
        mine = {u["key"]: u["bytes"] for u in usage_for(self.experiment)}
        self.assertEqual(measure_runs(self.experiment), mine[KIND])
        self.assertLess(mine[KIND], 200)
