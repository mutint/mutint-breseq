"""The whole path: drop reads, run breseq, import what it made.

`mutint_common.test_runner` forces `django.tasks` to its immediate backend, so `.enqueue()`
runs inline and one POST exercises launch, the subprocess, the ingest and the cleanup. That is
what makes an end-to-end test of an hours-long feature affordable -- with `fake_breseq` in
place of breseq itself, which is the only piece this repo is not responsible for.
"""

import json
import os
import shutil
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from mutint_common import store
from mutint_experiment.models import Project
from mutint_import import staging
from mutint_common.tools import ToolMissing
from mutint_import.tests import breseq_fixture
from mutint_jobs import logs
from mutint_sample.models import Mutation, MutationCall, Sample

from mutint_breseq import runner, tasks
from mutint_breseq.models import (
    STATUS_FAILED,
    STATUS_IMPORTED,
    STATUS_QUEUED,
    BreseqRun,
)
from mutint_breseq.tests import fake_breseq, fake_fastp
from mutint_breseq.tests.test_launch import establish_reference


class RunTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="owner", email="o@e.com", is_active=True)
        self.client.force_login(self.owner)

        self.store = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.store, True)
        self.tools = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tools, True)

        # What the fake copies into -o: a sample folder in exactly the shape
        # mutint_import.breseq_folder requires, built by core's own fixture.
        self.template_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.template_root, True)
        self.template = breseq_fixture.write_sample(self.template_root, "template")
        self.argv_record = os.path.join(self.template_root, "argv.json")
        self.fastp_record = os.path.join(self.template_root, "fastp.jsonl")

        fake_breseq.install(self.tools)
        fake_fastp.install(self.tools)
        patcher = override_settings(MUTINT_STORE_DIR=self.store, MUTINT_TOOLS_DIR=self.tools)
        patcher.enable()
        self.addCleanup(patcher.disable)

        for name, value in (("FAKE_BRESEQ_TEMPLATE", self.template),
                            ("FAKE_BRESEQ_ARGV", self.argv_record),
                            ("FAKE_FASTP_ARGV", self.fastp_record)):
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)

        self.project = Project.objects.create(name="p", user=self.owner)
        from mutint_experiment.views import _create_experiment
        self.experiment = _create_experiment(self.project, "e", self.owner)
        establish_reference(self.experiment)

    # --- helpers ------------------------------------------------------------------------

    def _stage(self, names=("s1_R1.fastq", "s1_R2.fastq"), content="ACGT"):
        session = staging.open_session(
            self.owner, self.experiment, "mutint_breseq",
            [{"path": name, "size": 4} for name in names])
        root = store.ensure_dir(store.staging_dir(session.id))
        for name in names:
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write(content)
        return session

    def _launch(self, sample_name="s1", arguments="", names=("s1_R1.fastq", "s1_R2.fastq"),
                trim_reads=None, content="ACGT"):
        session = self._stage(names, content=content)
        body = {"upload_id": str(session.id), "sample_name": sample_name,
                "arguments": arguments}
        if trim_reads is not None:
            body["trim_reads"] = trim_reads
        return self.client.post(
            "/breseq/launch?experiment_id=%s" % self.experiment.id,
            data=json.dumps(body), content_type="application/json")

    def _recorded_breseq(self):
        """Every breseq call the run made, in order; [] when it made none.

        One JSON line per call, because a run invokes breseq twice: `--dry-run`, then the
        real thing.
        """
        if not os.path.exists(self.argv_record):
            return []
        with open(self.argv_record) as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _recorded_argv(self):
        """The real run's call -- the last one that is not a dry run."""
        real = [call for call in self._recorded_breseq() if not call["dry_run"]]
        self.assertTrue(real, "breseq was never run for real")
        return real[-1]

    def _recorded_dry_runs(self):
        return [call for call in self._recorded_breseq() if call["dry_run"]]

    def _recorded_fastp(self):
        """Every fastp call the run made, in order; [] when it made none."""
        if not os.path.exists(self.fastp_record):
            return []
        with open(self.fastp_record) as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _job_logs(self):
        """Every job log under the store, as text.

        Read through `logs.read_tail` rather than by opening a filename, because a finished
        job's log is gzipped by the `task_finished` receiver and a running one is not -- which
        is exactly the distinction these tests are about. Keyed by the queue's id, which the
        run row does not carry until after `jobs.enqueue` returns, so this reads them all.
        """
        root = os.path.join(self.store, "components", "mutint_jobs")
        found = {}
        for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            text, _truncated = logs.read_tail(name)
            if text:
                found[name] = text
        return found

    # --- the preflight ------------------------------------------------------------------

    def test_the_dry_run_comes_before_any_trimming(self):
        """Order is the point: nothing should be trimmed for a command line breseq will not
        take. fastp's first call must fall after the dry run and before the real breseq."""
        self._launch()

        calls = self._recorded_breseq()
        self.assertTrue(calls[0]["dry_run"], "the first breseq call was not the preflight")
        self.assertFalse(calls[-1]["dry_run"], "the real run never happened")
        self.assertTrue(self._recorded_fastp(), "nothing was trimmed")
        # The fake appends as it goes, so the dry run's line existing before fastp ran is the
        # order. Asserted through the run's own log, which holds all three in sequence.
        run = BreseqRun.objects.get()
        log = "\n".join(self._job_logs().values())
        self.assertLess(log.index("--dry-run"), log.index("fastp"))
        self.assertEqual(run.status, STATUS_IMPORTED, run.error)

    def test_a_refused_command_line_fails_the_run_without_trimming_or_running(self):
        """The worker's half of the preflight, which launch can no longer produce.

        `views._preflight` refuses the same command line at launch, so reaching this state
        through the endpoint is impossible by construction -- and the task's check is not
        redundant for that: it runs on the machine that will do the work, which may not be the
        one that accepted the launch, and it is what catches a worker whose breseq is not the
        one the web host asked.
        """
        # Launched so that it *fails*, because a successful run imports and
        # `cleanup_after_import` takes the reads with it -- and this needs a run directory
        # still standing to re-run the task against.
        os.environ["FAKE_BRESEQ_FAIL"] = "3"
        self.assertEqual(self._launch().status_code, 200)
        os.environ.pop("FAKE_BRESEQ_FAIL", None)

        run = BreseqRun.objects.get()
        run.status, run.error, run.log = STATUS_QUEUED, "", ""
        run.save(update_fields=["status", "error", "log"])
        os.remove(self.argv_record)
        os.remove(self.fastp_record)

        os.environ["FAKE_BRESEQ_DRY_RUN_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_BRESEQ_DRY_RUN_FAIL", None)
        with self.assertRaises(RuntimeError):
            tasks.run_breseq.call(None, run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, STATUS_FAILED)
        self.assertIn("will not accept this command line", run.error)
        self.assertIn("no-such-flag", run.error)
        self.assertEqual([], self._recorded_fastp(), "reads were trimmed anyway")
        self.assertEqual([], [call for call in self._recorded_breseq()
                              if not call["dry_run"]], "breseq ran anyway")
        self.assertTrue(os.path.isdir(run.reads_dir()), "a failure keeps the reads")

    # --- the log ------------------------------------------------------------------------

    def test_the_log_can_be_read_while_the_tool_is_still_running(self):
        """The whole reason the output goes to a file instead of a pipe.

        Nothing could assert this before: `run_breseq_process` held everything in a pipe until
        the process exited, so a run three hours in had printed nothing anybody could see. The
        cancellation poll is used as the "during" moment because it is the one hook that fires
        while the subprocess is alive.
        """
        os.environ["FAKE_BRESEQ_SLEEP"] = "1"
        os.environ["FAKE_BRESEQ_PIDS"] = os.path.join(self.template_root, "pids.json")
        self.addCleanup(os.environ.pop, "FAKE_BRESEQ_SLEEP", None)
        self.addCleanup(os.environ.pop, "FAKE_BRESEQ_PIDS", None)

        seen = {}

        # `side_effect` is called with the mocked function's own arguments, so this takes the
        # queue id even though it does not use it.
        def look_then_cancel(task_result_id):
            if not seen:
                seen.update(self._job_logs())
            return bool(seen)

        with mock.patch.object(tasks.jobs, "is_cancelled", side_effect=look_then_cancel):
            self._launch()

        self.assertTrue(seen, "no log existed while breseq was running")
        written = "\n".join(seen.values())
        self.assertIn("$ ", written, "the command line is not in the log")
        self.assertIn("breseq", written)

    def test_the_log_holds_both_tools_in_the_order_they_ran(self):
        """fastp's account precedes breseq's because both append to one file, not because
        anything joins two strings in that order any more."""
        self._launch()

        written = "\n".join(self._job_logs().values())
        self.assertLess(written.index("fastp"), written.index("SUCCESSFULLY COMPLETED"))

    def test_the_log_survives_a_successful_run_s_cleanup(self):
        """It lives under `components/mutint_jobs/`, not under the run's own directory, which
        `cleanup_after_import` empties."""
        self._launch()
        run = BreseqRun.objects.get()

        self.assertEqual(run.status, STATUS_IMPORTED, run.error)
        self.assertTrue(self._job_logs(), "the log went with the run directory")

    # --- the happy path -----------------------------------------------------------------

    def test_a_run_produces_a_sample(self):
        response = self._launch()
        self.assertEqual(response.status_code, 200)

        run = BreseqRun.objects.get(pk=response.json()["run_id"])
        self.assertEqual(run.status, STATUS_IMPORTED, run.error)
        self.assertIsNotNone(run.sample, "the imported sample was not linked to the run")
        self.assertEqual(run.sample.source_name, "s1")

        # The mutations really landed, through core's own importer rather than anything here.
        self.assertEqual(Mutation.objects.filter(experiment=self.experiment).count(), 2)
        self.assertEqual(MutationCall.objects.filter(sample=run.sample).count(), 2)
        # And the alignment was stored, which is what the genome browser needs.
        self.assertTrue(Sample.objects.get(pk=run.sample_id).bam_stored)

    def test_the_sample_is_named_from_the_box(self):
        # Not from the read filenames. The whole reason this page exists rather than an
        # import handler is that the name is the person's to choose.
        self._launch(sample_name="Ara-2_500gen_763A", names=("weird_name_R1.fastq",))
        run = BreseqRun.objects.get()
        self.assertEqual(run.sample.source_name, "Ara-2_500gen_763A")
        # And that shape places the sample on its ALE, which is what makes it worth allowing.
        self.assertEqual(run.sample.population.name, "Ara-2")
        self.assertEqual(run.sample.time_point, 500)

    def test_the_reads_reach_breseq_as_positional_arguments(self):
        self._launch(names=("s1_R1.fastq", "s1_R2.fastq"))
        argv = self._recorded_argv()["argv"]
        self.assertEqual([os.path.basename(p) for p in argv[-2:]],
                         ["s1_R1.fastq", "s1_R2.fastq"])
        self.assertIn("-r", argv)
        self.assertTrue(argv[argv.index("-r") + 1].endswith("reference.gff3"))

    # --- trimming -------------------------------------------------------------------------

    def test_a_pair_is_trimmed_together_and_breseq_reads_the_trimmed_copies(self):
        response = self._launch(names=("s1_R1.fastq", "s1_R2.fastq"))
        run = BreseqRun.objects.get(pk=response.json()["run_id"])
        self.assertTrue(run.trim_reads)

        calls = self._recorded_fastp()
        self.assertEqual(1, len(calls), "one pair is one fastp call")
        fastp = calls[0]["argv"]
        self.assertIn("--disable_quality_filtering", fastp)
        self.assertIn("--detect_adapter_for_pe", fastp)
        self.assertEqual("s1_R1.fastq", os.path.basename(fastp[fastp.index("-i") + 1]))
        self.assertEqual("s1_R2.fastq", os.path.basename(fastp[fastp.index("-I") + 1]))
        # Same names, different directory: what keeps breseq pairing them and a .gz a .gz.
        self.assertEqual(os.path.join(run.trimmed_dir(), "s1_R1.fastq"),
                         fastp[fastp.index("-o") + 1])
        self.assertEqual(os.path.join(run.trimmed_dir(), "s1_R2.fastq"),
                         fastp[fastp.index("-O") + 1])
        # fastp shells out to nothing, but it must find the same environment breseq does.
        self.assertTrue(calls[0]["path"].startswith(os.path.join(self.tools, "bin")))

        breseq = self._recorded_argv()["argv"]
        self.assertEqual([os.path.join(run.trimmed_dir(), "s1_R1.fastq"),
                          os.path.join(run.trimmed_dir(), "s1_R2.fastq")], breseq[-2:])
        self.assertEqual(run.status, STATUS_IMPORTED, run.error)
        self.assertIn("total reads", run.log, "fastp's own summary reaches the log")

    def test_a_lone_file_is_trimmed_single_end(self):
        self._launch(names=("lane.fastq.gz",))
        fastp = self._recorded_fastp()[0]["argv"]
        self.assertIn("-i", fastp)
        self.assertNotIn("-I", fastp)
        self.assertNotIn("--detect_adapter_for_pe", fastp)

    def test_two_files_breseq_would_not_pair_are_trimmed_apart(self):
        # a1_R1 has two possible mates, so breseq leaves all three unpaired -- and so must this,
        # or breseq would meet trimmed files whose mates were decided differently.
        self._launch(names=("a1_R1.fastq", "a1_R2.fastq", "a2_R1.fastq"))
        calls = self._recorded_fastp()
        self.assertEqual(3, len(calls))
        for call in calls:
            self.assertNotIn("-I", call["argv"])

    def test_no_paired_mapping_in_the_box_trims_every_file_alone(self):
        self._launch(arguments="--no-paired-mapping")
        calls = self._recorded_fastp()
        self.assertEqual(2, len(calls))
        for call in calls:
            self.assertNotIn("--detect_adapter_for_pe", call["argv"])

    def test_trimming_can_be_switched_off(self):
        response = self._launch(trim_reads=False)
        run = BreseqRun.objects.get(pk=response.json()["run_id"])
        self.assertFalse(run.trim_reads)
        self.assertEqual([], self._recorded_fastp())
        breseq = self._recorded_argv()["argv"]
        self.assertEqual(run.reads_dir(), os.path.dirname(breseq[-1]))
        self.assertEqual(run.status, STATUS_IMPORTED, run.error)

    def test_long_reads_are_passed_to_breseq_untrimmed(self):
        # Sniffed from the file itself, at breseq's own trigger length, because there is no
        # flag that says "nanopore" -- breseq detects it by length too.
        long_read = "@r\n%s\n+\n%s\n" % ("A" * 1200, "I" * 1200)
        response = self._launch(names=("ont.fastq",), content=long_read)
        run = BreseqRun.objects.get(pk=response.json()["run_id"])
        self.assertEqual([], self._recorded_fastp())
        self.assertEqual(run.reads_dir(), os.path.dirname(self._recorded_argv()["argv"][-1]))
        self.assertIn("left ont.fastq untrimmed", run.log)
        self.assertIn("long reads", run.log)
        self.assertEqual(run.status, STATUS_IMPORTED, run.error)

    def test_a_file_that_is_not_fastq_is_passed_through(self):
        self._launch(names=("aligned.sam",), arguments="--aligned-sam")
        self.assertEqual([], self._recorded_fastp())
        run = BreseqRun.objects.get()
        self.assertIn("not a FASTQ", run.log)
        self.assertEqual("aligned.sam", os.path.basename(self._recorded_argv()["argv"][-1]))

    def test_fastp_failing_fails_the_run_and_keeps_the_directory(self):
        os.environ["FAKE_FASTP_FAIL"] = "2"
        self.addCleanup(os.environ.pop, "FAKE_FASTP_FAIL", None)
        self.assertEqual(self._launch().status_code, 200)

        run = BreseqRun.objects.get()
        self.assertEqual(run.status, STATUS_FAILED)
        self.assertIn("fastp exited 2", run.error)
        self.assertIn("adapter detection failed", run.log)
        self.assertTrue(os.path.isdir(run.reads_dir()))
        # breseq was never started **for real**. The preflight ran it before trimming, so the
        # record exists; what must not be there is a call without `--dry-run`.
        self.assertTrue(self._recorded_dry_runs(), "the preflight never ran")
        self.assertEqual([], [call for call in self._recorded_breseq()
                              if not call["dry_run"]])

    def test_a_missing_fastp_says_what_installs_it(self):
        os.remove(os.path.join(self.tools, "bin", "fastp"))
        with mock.patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(self._launch().status_code, 200)
        run = BreseqRun.objects.get()
        self.assertEqual(run.status, STATUS_FAILED)
        self.assertIn("fastp is not installed", run.error)

    def test_the_trimmed_copies_go_with_the_rest_after_import(self):
        response = self._launch()
        run = BreseqRun.objects.get(pk=response.json()["run_id"])
        self.assertEqual(run.status, STATUS_IMPORTED, run.error)
        self.assertFalse(os.path.exists(run.trimmed_dir()), "the trimmed reads were kept")

    def test_the_run_list_says_whether_the_reads_were_trimmed(self):
        self._launch(trim_reads=False)
        rows = self.client.get("/breseq/runs?experiment_id=%s" % self.experiment.id).json()
        self.assertFalse(rows["runs"][0]["trim_reads"])

    def test_typed_arguments_reach_breseq(self):
        self._launch(arguments="-p --polymorphism-minimum-variant-coverage 4")
        argv = self._recorded_argv()["argv"]
        self.assertIn("-p", argv)
        self.assertIn("--polymorphism-minimum-variant-coverage", argv)

    def test_the_tools_directory_is_on_the_path_breseq_sees(self):
        # breseq shells out to bowtie2 and samtools by bare name and exits 0 when it cannot
        # find them, so a missing PATH would be a silent no-op rather than a failure.
        self._launch()
        self.assertTrue(
            self._recorded_argv()["path"].startswith(os.path.join(self.tools, "bin")))

    def test_the_run_directory_is_emptied_and_the_report_is_on_the_sample(self):
        """The report belongs to the sample, not to the run that produced it.

        mutint-core's importer stores breseq's `output/` under the sample's own primary key,
        so nothing is left here -- and the report outlives this row, which is the point.
        """
        response = self._launch()
        run = BreseqRun.objects.get(pk=response.json()["run_id"])

        self.assertFalse(os.path.exists(run.reads_dir()), "the reads were kept")
        self.assertFalse(os.path.exists(run.output_dir()), "breseq's output was kept")

        from mutint_common import store as core_store
        self.assertTrue(run.sample.report_stored)
        self.assertTrue(os.path.isfile(
            os.path.join(core_store.sample_report_dir(run.sample_id), "index.html")))

    def test_the_staging_area_is_released(self):
        response = self._launch()
        run = BreseqRun.objects.get(pk=response.json()["run_id"])
        self.assertEqual(run.read_files, ["s1_R1.fastq", "s1_R2.fastq"])
        self.assertFalse(
            os.path.exists(os.path.join(self.store, "staging")) and
            os.listdir(os.path.join(self.store, "staging")),
            "the staged copy of the reads was left behind")

    def test_a_nested_drop_is_flattened(self):
        # A run folder from a sequencing core arrives with directories; breseq takes read
        # files positionally and has no notion of where they sat.
        self._launch(names=("L1/r.fastq", "L2/r.fastq"))
        run = BreseqRun.objects.get()
        self.assertEqual(run.read_files, ["L1__r.fastq", "L2__r.fastq"])
        self.assertEqual(run.status, STATUS_IMPORTED, run.error)

    # --- the report, which core owns ------------------------------------------------------

    def test_the_run_list_links_to_cores_viewer(self):
        """No route of our own any more.

        The plugin used to serve the report itself at /breseq/run/<pk>/report/<path>. That is
        gone: mutint-core keeps it under the sample and serves it sandboxed, and this links
        there. Containment and the sandbox are tested where they live, in
        `mutint_sample/tests/test_report.py`.
        """
        response = self._launch()
        run = BreseqRun.objects.get(pk=response.json()["run_id"])

        rows = self.client.get("/breseq/runs?experiment_id=%s" % self.experiment.id).json()
        row = [r for r in rows["runs"] if r["id"] == run.pk][0]
        self.assertEqual("/mutations/report/%d/" % run.sample_id, row["report_url"])

    def test_the_old_plugin_route_is_gone(self):
        response = self._launch()
        run = BreseqRun.objects.get(pk=response.json()["run_id"])
        self.assertEqual(
            404, self.client.get("/breseq/run/%d/report/index.html" % run.pk).status_code)

    # --- failure --------------------------------------------------------------------------

    def test_a_nonzero_exit_is_recorded_and_the_directory_kept(self):
        os.environ["FAKE_BRESEQ_FAIL"] = "3"
        self.addCleanup(os.environ.pop, "FAKE_BRESEQ_FAIL", None)

        # The launch itself still succeeds: enqueueing worked, and what happened afterwards is
        # the run's business. (Under the test runner's immediate backend the task has already
        # run by the time this returns, which is what lets the row be asserted below; on a
        # real worker the same row would be written minutes later.)
        self.assertEqual(self._launch().status_code, 200)

        run = BreseqRun.objects.get()
        self.assertEqual(run.status, STATUS_FAILED)
        self.assertIn("exited 3", run.error)
        # The log is what a person actually needs, and it has to survive the failure.
        self.assertIn("something went wrong", run.log)
        # Kept, so the run can be looked at. A failed run is exactly when the files matter.
        self.assertTrue(os.path.isdir(run.reads_dir()))
        self.assertIsNone(run.sample)

    def test_output_breseq_did_not_finish_writing_is_named(self):
        # breseq can print an error and still exit 0 -- a missing bowtie2 does exactly that --
        # so the returncode is not the test.
        os.environ["FAKE_BRESEQ_TRUNCATE"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_BRESEQ_TRUNCATE", None)

        self.assertEqual(self._launch().status_code, 200)

        run = BreseqRun.objects.get()
        self.assertEqual(run.status, STATUS_FAILED)
        self.assertIn("output.gd", run.error)
        self.assertEqual(Sample.objects.filter(population__experiment=self.experiment).count(), 0)

    def test_a_missing_breseq_says_what_installs_it(self):
        # The likeliest failure in production: a db_worker started outside ./mutint has no
        # MUTINT_TOOLS_DIR, so it finds no breseq and every run fails the same way.
        #
        # PATH is emptied as well as the tools dir moved aside, because `tool_path` falls back
        # to PATH deliberately -- a developer with their own breseq should not have to wait for
        # a solve. On a machine that has one, not clearing it tests the developer's breseq.
        with override_settings(MUTINT_TOOLS_DIR=os.path.join(self.tools, "empty")), \
                mock.patch.dict(os.environ, {"PATH": ""}):
            response = self._launch()

        # **Launch refuses now**, where this used to be a run that failed later: the preflight
        # needs breseq before it can ask breseq anything, so a machine without it is told at
        # the point somebody is standing there rather than after the upload is consumed.
        self.assertEqual(response.status_code, 400)
        self.assertIn("breseq is not installed", response.json()["error"])
        self.assertIn("install", response.json()["error"])
        self.assertFalse(BreseqRun.objects.exists(), "a refused launch left a run behind")

    def test_a_worker_without_breseq_fails_the_run_and_says_what_installs_it(self):
        """The other half, which launch can no longer reach.

        A run gets past the preflight on the web host and is then picked up by a `db_worker`
        started outside `./mutint`, which has no MUTINT_TOOLS_DIR and finds no breseq. Called
        directly, because by construction this cannot be produced through the endpoint.
        """
        self.assertEqual(self._launch().status_code, 200)
        run = BreseqRun.objects.get()
        run.status, run.error = STATUS_QUEUED, ""
        run.save(update_fields=["status", "error"])

        with override_settings(MUTINT_TOOLS_DIR=os.path.join(self.tools, "empty")), \
                mock.patch.dict(os.environ, {"PATH": ""}):
            with self.assertRaises(ToolMissing):
                tasks.run_breseq.call(None, run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, STATUS_FAILED)
        self.assertIn("breseq is not installed", run.error)
        self.assertIn("install", run.error)

    def test_the_task_re_raises_after_recording_a_failure(self):
        """Called directly, because `.enqueue()` hands the exception to the backend.

        Re-raising is deliberate and is easy to remove by accident: the row is what a person
        reads, and the queue's own record is what says a worker tried and could not. A task
        that returned quietly would leave `db_worker` reporting a clean run of a job that did
        nothing.
        """
        from mutint_breseq import tasks

        os.environ["FAKE_BRESEQ_FAIL"] = "3"
        self.addCleanup(os.environ.pop, "FAKE_BRESEQ_FAIL", None)
        self._launch()
        run = BreseqRun.objects.get()

        with self.assertRaises(RuntimeError):
            tasks.run_breseq.call(None, run.pk)

    def test_a_run_deleted_before_the_worker_reaches_it_is_not_an_error(self):
        from mutint_breseq import tasks
        self.assertIsNone(tasks.run_breseq.call(None, 999999))
