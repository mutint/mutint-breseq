"""What the launch endpoint refuses, and why each refusal is here.

Every one of these is a case a browser cannot easily be made to show, and three of them are
the ones that matter most: a locked experiment, a reader, and a sample name that would become
a path. The happy path is `test_run`.
"""

import json
import os
import shutil
import tempfile

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from mutint_common import store
from mutint_experiment.models import Experiment, Project
from mutint_import import reference, reference_store, staging
from mutint_import.tests import breseq_fixture

from mutint_breseq.models import (
    STATUS_IMPORTED, STATUS_RUNNING, BreseqRun,
)


def establish_reference(experiment, sequences=None):
    """Give `experiment` a stored reference matching the shared fixture's sequences."""
    sequences = sequences or [("test_ref", breseq_fixture.SEQUENCE_A)]
    gff3_text = breseq_fixture.gff3_text(sequences)
    directory = tempfile.mkdtemp()
    try:
        path = os.path.join(directory, "ref.gff3")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(gff3_text)
        normalized, parsed = reference.normalize_reference(path)
        return reference_store.establish_or_check(experiment, normalized, parsed)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class LaunchTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="owner", email="o@e.com", is_active=True)
        self.owner.set_password("pw")
        self.owner.save()
        self.client.force_login(self.owner)

        self.store = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.store, True)
        patcher = override_settings(MUTINT_STORE_DIR=self.store)
        patcher.enable()
        self.addCleanup(patcher.disable)

        self.project = Project.objects.create(name="p", user=self.owner)
        from mutint_experiment.views import _create_experiment
        self.experiment = _create_experiment(self.project, "e", self.owner)
        establish_reference(self.experiment)

    # --- helpers ------------------------------------------------------------------------

    def _stage(self, user=None, experiment=None, files=None):
        session = staging.open_session(
            user or self.owner, experiment or self.experiment, "mutint_breseq",
            files or [{"path": "r1.fastq", "size": 4}])
        root = store.ensure_dir(store.staging_dir(session.id))
        with open(os.path.join(root, "r1.fastq"), "w") as handle:
            handle.write("ACGT")
        return session

    def _launch(self, upload_id, sample_name="s1", arguments="", experiment_id=None):
        return self.client.post(
            "/breseq/launch?experiment_id=%s" % (
                self.experiment.id if experiment_id is None else experiment_id),
            data=json.dumps({"upload_id": str(upload_id),
                             "sample_name": sample_name,
                             "arguments": arguments}),
            content_type="application/json")

    # --- the page -----------------------------------------------------------------------

    def test_the_page_renders_and_offers_the_form(self):
        response = self.client.get("/breseq/?experiment_id=%s" % self.experiment.id)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "breseq-sample-name")
        self.assertContains(response, "breseq-arguments")
        # Trimming is offered and on by default.
        self.assertContains(response, 'id="breseq-trim-reads" checked')

    def test_without_a_reference_the_form_is_replaced_by_a_banner(self):
        from mutint_experiment.views import _create_experiment
        bare = _create_experiment(self.project, "bare", self.owner)
        response = self.client.get("/breseq/?experiment_id=%s" % bare.id)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no reference genome")
        # Not merely disabled: a Launch button that always refuses is worse than none.
        self.assertNotContains(response, 'id="breseq-sample-name"')

    def test_a_reader_gets_the_run_list_and_no_form(self):
        reader = User.objects.create(username="reader", email="r@e.com", is_active=True)
        from mutint_experiment.permissions import grant_project_access
        grant_project_access(self.project, reader, "read")
        self.client.force_login(reader)
        response = self.client.get("/breseq/?experiment_id=%s" % self.experiment.id)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="breseq-sample-name"')

    # --- refusals -----------------------------------------------------------------------

    def test_a_reader_cannot_launch(self):
        session = self._stage()
        reader = User.objects.create(username="reader", email="r@e.com", is_active=True)
        from mutint_experiment.permissions import grant_project_access
        grant_project_access(self.project, reader, "read")
        self.client.force_login(reader)
        # 403 from session_for -- the session is somebody else's -- or from the permission
        # check. Either way nothing is launched, which is what this asserts.
        self.assertEqual(self._launch(session.id).status_code, 403)
        self.assertEqual(BreseqRun.objects.count(), 0)

    def test_a_locked_experiment_refuses(self):
        # can_edit_experiment, not can_edit_project. A lock outranks every role, and what this
        # endpoint eventually writes is a sample everybody sees.
        session = self._stage()
        self.experiment.lock(self.owner)
        response = self._launch(session.id)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BreseqRun.objects.count(), 0)

    def test_an_experiment_with_no_reference_refuses(self):
        from mutint_experiment.views import _create_experiment
        bare = _create_experiment(self.project, "bare", self.owner)
        session = self._stage(experiment=bare)
        response = self._launch(session.id, experiment_id=bare.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("reference genome", response.json()["error"])

    def test_a_sample_name_that_would_be_a_path_is_refused(self):
        # This string becomes a directory name under the store. `store.component_dir` contains
        # the run directory, but the sample folder inside it is named from here.
        for bad in ("../../etc", "a/b", "", " ", ".hidden", "a b", "s1;rm -rf /"):
            session = self._stage()
            response = self._launch(session.id, sample_name=bad)
            self.assertEqual(response.status_code, 400, "accepted %r" % (bad,))
        self.assertEqual(BreseqRun.objects.count(), 0)

    def test_the_two_parseable_name_shapes_are_accepted(self):
        # Both are what sample_names.parse_sample_identity reads a coordinate out of, so a
        # narrower pattern here would quietly stop samples landing on their ALE.
        from mutint_breseq.views import SAMPLE_NAME_RE
        for good in ("3-30000-1-1", "Ara-2_500gen_763A", "s1", "A.1+2"):
            self.assertTrue(SAMPLE_NAME_RE.match(good), "refused %r" % (good,))

    def test_unbalanced_quotes_in_the_arguments_are_refused(self):
        session = self._stage()
        response = self._launch(session.id, arguments='--name "unterminated')
        self.assertEqual(response.status_code, 400)
        self.assertIn("could not be read", response.json()["error"])

    def test_another_experiments_upload_is_refused(self):
        # Two ids arrive from the client and nothing else pairs them.
        from mutint_experiment.views import _create_experiment
        other = _create_experiment(self.project, "other", self.owner)
        establish_reference(other)
        session = self._stage(experiment=other)
        response = self._launch(session.id)
        self.assertEqual(response.status_code, 409)

    def test_an_ordinary_import_session_is_refused(self):
        # A session opened for the import registry is not this plugin's to claim.
        response = self.client.post(
            "/import/uploads/",
            data=json.dumps({"experiment_id": self.experiment.id,
                             "import_type": "genomediff",
                             "files": [{"path": "a.gd", "size": 1}]}),
            content_type="application/json")
        self.assertEqual(self._launch(response.json()["upload_id"]).status_code, 409)

    def test_an_unknown_upload_is_a_404(self):
        self.assertEqual(
            self._launch("00000000-0000-0000-0000-000000000000").status_code, 404)

    def test_an_empty_drop_is_refused_and_leaves_no_run(self):
        session = staging.open_session(
            self.owner, self.experiment, "mutint_breseq", [{"path": "r1.fastq", "size": 4}])
        store.ensure_dir(store.staging_dir(session.id))          # declared but never sent
        response = self._launch(session.id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(BreseqRun.objects.count(), 0)

    def test_launch_must_be_a_post(self):
        self.assertEqual(
            self.client.get("/breseq/launch?experiment_id=%s" % self.experiment.id).status_code,
            405)


class RunListTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="owner", email="o@e.com", is_active=True)
        self.client.force_login(self.owner)
        self.store = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.store, True)
        patcher = override_settings(MUTINT_STORE_DIR=self.store)
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.project = Project.objects.create(name="p", user=self.owner)
        from mutint_experiment.views import _create_experiment
        self.experiment = _create_experiment(self.project, "e", self.owner)

    def test_the_list_is_scoped_to_the_experiment(self):
        from mutint_experiment.views import _create_experiment
        other = _create_experiment(self.project, "other", self.owner)
        BreseqRun.objects.create(experiment=self.experiment, sample_name="mine")
        BreseqRun.objects.create(experiment=other, sample_name="theirs")

        body = self.client.get("/breseq/runs?experiment_id=%s" % self.experiment.id).json()
        self.assertEqual([row["sample_name"] for row in body["runs"]], ["mine"])

    def test_deleting_a_run_removes_its_directory(self):
        run = BreseqRun.objects.create(experiment=self.experiment, sample_name="s1",
                                       status=STATUS_IMPORTED)
        directory = store.ensure_dir(run.directory())
        with open(os.path.join(directory, "kept"), "w") as handle:
            handle.write("x")

        response = self.client.post("/breseq/run/%d/delete" % run.pk)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(BreseqRun.objects.filter(pk=run.pk).exists())
        # The post_delete receiver is the whole lifecycle of that directory -- core reaps
        # nothing under components/.
        self.assertFalse(os.path.exists(directory))

    def test_an_unfinished_run_cannot_be_deleted(self):
        # The post_delete receiver rmtrees the run directory, which for a running run pulls
        # the reads out from under the live subprocess -- breseq then fails minutes later with
        # an error naming neither cause nor culprit. Cancelling is the way to stop it.
        run = BreseqRun.objects.create(experiment=self.experiment, sample_name="s1",
                                       status=STATUS_RUNNING)
        response = self.client.post("/breseq/run/%d/delete" % run.pk)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Cancel it", response.json()["error"])
        self.assertTrue(BreseqRun.objects.filter(pk=run.pk).exists())

    def test_deleting_the_experiment_takes_the_run_directory_with_it(self):
        # Deleting the *experiment* is not gated on the run being finished: cascade is not the
        # delete button, and an experiment being removed is a decision about the whole dataset.
        run = BreseqRun.objects.create(experiment=self.experiment, sample_name="s1")
        directory = store.ensure_dir(run.directory())
        self.experiment.delete()
        self.assertFalse(os.path.exists(directory))

    def test_a_reader_cannot_delete_a_run(self):
        run = BreseqRun.objects.create(experiment=self.experiment, sample_name="s1",
                                       status=STATUS_IMPORTED)
        reader = User.objects.create(username="reader", email="r@e.com", is_active=True)
        from mutint_experiment.permissions import grant_project_access
        grant_project_access(self.project, reader, "read")
        self.client.force_login(reader)
        self.assertEqual(
            self.client.post("/breseq/run/%d/delete" % run.pk).status_code, 403)
        self.assertTrue(BreseqRun.objects.filter(pk=run.pk).exists())
