"""Stopping a run, and stopping *everything* it started.

The queue offers no way to interrupt a running task, so all of this rests on the run loop
asking. These tests exercise the loop against a real process, because the failure worth
catching -- signalling the parent and leaving its children alive -- is invisible to anything
that mocks the subprocess away.
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from aledb_common import store
from aledb_experiment.models import Project
from aledb_import import staging
from aledb_import.tests import breseq_fixture
from aledb_jobs import jobs as jobs_api

from mutint_breseq import runner, tasks
from mutint_breseq.models import STATUS_CANCELLED, BreseqRun
from mutint_breseq.tests import fake_breseq
from mutint_breseq.tests.test_launch import establish_reference


def alive(pid):
    """Whether `pid` is a live process. Signal 0 checks without delivering anything."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def wait_until_gone(pid, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


class ProcessGroupTestCase(TestCase):
    """`run_breseq_process` alone, with no database and no task."""

    def setUp(self):
        self.tools = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tools, True)
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)

        fake_breseq.install(self.tools)
        self.pids_file = os.path.join(self.work, "pids.json")
        self.env = dict(os.environ, **{
            "FAKE_BRESEQ_TEMPLATE": self.work,
            "FAKE_BRESEQ_ARGV": os.path.join(self.work, "argv.json"),
            "FAKE_BRESEQ_SLEEP": "1",
            "FAKE_BRESEQ_PIDS": self.pids_file,
        })
        self.breseq = os.path.join(self.tools, "bin", "breseq")

    def _pids(self, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if os.path.isfile(self.pids_file) and os.path.getsize(self.pids_file):
                with open(self.pids_file) as handle:
                    try:
                        return json.load(handle)
                    except ValueError:
                        pass
            time.sleep(0.05)
        self.fail("the fake breseq never reported its pids")

    def test_cancelling_kills_the_process_and_its_children(self):
        """The assertion the whole `killpg` design exists for.

        `process.kill()` would pass a test that checked only the parent, and would leave
        bowtie2 and samtools running on a real machine while the page said the job had
        stopped. So the child is asserted dead too.
        """
        pids = {}

        def is_cancelled():
            # Cancel as soon as the fake has spawned its child and told us both pids.
            if not pids:
                pids.update(self._pids())
            return True

        with self.assertRaises(runner.Cancelled):
            runner.run_breseq_process(
                [self.breseq, "-o", os.path.join(self.work, "out"), "-r", "ref.gff3"],
                self.env, timeout=60, is_cancelled=is_cancelled, poll_seconds=0.1)

        self.assertTrue(wait_until_gone(pids["parent"]), "breseq itself survived")
        self.assertTrue(wait_until_gone(pids["child"]),
                        "a child of breseq survived -- the signal did not reach the group")

    def test_a_run_nobody_cancels_completes_normally(self):
        env = dict(self.env)
        env.pop("FAKE_BRESEQ_SLEEP")
        os.makedirs(os.path.join(self.work, "data"), exist_ok=True)

        returncode, output = runner.run_breseq_process(
            [self.breseq, "-o", os.path.join(self.work, "out"), "-r", "ref.gff3"],
            env, timeout=60, is_cancelled=lambda: False, poll_seconds=0.1)

        self.assertEqual(0, returncode)
        self.assertIn("SUCCESSFULLY COMPLETED", output)

    def test_the_timeout_also_stops_the_group(self):
        pids = {}

        def capture():
            if not pids:
                pids.update(self._pids())
            return False

        with self.assertRaises(subprocess.TimeoutExpired):
            runner.run_breseq_process(
                [self.breseq, "-o", os.path.join(self.work, "out"), "-r", "ref.gff3"],
                self.env, timeout=1, is_cancelled=capture, poll_seconds=0.1)

        self.assertTrue(wait_until_gone(pids["parent"]))
        self.assertTrue(wait_until_gone(pids["child"]))

    def test_no_cancellation_callback_is_allowed(self):
        # A task that does not offer to be cancelled still has to be runnable.
        env = dict(self.env)
        env.pop("FAKE_BRESEQ_SLEEP")
        returncode, _ = runner.run_breseq_process(
            [self.breseq, "-o", os.path.join(self.work, "out"), "-r", "ref.gff3"],
            env, timeout=60, poll_seconds=0.1)
        self.assertEqual(0, returncode)


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
        patcher = override_settings(ALEDB_STORE_DIR=self.store, ALEDB_TOOLS_DIR=self.tools)
        patcher.enable()
        self.addCleanup(patcher.disable)

        for name, value in (("FAKE_BRESEQ_TEMPLATE", self.template),
                            ("FAKE_BRESEQ_ARGV",
                             os.path.join(self.template_root, "argv.json"))):
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)

        self.project = Project.objects.create(name="p", user=self.owner)
        from aledb_experiment.views import _create_experiment
        self.experiment = _create_experiment(self.project, "e", self.owner)
        establish_reference(self.experiment)

    def _launch(self, sample_name="s1"):
        session = staging.open_session(
            self.owner, self.experiment, "mutint_breseq", [{"path": "r1.fastq", "size": 4}])
        root = store.ensure_dir(store.staging_dir(session.id))
        with open(os.path.join(root, "r1.fastq"), "w") as handle:
            handle.write("ACGT")
        return self.client.post(
            "/breseq/launch?experiment_id=%s" % self.experiment.id,
            data=json.dumps({"upload_id": str(session.id),
                             "sample_name": sample_name, "arguments": ""}),
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

        self.assertIsNone(tasks.run_breseq.call(run.pk))

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

        tasks.run_breseq.call(run.pk)          # does not raise

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

    def test_a_cancelled_run_can_then_be_deleted(self):
        self._launch()
        run = BreseqRun.objects.get()
        job = jobs_api.for_user(self.owner).get(task_result_id=run.task_result_id)
        jobs_api.request_cancel(job, by=self.owner)
        tasks.run_breseq.call(run.pk)

        response = self.client.post("/breseq/run/%d/delete" % run.pk)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BreseqRun.objects.filter(pk=run.pk).exists())
