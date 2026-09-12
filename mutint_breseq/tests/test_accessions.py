"""Reads named by SRA accession instead of, or beside, a drop.

The fetch is core's (`mutint_import.sra_fetch`) and is tested there against a fake ENA. What
is tested here is what this plugin decides: that a launch with no drop opens no session, how
an accession becomes a sample and what it is called, that the download happens on the worker
and shows in the log, and that the sample records the run it came from. Every test patches
`requests.get`; **none may reach the network**.
"""

import json
import os
import shutil
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from mutint_common import store
from mutint_experiment.models import Project
from mutint_import import staging
from mutint_import.models import STATE_OPEN, UploadSession
from mutint_import.tests import breseq_fixture
from mutint_import.tests.test_sra_fetch import _row, fake_ena, no_delay
from mutint_jobs import logs
from mutint_sample.models import Sample

from mutint_breseq.models import STATUS_FAILED, STATUS_IMPORTED, BreseqRun
from mutint_breseq.tests import fake_breseq, fake_fastp, fastq_fixture
from mutint_breseq.tests.test_launch import establish_reference


class AccessionTestCase(TestCase):
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
        self.argv_record = os.path.join(self.template_root, "argv.json")

        fake_breseq.install(self.tools)
        fake_fastp.install(self.tools)
        patcher = override_settings(MUTINT_STORE_DIR=self.store, MUTINT_TOOLS_DIR=self.tools)
        patcher.enable()
        self.addCleanup(patcher.disable)
        for name, value in (("FAKE_BRESEQ_TEMPLATE", self.template),
                            ("FAKE_BRESEQ_ARGV", self.argv_record),
                            ("FAKE_FASTP_ARGV", os.path.join(self.template_root, "fastp.jsonl"))):
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)

        self.project = Project.objects.create(name="p", user=self.owner)
        from mutint_experiment.views import _create_experiment
        self.experiment = _create_experiment(self.project, "e", self.owner)
        establish_reference(self.experiment)

        # What the fake ENA serves: bytes by filename, and rows by accession. Filled per test.
        self.served = {}
        self.rows = {}

    # --- the fake archive ---------------------------------------------------------------

    def _reads(self, name, count=4):
        path = os.path.join(self.template_root, "ena_" + name)
        fastq_fixture.write(path, count=count)
        with open(path, "rb") as handle:
            return handle.read()

    def _run(self, run="SRR1", sample="SAMN1", alias="REL768A", paired=True):
        """One run ENA knows: its files are served and its row is ready to be listed."""
        if paired:
            files = [("%s_1.fastq.gz" % run, self._reads("%s_1.fastq.gz" % run)),
                     ("%s_2.fastq.gz" % run, self._reads("%s_2.fastq.gz" % run))]
        else:
            files = [("%s.fastq.gz" % run, self._reads("%s.fastq.gz" % run))]
        self.served.update(dict(files))
        return _row(run=run, sample=sample, alias=alias, files=files)

    def _ena(self, **rows_by_token):
        """`SRR1=[row, ...]` and so on: what the file report answers for each token."""
        self.rows.update(rows_by_token)
        return mock.patch("mutint_import.sra_fetch.requests.get",
                          side_effect=fake_ena(self.rows, self.served))

    # --- the page -----------------------------------------------------------------------

    def _stage(self, names):
        session = staging.open_session(
            self.owner, self.experiment, "mutint_breseq",
            [{"path": name, "size": 4} for name in names])
        root = store.ensure_dir(store.staging_dir(session.id))
        for name in names:
            fastq_fixture.write(os.path.join(root, name), count=4)
        return session

    def _launch(self, accessions="", names=(), sample="s1", input_mode=None,
                sample_name=None, arguments=""):
        body = {"accessions": accessions, "sample": sample, "population": "",
                "time_point": "", "arguments": arguments,
                "upload_id": str(self._stage(names).id) if names else ""}
        if input_mode is not None:
            body["input_mode"] = input_mode
        if sample_name is not None:
            body["sample_name"] = sample_name
        return self.client.post(
            "/breseq/launch?experiment_id=%s" % self.experiment.id,
            data=json.dumps(body), content_type="application/json")

    def _preview(self, accessions="", names=()):
        return self.client.post(
            "/breseq/preview?experiment_id=%s" % self.experiment.id,
            data=json.dumps({"names": list(names), "accessions": accessions}),
            content_type="application/json")

    def _recorded_argv(self):
        with open(self.argv_record) as handle:
            calls = [json.loads(line) for line in handle if line.strip()]
        real = [call for call in calls if not call["dry_run"]]
        self.assertTrue(real, "breseq was never run for real")
        return real[-1]["argv"]

    def _job_logs(self):
        root = os.path.join(self.store, "components", "mutint_jobs")
        return "\n".join(logs.read_tail(name)[0]
                         for name in (sorted(os.listdir(root)) if os.path.isdir(root) else []))

    # --- launching ----------------------------------------------------------------------

    def test_an_accession_alone_opens_no_session_and_produces_a_sample(self):
        with no_delay(), self._ena(SRR1=[self._run()]):
            response = self._launch("SRR1")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(UploadSession.objects.count(), 0)
        run = BreseqRun.objects.get()
        self.assertEqual(run.status, STATUS_IMPORTED)
        self.assertEqual(run.read_files, ["SRR1_1.fastq.gz", "SRR1_2.fastq.gz"])
        self.assertEqual([plan["typed"] for plan in run.accessions], ["SRR1"])
        self.assertEqual(Sample.objects.filter(population__experiment=self.experiment).count(), 1)

    def test_the_download_is_logged_and_reaches_breseq_by_basename(self):
        with no_delay(), self._ena(SRR1=[self._run()]):
            self._launch("SRR1")
        log = self._job_logs()
        self.assertIn("Downloading SRR1_1.fastq.gz (1 of 2", log)
        self.assertIn("Downloading SRR1_2.fastq.gz (2 of 2", log)
        self.assertLess(log.index("Downloading SRR1_1"), log.index("--dry-run"))
        # Trimmed copies keep their basenames, so breseq is handed ENA's names either way --
        # which is what lets its mate rule pair them.
        argv = self._recorded_argv()
        self.assertTrue(any(arg.endswith("SRR1_1.fastq.gz") for arg in argv), argv)

    def test_a_dropped_pair_and_an_accession_are_one_sample_in_name_mode(self):
        with no_delay(), self._ena(SRR1=[self._run()]):
            response = self._launch("SRR1", names=("s1_R1.fastq", "s1_R2.fastq"),
                                    input_mode="name", sample_name="s1")

        self.assertEqual(response.status_code, 200, response.content)
        run = BreseqRun.objects.get()
        self.assertEqual(run.status, STATUS_IMPORTED)
        self.assertEqual(run.read_files,
                         ["SRR1_1.fastq.gz", "SRR1_2.fastq.gz", "s1_R1.fastq", "s1_R2.fastq"])
        self.assertEqual(run.sample_name, "s1")
        argv = self._recorded_argv()
        self.assertEqual(4, sum(1 for arg in argv if arg.endswith((".fastq", ".fastq.gz"))))
        # The staged copy is released like any other drop's.
        self.assertEqual(UploadSession.objects.get().state, "finalized")

    def test_in_read_names_mode_each_accession_is_its_own_sample_named_by_alias(self):
        with no_delay(), self._ena(SRR1=[self._run("SRR1", "SAMN1", "REL768A")],
                                   SAMN2=[self._run("SRR2", "SAMN2", "REL1158A"),
                                          self._run("SRR3", "SAMN2", "REL1158A", paired=False)]):
            response = self._launch("SRR1 SAMN2", input_mode="read_names")

        self.assertEqual(response.status_code, 200, response.content)
        runs = {run.sample_name: run for run in BreseqRun.objects.all()}
        self.assertEqual(set(runs), {"REL768A", "REL1158A"})
        self.assertEqual(runs["REL1158A"].read_files,
                         ["SRR2_1.fastq.gz", "SRR2_2.fastq.gz", "SRR3.fastq.gz"])
        self.assertEqual([plan["typed"] for plan in runs["REL1158A"].accessions], ["SAMN2"])
        self.assertEqual([plan["typed"] for plan in runs["REL768A"].accessions], ["SRR1"])
        self.assertEqual(
            {run.status for run in runs.values()}, {STATUS_IMPORTED})
        self.assertEqual(Sample.objects.filter(population__experiment=self.experiment).count(), 2)

    def test_an_alias_the_pattern_refuses_falls_back_to_the_accession(self):
        with no_delay(), self._ena(SRR1=[self._run(alias="E. coli / REL606")]):
            response = self._launch("SRR1", input_mode="read_names")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(BreseqRun.objects.get().sample_name, "SRR1")

    def test_a_study_makes_one_run_per_biosample(self):
        with no_delay(), self._ena(SRP1=[self._run("SRR1", "SAMN1", "a1"),
                                         self._run("SRR2", "SAMN2", "a2"),
                                         self._run("SRR3", "SAMN1", "a1")]):
            response = self._launch("SRP1", input_mode="read_names")

        self.assertEqual(response.status_code, 200, response.content)
        runs = {run.sample_name: run for run in BreseqRun.objects.all()}
        self.assertEqual(set(runs), {"a1", "a2"})
        self.assertEqual(runs["a1"].read_files,
                         ["SRR1_1.fastq.gz", "SRR1_2.fastq.gz",
                          "SRR3_1.fastq.gz", "SRR3_2.fastq.gz"])
        # Each row carries only its own runs, under the study's name.
        self.assertEqual([r["accession"] for r in runs["a2"].accessions[0]["runs"]], ["SRR2"])
        self.assertEqual(runs["a2"].accessions[0]["typed"], "SRP1")

    def test_a_study_member_with_no_alias_is_named_by_its_biosample(self):
        with no_delay(), self._ena(SRP1=[self._run("SRR1", "SAMN1", "")]):
            self._launch("SRP1", input_mode="read_names")
        self.assertEqual(BreseqRun.objects.get().sample_name, "SAMN1")

    def test_two_samples_with_one_name_are_refused(self):
        with no_delay(), self._ena(SRR1=[self._run("SRR1", "SAMN1", "same")],
                                   SRR2=[self._run("SRR2", "SAMN2", "same")]):
            response = self._launch("SRR1 SRR2", input_mode="read_names")
        self.assertEqual(response.status_code, 400)
        self.assertIn("same", response.json()["error"])
        self.assertEqual(response.json()["field"], "accessions")
        self.assertEqual(BreseqRun.objects.count(), 0)

    # --- refusals, and what they leave behind -------------------------------------------

    def test_an_unknown_accession_is_refused_and_the_session_left_open(self):
        with no_delay(), self._ena():
            response = self._launch("SRR999", names=("s1_R1.fastq", "s1_R2.fastq"))

        self.assertEqual(response.status_code, 400)
        self.assertIn("SRR999", response.json()["error"])
        self.assertEqual(response.json()["field"], "accessions")
        self.assertEqual(BreseqRun.objects.count(), 0)
        # Refused before the claim: fix the box and press the button again.
        self.assertEqual(UploadSession.objects.get().state, STATE_OPEN)

    def test_a_token_of_no_sra_shape_is_refused_before_ena_is_asked(self):
        with self._ena() as get:
            response = self._launch("NC_000913.3")
        self.assertEqual(response.status_code, 400)
        self.assertIn("run", response.json()["error"])
        get.assert_not_called()

    def test_a_run_with_no_fastq_is_refused_naming_the_run(self):
        row = self._run("SRR5", "SAMN5")
        row["fastq_ftp"] = row["fastq_md5"] = row["fastq_bytes"] = ""
        with no_delay(), self._ena(SAMN5=[row]):
            response = self._launch("SAMN5")
        self.assertEqual(response.status_code, 400)
        self.assertIn("SRR5", response.json()["error"])
        self.assertIn("no FASTQ", response.json()["error"])

    def test_a_run_named_twice_is_refused_before_anything_is_claimed(self):
        with no_delay(), self._ena(SRP1=[self._run("SRR1"), self._run("SRR2")],
                                   SRR2=[self._run("SRR2")]):
            response = self._launch("SRP1 SRR2", names=("s1_R1.fastq", "s1_R2.fastq"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("SRR2 is named twice", response.json()["error"])
        self.assertEqual(UploadSession.objects.get().state, STATE_OPEN)

    def test_a_dropped_file_named_like_an_ena_file_is_refused(self):
        with no_delay(), self._ena(SRR1=[self._run()]):
            response = self._launch("SRR1", names=("SRR1_1.fastq.gz", "other.fastq"))
        self.assertEqual(response.status_code, 409)
        self.assertIn("SRR1_1.fastq.gz", response.json()["error"])
        self.assertEqual(BreseqRun.objects.count(), 0)
        self.assertEqual(UploadSession.objects.get().state, "failed")

    def test_neither_files_nor_accessions_is_refused(self):
        with self._ena():
            response = self._launch("")
        self.assertEqual(response.status_code, 400)
        self.assertIn("accession", response.json()["error"])
        self.assertEqual(BreseqRun.objects.count(), 0)

    def test_a_reader_may_not_make_the_server_ask_ena(self):
        # A lock leaves the owner able to see and not to write, which is the distinction.
        self.experiment.locked_at = timezone.now()
        self.experiment.save(update_fields=["locked_at"])
        with self._ena(SRR1=[self._run()]) as get:
            response = self._preview("SRR1")
        self.assertEqual(response.status_code, 403)
        get.assert_not_called()
        # And an ordinary preview of names still works for them.
        self.assertEqual(self._preview(names=["a_R1.fastq"]).status_code, 200)

    # --- the worker ---------------------------------------------------------------------

    def test_a_checksum_failure_fails_the_run_with_the_sentence_and_discards_the_reads(self):
        row = self._run()
        self.served["SRR1_2.fastq.gz"] = b"x" * len(self.served["SRR1_2.fastq.gz"])
        with no_delay(), self._ena(SRR1=[row]):
            # The task re-raises after recording; the immediate backend keeps the exception
            # as the job's result rather than letting it out of the POST.
            self.assertEqual(self._launch("SRR1").status_code, 200)

        run = BreseqRun.objects.get()
        self.assertEqual(run.status, STATUS_FAILED)
        self.assertIn("checksum", run.error)
        self.assertIn("SRR1_2.fastq.gz", run.error)
        # The file that did arrive goes with the rest: a failure discards its reads like
        # every other ending, and the sentence on the row is what survives.
        self.assertFalse(os.path.exists(run.reads_dir()))
        # The launch's own preflight is a dry run; nothing after it may have run breseq.
        with open(self.argv_record) as handle:
            calls = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(all(call["dry_run"] for call in calls),
                        "breseq was run without its reads")

    def test_the_sample_records_the_run_as_one_sra_input_and_dropped_files_as_reads(self):
        with no_delay(), self._ena(SRR1=[self._run()]):
            self._launch("SRR1", names=("s1_R1.fastq", "s1_R2.fastq"),
                         input_mode="name", sample_name="s1")

        sample = Sample.objects.get(pk=BreseqRun.objects.get().sample_id)
        by_kind = {}
        for entry in sample.inputs:
            by_kind.setdefault(entry["kind"], []).append(entry)
        self.assertEqual(set(by_kind), {"reads", "sra"})
        self.assertEqual([entry["value"] for entry in by_kind["sra"]], ["SRR1"])
        self.assertEqual(sorted(entry["value"] for entry in by_kind["reads"]),
                         ["s1_R1.fastq", "s1_R2.fastq"])
        # The pair is still a pair, in a group of its own; the run is in another.
        self.assertEqual(1, len({entry["group"] for entry in by_kind["reads"]}))
        self.assertNotEqual(by_kind["sra"][0]["group"], by_kind["reads"][0]["group"])

    # --- the preview and the run list ---------------------------------------------------

    def test_the_preview_lists_accession_samples_with_their_runs_and_bytes(self):
        with no_delay(), self._ena(SAMN2=[self._run("SRR2", "SAMN2", "REL1158A"),
                                          self._run("SRR3", "SAMN2", "REL1158A", paired=False)]):
            response = self._preview("SAMN2", names=["a_R1.fastq", "a_R2.fastq"])

        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["samples"]
        self.assertEqual([row["name"] for row in rows], ["a", "REL1158A"])
        accession = rows[1]
        self.assertEqual(accession["accession"], "SAMN2")
        self.assertEqual(accession["alias"], "REL1158A")
        self.assertEqual([run["accession"] for run in accession["runs"]], ["SRR2", "SRR3"])
        self.assertEqual(accession["files"],
                         ["SRR2_1.fastq.gz", "SRR2_2.fastq.gz", "SRR3.fastq.gz"])
        self.assertEqual(accession["bytes"],
                         sum(len(self.served[name]) for name in accession["files"]))
        self.assertFalse(accession["placed"])
        self.assertNotIn("accession", rows[0])

    def test_the_preview_refuses_what_the_launch_would(self):
        with no_delay(), self._ena():
            response = self._preview("SRR999")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["field"], "accessions")

    def test_the_preview_and_the_launch_agree_on_names(self):
        rows = [self._run("SRR1", "SAMN1", "3-30000-1-1"), self._run("SRR2", "SAMN2", "x/y")]
        with no_delay(), self._ena(SRR1=[rows[0]], SRR2=[rows[1]]):
            previewed = [row["name"] for row in self._preview("SRR1 SRR2").json()["samples"]]
            self._launch("SRR1 SRR2", input_mode="read_names")
        self.assertEqual(previewed, ["3-30000-1-1", "SRR2"])
        self.assertEqual(sorted(BreseqRun.objects.values_list("sample_name", flat=True)),
                         sorted(previewed))

    def test_the_run_list_says_which_accessions_a_run_fetched(self):
        with no_delay(), self._ena(SAMN2=[self._run("SRR2", "SAMN2"), self._run("SRR3", "SAMN2")]):
            self._launch("SAMN2")
        rows = self.client.get("/breseq/runs?experiment_id=%s" % self.experiment.id).json()
        self.assertEqual(rows["runs"][0]["accessions"],
                         [{"typed": "SAMN2", "kind": "sample", "runs": ["SRR2", "SRR3"]}])
