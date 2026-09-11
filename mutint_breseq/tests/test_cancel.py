"""Stopping a run: the whole path, from the button to what is left on disk.

The queue offers no way to interrupt a running task, so all of this rests on the run loop
asking. **The loop itself is tested in core** -- `mutint_jobs.tests.test_processes`, which is
where `run_tool` lives and where the process-group assertions went with it. What is left here
is what this plugin decides: that a cancellation is not a failure, that it throws the work
away, and that the page stops offering the button.
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
from mutint_import import sra_fetch, staging
from mutint_import.tests import breseq_fixture
from mutint_import.tests.test_sra_fetch import _row, fake_ena, no_delay
from mutint_jobs import jobs as jobs_api
# The process-group helpers live with the loop they were written for, in core.
from mutint_jobs.tests.test_processes import wait_until_gone

from mutint_breseq import tasks
from mutint_breseq.models import STATUS_CANCELLED, BreseqRun
from mutint_breseq.tests import fake_breseq, fake_fastp, fastq_fixture
from mutint_breseq.tests.test_launch import establish_reference


DATABASE_BACKEND = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend"}}


@override_settings(TASKS=DATABASE_BACKEND)
class CancelledRunTestCase(TestCase):
    """The whole path: a launched run, cancelled, and what is left of it.

    Against the **database** backend, not the immediate one the suite runs under, and that is
    not incidental: under the immediate backend `.enqueue()` means "run it now", so launching
    would complete the whole analysis before there was anything to cancel. A queued job only
    exists where a queue does.
    """

    def setUp(self):
        self.owner = User.objects.create(username="owner", email="o@e.com", is_active=True)
        self.client.force_login(self.owner)

        self.store = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.store, True)
        self.tools = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tools, True)
        self.template_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.template_root, True)
        self.template = breseq_fixture.write_sample(self.template_root, "template")

        fake_breseq.install(self.tools)
        fake_fastp.install(self.tools)
        patcher = override_settings(MUTINT_STORE_DIR=self.store, MUTINT_TOOLS_DIR=self.tools)
        patcher.enable()
        self.addCleanup(patcher.disable)

        for name, value in (("FAKE_BRESEQ_TEMPLATE", self.template),
                            ("FAKE_BRESEQ_ARGV",
                             os.path.join(self.template_root, "argv.json"))):
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)

        self.project = Project.objects.create(name="p", user=self.owner)
        from mutint_experiment.views import _create_experiment
        self.experiment = _create_experiment(self.project, "e", self.owner)
        establish_reference(self.experiment)

    def _launch(self, sample="s1"):
        session = staging.open_session(
            self.owner, self.experiment, "mutint_breseq", [{"path": "r1.fastq", "size": 4}])
        root = store.ensure_dir(store.staging_dir(session.id))
        with open(os.path.join(root, "r1.fastq"), "w") as handle:
            handle.write("ACGT")
        return self.client.post(
            "/breseq/launch?experiment_id=%s" % self.experiment.id,
            data=json.dumps({"upload_id": str(session.id),
                             "sample": sample, "population": "", "time_point": "",
                             "arguments": ""}),
            content_type="application/json")

    def test_launching_records_a_cancellable_job(self):
        self._launch()
        run = BreseqRun.objects.get()
        job = jobs_api.for_user(self.owner).get(task_result_id=run.task_result_id)

        self.assertTrue(job.cancellable)
        self.assertEqual(job.user, self.owner)
        self.assertEqual(job.experiment, self.experiment)
        self.assertIn("s1", job.label)
        self.assertEqual(job.component, "mutint_breseq")

    def test_a_run_cancelled_before_it_starts_does_nothing_and_says_so(self):
        """The queue is never touched by cancelling, so the task is still handed to a worker.

        This is that path: it starts, sees the flag, and stops before running breseq at all.
        """
        self._launch()
        run = BreseqRun.objects.get()
        job = jobs_api.for_user(self.owner).get(task_result_id=run.task_result_id)
        jobs_api.request_cancel(job, by=self.owner)

        self.assertIsNone(tasks.run_breseq.call(None, run.pk))

        run.refresh_from_db()
        self.assertEqual(run.status, STATUS_CANCELLED)
        self.assertIsNone(run.sample)
        # The files go, which is the one respect in which cancelled differs from failed.
        self.assertFalse(os.path.exists(run.reads_dir()))

    def test_cancelling_is_not_recorded_as_a_failure(self):
        # It must not raise: a cancellation is not a failure, and letting it out would record
        # the job FAILED on the queue and put a traceback in front of somebody who got exactly
        # what they asked for.
        self._launch()
        run = BreseqRun.objects.get()
        job = jobs_api.for_user(self.owner).get(task_result_id=run.task_result_id)
        jobs_api.request_cancel(job, by=self.owner)

        tasks.run_breseq.call(None, run.pk)          # does not raise

        run.refresh_from_db()
        self.assertEqual(run.error, "")

    def test_the_jobs_page_offers_cancel_and_then_stops_offering_it(self):
        self._launch()
        run = BreseqRun.objects.get()
        job = jobs_api.for_user(self.owner).get(task_result_id=run.task_result_id)

        response = self.client.post("/jobs/%d/cancel" % job.pk)
        self.assertEqual(response.status_code, 200)

        rows = self.client.get("/jobs/list").json()["jobs"]
        row = [r for r in rows if r["id"] == job.pk][0]
        self.assertTrue(row["cancel_requested"])
        # Asked once is enough; a second button would suggest the first had not worked.
        self.assertFalse(row["cancellable"])

    def test_a_cancel_during_trimming_stops_fastp_and_never_starts_breseq(self):
        """The fastp stage polls through the same loop breseq does, and the gap after it polls
        once more -- so a cancel that lands mid-trim ends the run there."""
        pids_file = os.path.join(self.template_root, "fastp_pids.json")
        os.environ["FAKE_FASTP_SLEEP"] = "1"
        os.environ["FAKE_FASTP_PIDS"] = pids_file
        self.addCleanup(os.environ.pop, "FAKE_FASTP_SLEEP", None)
        self.addCleanup(os.environ.pop, "FAKE_FASTP_PIDS", None)

        self._launch()
        run = BreseqRun.objects.get()
        # Not cancelled at entry; cancelled at every poll after it. The first poll is the one
        # inside the fastp loop, two seconds in.
        with mock.patch.object(tasks.jobs, "is_cancelled", side_effect=[False] + [True] * 50):
            self.assertIsNone(tasks.run_breseq.call(None, run.pk))

        run.refresh_from_db()
        self.assertEqual(run.status, STATUS_CANCELLED)
        with open(pids_file) as handle:
            pids = json.load(handle)
        self.assertTrue(wait_until_gone(pids["parent"]))
        self.assertTrue(wait_until_gone(pids["child"]), "fastp's child outlived the cancel")
        self.assertFalse(os.path.exists(run.trimmed_dir()))
        self.assertFalse(os.path.exists(run.reads_dir()))
        # Not "the file does not exist" any more: the preflight runs breseq with `--dry-run`
        # before trimming, so the record is already there. What must not have happened is a
        # *real* run.
        with open(os.path.join(self.template_root, "argv.json")) as handle:
            calls = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(calls, "the preflight never ran")
        self.assertTrue(all(call["dry_run"] for call in calls),
                        "breseq was started for real after the cancel")

    def test_a_cancel_during_the_download_deletes_the_partial_file_and_never_starts_breseq(self):
        """The download polls the same flag the tool loop does, between chunks; a cancel that
        lands mid-file ends the run there, with nothing left on disk that breseq could read."""
        reads = fastq_fixture.write(os.path.join(self.template_root, "r.fastq.gz"), count=64)
        with open(reads, "rb") as handle:
            data = handle.read()
        rows = {"SRR1": [_row(run="SRR1", files=[("SRR1.fastq.gz", data)])]}

        with mock.patch("mutint_import.sra_fetch.requests.get",
                        side_effect=fake_ena(rows, {"SRR1.fastq.gz": data})):
            response = self.client.post(
                "/breseq/launch?experiment_id=%s" % self.experiment.id,
                data=json.dumps({"upload_id": "", "accessions": "SRR1", "sample": "s1",
                                 "population": "", "time_point": "", "arguments": ""}),
                content_type="application/json")
            self.assertEqual(response.status_code, 200, response.content)
            run = BreseqRun.objects.get()

            # Not cancelled at entry; cancelled at the first poll after it, which is inside
            # the download once the poll gap is zero.
            with no_delay(), mock.patch.object(sra_fetch, "CANCEL_POLL_SECONDS", 0), \
                    mock.patch.object(tasks.jobs, "is_cancelled",
                                      side_effect=[False] + [True] * 50):
                self.assertIsNone(tasks.run_breseq.call(None, run.pk))

        run.refresh_from_db()
        self.assertEqual(run.status, STATUS_CANCELLED)
        self.assertFalse(os.path.exists(run.reads_dir()))
        # The launch's preflight is a dry run and is recorded; the worker's dry run comes
        # after the download and must not have happened, let alone the real run.
        with open(os.path.join(self.template_root, "argv.json")) as handle:
            calls = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(1, len(calls), "breseq was started after the cancel")
        self.assertTrue(calls[0]["dry_run"])

    def test_relaunching_the_same_sample_stops_the_run_already_under_way(self):
        """Two runs writing one sample race, and the import supersedes a sample's calls
        rather than adding to them -- so the older run's output is about to be overwritten
        whatever happens, and finishing it is hours of CPU spent on something discarded."""
        self._launch()
        first = BreseqRun.objects.get()
        self.assertFalse(
            jobs_api.for_user(self.owner).get(
                task_result_id=first.task_result_id).cancel_requested)

        response = self._launch()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(1, response.json()["superseded"])
        self.assertTrue(
            jobs_api.for_user(self.owner).get(
                task_result_id=first.task_result_id).cancel_requested,
            "the run already under way was left running")
        # And the new one is not asked to stop by its own arrival.
        second = BreseqRun.objects.exclude(pk=first.pk).get()
        self.assertFalse(
            jobs_api.for_user(self.owner).get(
                task_result_id=second.task_result_id).cancel_requested)

    def test_a_different_sample_is_left_alone(self):
        self._launch(sample="s1")
        first = BreseqRun.objects.get()

        response = self._launch(sample="s2")

        self.assertEqual(0, response.json()["superseded"])
        self.assertFalse(
            jobs_api.for_user(self.owner).get(
                task_result_id=first.task_result_id).cancel_requested)

    def test_a_finished_run_is_not_disturbed(self):
        """Only queued and running rows are in flight. A finished one has nothing to stop."""
        self._launch()
        first = BreseqRun.objects.get()
        first.status = STATUS_CANCELLED
        first.save(update_fields=["status"])

        self.assertEqual(0, self._launch().json()["superseded"])

    def test_a_cancelled_run_can_then_be_deleted(self):
        self._launch()
        run = BreseqRun.objects.get()
        job = jobs_api.for_user(self.owner).get(task_result_id=run.task_result_id)
        jobs_api.request_cancel(job, by=self.owner)
        tasks.run_breseq.call(None, run.pk)

        response = self.client.post("/breseq/run/%d/delete" % run.pk)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BreseqRun.objects.filter(pk=run.pk).exists())
