"""The whole path: drop reads, run breseq, import what it made.

`aledb_common.test_runner` forces `django.tasks` to its immediate backend, so `.enqueue()`
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

from aledb_common import store
from aledb_experiment.models import Project
from aledb_import import staging
from aledb_import.tests import breseq_fixture
from aledb_sample.models import Mutation, MutationCall, Sample

from mutint_breseq import runner
from mutint_breseq.models import STATUS_FAILED, STATUS_IMPORTED, BreseqRun
from mutint_breseq.tests import fake_breseq
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
        # aledb_import.breseq_folder requires, built by core's own fixture.
        self.template_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.template_root, True)
        self.template = breseq_fixture.write_sample(self.template_root, "template")
        self.argv_record = os.path.join(self.template_root, "argv.json")

        fake_breseq.install(self.tools)
        patcher = override_settings(ALEDB_STORE_DIR=self.store, ALEDB_TOOLS_DIR=self.tools)
        patcher.enable()
        self.addCleanup(patcher.disable)

        for name, value in (("FAKE_BRESEQ_TEMPLATE", self.template),
                            ("FAKE_BRESEQ_ARGV", self.argv_record)):
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)

        self.project = Project.objects.create(name="p", user=self.owner)
        from aledb_experiment.views import _create_experiment
        self.experiment = _create_experiment(self.project, "e", self.owner)
        establish_reference(self.experiment)

    # --- helpers ------------------------------------------------------------------------

    def _stage(self, names=("s1_R1.fastq", "s1_R2.fastq")):
        session = staging.open_session(
            self.owner, self.experiment, "mutint_breseq",
            [{"path": name, "size": 4} for name in names])
        root = store.ensure_dir(store.staging_dir(session.id))
        for name in names:
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write("ACGT")
        return session

    def _launch(self, sample_name="s1", arguments="", names=("s1_R1.fastq", "s1_R2.fastq")):
        session = self._stage(names)
        return self.client.post(
            "/breseq/launch?experiment_id=%s" % self.experiment.id,
            data=json.dumps({"upload_id": str(session.id),
                             "sample_name": sample_name, "arguments": arguments}),
            content_type="application/json")

    def _recorded_argv(self):
        with open(self.argv_record) as handle:
            return json.load(handle)

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

        aledb-core's importer stores breseq's `output/` under the sample's own primary key,
        so nothing is left here -- and the report outlives this row, which is the point.
        """
        response = self._launch()
        run = BreseqRun.objects.get(pk=response.json()["run_id"])

        self.assertFalse(os.path.exists(run.reads_dir()), "the reads were kept")
        self.assertFalse(os.path.exists(run.output_dir()), "breseq's output was kept")

        from aledb_common import store as core_store
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
        gone: aledb-core keeps it under the sample and serves it sandboxed, and this links
        there. Containment and the sandbox are tested where they live, in
        `aledb_sample/tests/test_report.py`.
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
        # ALEDB_TOOLS_DIR, so it finds no breseq and every run fails the same way.
        #
        # PATH is emptied as well as the tools dir moved aside, because `tool_path` falls back
        # to PATH deliberately -- a developer with their own breseq should not have to wait for
        # a solve. On a machine that has one, not clearing it tests the developer's breseq.
        with override_settings(ALEDB_TOOLS_DIR=os.path.join(self.tools, "empty")), \
                mock.patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(self._launch().status_code, 200)

        run = BreseqRun.objects.get()
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
            tasks.run_breseq.call(run.pk)

    def test_a_run_deleted_before_the_worker_reaches_it_is_not_an_error(self):
        from mutint_breseq import tasks
        self.assertIsNone(tasks.run_breseq.call(999999))
