"""Curation checks: breseq re-run on a sample's reads with its curation applied.

End to end against the fake breseq, which under `--apply-check` shifts every coordinate it
writes and carries the unshifted value in `original_*` -- so a position that lands right is
one that was read from the original fields rather than the mutated frame.
"""

import json
import os
import shutil
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings

from mutint_common import store
from mutint_import import breseq_folder
from mutint_import.gd_import import export_gd_text
from mutint_import.tests import breseq_fixture
from mutint_sample.models import MutationCall, Sample

from mutint_breseq import runner, storage
from mutint_breseq.models import (
    KIND_CHECK, STATUS_CHECKED, STATUS_FAILED, STATUS_QUEUED, BreseqRun, Evidence,
)
from mutint_breseq.tests import fastq_fixture
from mutint_breseq.tests.test_evidence import GD_WITH_EVIDENCE
from mutint_breseq.tests.test_run import RunTestCase

RESIDUAL = "SNP\t1\t.\ttest_ref\t140\tT\tfrequency=1"


class CheckTestCase(RunTestCase):

    def setUp(self):
        super().setUp()
        # The template the fake copies in carries evidence, so the analysis leaves rows.
        shutil.rmtree(self.template, ignore_errors=True)
        self.template = breseq_fixture.write_sample(self.template_root, "template",
                                                    gd_text=GD_WITH_EVIDENCE)
        os.environ["FAKE_BRESEQ_TEMPLATE"] = self.template

    def _analysed_sample(self, **kwargs):
        response = self._launch(**kwargs)
        self.assertEqual(200, response.status_code, response.content)
        sample = Sample.objects.get()
        return sample

    def _check(self, sample, body=None, upload=None):
        body = dict(body or {})
        if upload is not None:
            body["upload_id"] = str(upload.id)
        return self.client.post(
            "/breseq/check/launch?experiment_id=%s&sample_id=%s" % (self.experiment.id, sample.id),
            data=json.dumps(body), content_type="application/json")

    def _checks(self):
        return [call for call in self._recorded_breseq() if call.get("apply_check")]

    # --- what a check hands breseq ------------------------------------------------------

    def test_a_check_hands_breseq_the_exported_curated_gd(self):
        sample = self._analysed_sample()
        response = self._check(sample)
        self.assertEqual(200, response.status_code, response.content)
        # The launch's preflight, the worker's dry run, then the run itself.
        checks = self._checks()
        self.assertEqual([True, True, False], [c["dry_run"] for c in checks])
        with open(checks[2]["apply_check"]) as handle:
            self.assertEqual(export_gd_text(sample), handle.read())
        self.assertEqual(checks[2]["argv"].index("--apply-check") + 2,
                         checks[2]["argv"].index("-o"))

    def test_a_check_imports_no_sample_and_moves_no_call(self):
        sample = self._analysed_sample()
        before = set(MutationCall.objects.filter(sample=sample).values_list("mutation_id", flat=True))
        self._check(sample)
        self.assertEqual(1, Sample.objects.count())
        self.assertEqual(before, set(MutationCall.objects.filter(sample=sample)
                                     .values_list("mutation_id", flat=True)))
        run = BreseqRun.objects.get(kind=KIND_CHECK)
        self.assertEqual((STATUS_CHECKED, sample.pk), (run.status, run.sample_id))

    def test_a_check_replaces_the_evidence_from_the_original_fields(self):
        sample = self._analysed_sample()
        self.assertEqual({"import"}, set(Evidence.objects.filter(sample=sample)
                                         .values_list("origin", flat=True)))
        os.environ["FAKE_BRESEQ_CHECK_RESIDUAL"] = RESIDUAL
        self.addCleanup(os.environ.pop, "FAKE_BRESEQ_CHECK_RESIDUAL", None)

        self._check(sample)

        rows = {row.evidence_type: row for row in Evidence.objects.filter(sample=sample)}
        self.assertEqual({"SNP", "JC", "MC"}, set(rows))
        self.assertTrue(all(row.origin == "check" for row in rows.values()))
        # The fake shifted these by the applied DEL (5 bp at 200 does not reach 130/140,
        # but the SNP at 100 and the DEL at 200 are both below none of them -- so it is the
        # original_* fields, not the shift, that the assertion is about).
        self.assertEqual(140, rows["SNP"].position)
        self.assertEqual((130, 150), (rows["MC"].position, rows["MC"].end))
        self.assertEqual("140", str(rows["SNP"].record["original_position"]))
        run = BreseqRun.objects.get(kind=KIND_CHECK)
        self.assertEqual((1, 2), (run.check_summary["residual_mutations"],
                                  run.check_summary["evidence"]))

    def test_positions_above_an_applied_deletion_still_come_out_original(self):
        # A residual at 300, past the fixture's DEL at 200..204: the fake writes it at 295
        # with original_position=300, and the row must say 300.
        sample = self._analysed_sample()
        os.environ["FAKE_BRESEQ_CHECK_RESIDUAL"] = "SNP\t1\t.\ttest_ref\t300\tT\tfrequency=1"
        self.addCleanup(os.environ.pop, "FAKE_BRESEQ_CHECK_RESIDUAL", None)
        self._check(sample)
        row = Evidence.objects.get(sample=sample, evidence_type="SNP")
        self.assertEqual((300, 295), (row.position, int(row.record["position"])))

    def test_a_check_stores_the_report_as_the_samples_second_report(self):
        sample = self._analysed_sample()
        self._check(sample)
        sample.refresh_from_db()
        self.assertTrue(sample.check_report_stored)
        self.assertTrue(sample.report_stored, "the import's report is untouched")
        response = self.client.get("/mutations/report/%d/check/" % sample.pk)
        self.assertEqual(200, response.status_code)
        self.assertIn("/mutations/report/%d/check/files/" % sample.pk, response.content.decode())

    # --- where the reads come from ------------------------------------------------------

    def test_a_check_reuses_the_kept_reads_by_hard_link(self):
        sample = self._analysed_sample()
        kept = storage.kept_reads(sample)
        self.assertEqual(2, len(kept))
        self._check(sample)
        run = BreseqRun.objects.get(kind=KIND_CHECK)
        self.assertEqual("kept", run.check_summary["reads_source"])
        self.assertEqual(sorted(os.path.basename(p) for p in kept), run.read_files)
        # The run's directory is emptied afterwards and the sample's copy is still there.
        self.assertFalse(os.path.exists(run.reads_dir()))
        self.assertEqual(2, len(storage.kept_reads(sample)))
        reads = [a for a in self._checks()[2]["argv"] if a.endswith(".fastq")]
        self.assertTrue(all(a.startswith(run.directory()) for a in reads), reads)

    def test_a_check_refetches_by_accession_when_nothing_is_kept(self):
        sample = self._analysed_sample()
        storage.clear_reads(self.experiment)
        sample.refresh_from_db()
        from mutint_sample import inputs
        inputs.record_inputs(sample, [inputs.Input(inputs.KIND_SRA, "SRR000001", 1, None)])

        from mutint_import import sra
        plan = sra.Plan("SRR000001", "run", [sra.Run(
            "SRR000001",
            [{"name": "SRR000001_1.fastq", "url": "u1", "md5": "", "bytes": 4},
             {"name": "SRR000001_2.fastq", "url": "u2", "md5": "", "bytes": 4}],
            sample_accession="SAMN1", alias="s", title="t", layout="PAIRED")])

        def fake_download(directory, plans, report=None, is_cancelled=None):
            os.makedirs(directory, exist_ok=True)
            for name in ("SRR000001_1.fastq", "SRR000001_2.fastq"):
                fastq_fixture.write(os.path.join(directory, name), count=4)
            return {"SRR000001_1.fastq": "SRR000001", "SRR000001_2.fastq": "SRR000001"}

        with mock.patch("mutint_breseq.views.sra_fetch.resolve", return_value=[plan]), \
                mock.patch("mutint_breseq.tasks.sra_fetch.download", side_effect=fake_download):
            response = self._check(sample)
        self.assertEqual(200, response.status_code, response.content)
        run = BreseqRun.objects.get(kind=KIND_CHECK)
        self.assertEqual(("sra", STATUS_CHECKED), (run.check_summary["reads_source"], run.status))
        # Fetched reads are kept for the next check.
        sample.refresh_from_db()
        self.assertEqual(2, len(storage.kept_reads(sample)))

    def test_a_check_takes_a_fresh_drop_and_keeps_it(self):
        sample = self._analysed_sample()
        storage.clear_reads(self.experiment)
        session = self._stage(("again_R1.fastq", "again_R2.fastq"))
        response = self._check(sample, {"reads_source": "drop"}, upload=session)
        self.assertEqual(200, response.status_code, response.content)
        sample.refresh_from_db()
        self.assertEqual(["again_R1.fastq", "again_R2.fastq"],
                         sorted(os.path.basename(p) for p in storage.kept_reads(sample)))

    def test_a_check_with_nothing_to_run_on_is_refused(self):
        sample = self._analysed_sample()
        storage.clear_reads(self.experiment)
        response = self._check(sample)
        self.assertEqual(400, response.status_code)
        self.assertIn("Drop its read files", response.json()["error"])
        self.assertFalse(BreseqRun.objects.filter(kind=KIND_CHECK).exists())

    # --- refusals -------------------------------------------------------------------

    def test_an_old_breseq_is_refused_by_name(self):
        sample = self._analysed_sample()
        os.environ["FAKE_BRESEQ_NO_APPLY_CHECK"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_BRESEQ_NO_APPLY_CHECK", None)
        response = self._check(sample)
        self.assertEqual(400, response.status_code)
        self.assertIn("g25617bb8", response.json()["error"])
        self.assertFalse(BreseqRun.objects.filter(kind=KIND_CHECK).exists())

    def test_apply_check_typed_into_the_box_is_refused(self):
        sample = self._analysed_sample()
        response = self._check(sample, {"arguments": "--apply-check x.gd"})
        self.assertEqual(400, response.status_code)
        self.assertEqual("arguments", response.json()["field"])

    def test_a_sample_of_another_experiment_is_a_404(self):
        sample = self._analysed_sample()
        from mutint_experiment.views import _create_experiment
        other = _create_experiment(self.project, "other", self.owner)
        response = self.client.post(
            "/breseq/check/launch?experiment_id=%s&sample_id=%s" % (other.id, sample.id),
            data="{}", content_type="application/json")
        self.assertEqual(404, response.status_code)

    def test_a_locked_experiment_refuses_a_check(self):
        from django.utils import timezone
        sample = self._analysed_sample()
        self.experiment.locked_at = timezone.now()
        self.experiment.locked_by = self.owner
        self.experiment.save()
        self.assertEqual(403, self._check(sample).status_code)

    def test_a_population_sample_is_checked_with_p(self):
        sample = self._analysed_sample()
        self._check(sample, {"population_sample": True})
        self.assertIn("-p", self._checks()[2]["argv"])

    def test_a_second_check_supersedes_the_first_and_not_the_analysis(self):
        sample = self._analysed_sample()
        first = BreseqRun.objects.create(
            experiment=self.experiment, kind=KIND_CHECK, sample=sample,
            sample_name=sample.source_name, status=STATUS_QUEUED, task_result_id="t1")
        analysis = BreseqRun.objects.filter(kind="analysis").get()
        with mock.patch("mutint_breseq.views.jobs_api.request_cancel_for",
                        return_value=1) as cancel:
            response = self._check(sample)
        self.assertEqual(1, response.json()["superseded"])
        self.assertEqual(["t1"], cancel.call_args[0][0])
        self.assertNotIn(analysis.task_result_id, cancel.call_args[0][0])

    def test_a_failed_check_leaves_the_evidence_and_the_kept_reads_alone(self):
        sample = self._analysed_sample()
        before = list(Evidence.objects.filter(sample=sample).values_list("pk", flat=True))
        os.environ["FAKE_BRESEQ_FAIL"] = "3"
        self.addCleanup(os.environ.pop, "FAKE_BRESEQ_FAIL", None)
        # The launch succeeds; under the immediate backend the task has already failed.
        self.assertEqual(200, self._check(sample).status_code)
        run = BreseqRun.objects.get(kind=KIND_CHECK)
        self.assertEqual(STATUS_FAILED, run.status)
        self.assertEqual(before, list(Evidence.objects.filter(sample=sample)
                                      .values_list("pk", flat=True)))
        self.assertEqual(2, len(storage.kept_reads(sample)))
        self.assertTrue(os.path.isfile(run.curated_gd_path()))

    def test_a_check_whose_sample_was_deleted_fails_cleanly(self):
        from mutint_breseq import tasks
        sample = self._analysed_sample()
        run = BreseqRun.objects.create(
            experiment=self.experiment, kind=KIND_CHECK, sample=None,
            sample_name="gone", status=STATUS_QUEUED)
        with self.assertRaises(RuntimeError):
            tasks.run_breseq.call(None, run.pk)
        run.refresh_from_db()
        self.assertEqual(STATUS_FAILED, run.status)
        self.assertIn("deleted", run.error)

    # --- the pages -----------------------------------------------------------------------

    def test_the_check_page_prefills_the_last_analysis_options(self):
        sample = self._analysed_sample(arguments="-b 5", coverage_limit="80")
        response = self.client.get("/breseq/check", {"experiment_id": self.experiment.id,
                                                     "sample_id": sample.id})
        html = response.content.decode()
        self.assertEqual(200, response.status_code)
        self.assertIn('value="-b 5"', html)
        self.assertIn('value="80.0"', html.replace('value="80"', 'value="80.0"'))
        self.assertIn("are kept", html)
        self.assertIn("applies <b>2</b> mutations", html)

    def test_the_sample_page_offers_check_curation_to_an_editor_only(self):
        sample = self._analysed_sample()
        url = "/breseq/check?experiment_id=%d&amp;sample_id=%d" % (self.experiment.id, sample.id)
        page = "/mutations/breseq?experiment_id=%d&sample_id=%d" % (self.experiment.id, sample.id)
        self.assertIn(url, self.client.get(page).content.decode())
        reader = User.objects.create(username="reader", email="r@e.com", is_active=True)
        from mutint_experiment.permissions import grant_project_access
        grant_project_access(self.project, reader, "read")
        self.client.force_login(reader)
        self.assertNotIn(url, self.client.get(page).content.decode())

    def test_the_run_list_tells_a_check_from_an_analysis(self):
        sample = self._analysed_sample()
        self._check(sample)
        rows = self.client.get("/breseq/runs?experiment_id=%d" % self.experiment.id).json()["runs"]
        kinds = {row["kind"]: row for row in rows}
        self.assertEqual({"analysis", "check"}, set(kinds))
        self.assertTrue(kinds["check"]["evidence_url"].endswith("sample_id=%d" % sample.id))
        self.assertTrue(kinds["check"]["check_report_url"].endswith("/check/"))
        self.assertIsNone(kinds["check"]["report_url"])
