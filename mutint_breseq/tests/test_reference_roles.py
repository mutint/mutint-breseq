"""Handing breseq several reference files, one per role.

The roles themselves are mutint-core's (`mutint_import.reference_roles`). What is asserted
here is this plugin's half: that the argv carries one flag per group, that the files are
written where this plugin already cleans up, and -- the one that guards everything else --
that an experiment with no roles set produces the command line it always did.
"""

import os
import shutil
import tempfile

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings

from mutint_experiment.models import Experiment, Project
from mutint_import import annotation, reference, reference_store
from mutint_import.reference_roles import (
    ROLE_CONTIG,
    ROLE_JUNCTION_ONLY,
    ROLE_REFERENCE,
    set_roles,
)
from mutint_import.tests import breseq_fixture

from mutint_breseq import runner

SEQUENCE_C = "TTACGGCA" * 12


class ArgvTestCase(SimpleTestCase):
    """Pure: the flags, given the groups."""

    def test_one_plain_reference_is_the_command_line_it_always_was(self):
        """The invariant every existing run and every existing test rests on.

        Spelled out literally rather than compared against a helper, because what it is
        protecting is that nothing moved -- a helper that changed with the code would go on
        agreeing with whatever the code now does.
        """
        argv = runner.build_argv("/bin/breseq", "/out/s1", [("-r", "/ref.gff3")],
                                 "", ["r1.fastq"])
        self.assertEqual(argv[:2], ["/bin/breseq", "--max-evidence-items"])
        self.assertEqual(argv[argv.index("-o"):],
                         ["-o", "/out/s1", "-r", "/ref.gff3", "r1.fastq"])

    def test_each_group_gets_its_own_flag_and_file(self):
        argv = runner.build_argv(
            "/bin/breseq", "/out/s1",
            [("-r", "/ref/reference.gff3"), ("-c", "/ref/contig.gff3"),
             ("-s", "/ref/junction_only.gff3")],
            "", ["r1.fastq"])
        self.assertEqual(argv[argv.index("-o"):],
                         ["-o", "/out/s1",
                          "-r", "/ref/reference.gff3",
                          "-c", "/ref/contig.gff3",
                          "-s", "/ref/junction_only.gff3",
                          "r1.fastq"])

    def test_the_references_still_precede_the_reads(self):
        """They are positional, so a reference file after them would be read as one."""
        argv = runner.build_argv("/bin/breseq", "/out", [("-c", "/a.gff3"), ("-s", "/b.gff3")],
                                 "", ["r1.fastq", "r2.fastq"])
        self.assertEqual(argv[-2:], ["r1.fastq", "r2.fastq"])
        self.assertLess(argv.index("-s"), argv.index("r1.fastq"))

    def test_the_typed_box_still_has_the_last_word(self):
        argv = runner.build_argv("/bin/breseq", "/out", [("-c", "/a.gff3")],
                                 "-j 2", ["r1.fastq"], processors=8)
        self.assertLess(argv.index("-c"), argv.index("-j"))
        self.assertEqual(argv[argv.index("-j") + 1], "2")


class WriteFilesTestCase(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_each_group_is_written_and_named_for_its_role(self):
        written = runner.write_reference_files(
            os.path.join(self.directory, "reference"),
            [(ROLE_CONTIG, "-c", "contig text"),
             (ROLE_JUNCTION_ONLY, "-s", "junction text")])

        self.assertEqual([flag for flag, _path in written], ["-c", "-s"])
        self.assertEqual([os.path.basename(path) for _flag, path in written],
                         ["contig.gff3", "junction_only.gff3"])
        for _flag, path in written:
            self.assertTrue(os.path.isfile(path))
        with open(written[0][1], encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "contig text")


class _Experiment(TestCase):
    SEQUENCES = [("test_ref", breseq_fixture.SEQUENCE_A),
                 ("NODE_1", breseq_fixture.SEQUENCE_B),
                 ("IS150", SEQUENCE_C)]

    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.drop = tempfile.mkdtemp()
        self.scratch = tempfile.mkdtemp()
        for directory in (self.store, self.drop, self.scratch):
            self.addCleanup(shutil.rmtree, directory, True)
        patcher = override_settings(MUTINT_STORE_DIR=self.store)
        patcher.enable()
        self.addCleanup(patcher.disable)
        annotation.clear_cache()
        self.addCleanup(annotation.clear_cache)

        owner = User.objects.create(username="owner")
        project = Project.objects.create(name="P", user=owner)
        self.experiment = Experiment.objects.create(name="e", project=project)

        path = os.path.join(self.drop, "ref.fasta")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(reference.render_fasta(self.SEQUENCES))
        gff3_text, sequences = reference.normalize_reference(path)
        reference_store.establish_or_check(self.experiment, gff3_text, sequences)

        from mutint_common import store
        self.stored = store.experiment_reference_path(self.experiment.id, store.REFERENCE_GFF3)

    def _refresh(self):
        self.experiment.refresh_from_db()
        try:
            del self.experiment.reference
        except AttributeError:
            pass
        return self.experiment


class ReferenceArgumentsTestCase(_Experiment):
    def test_a_uniform_reference_is_the_stored_file_untouched(self):
        """Nothing rendered, nothing written -- and the directory is not even created."""
        set_roles(self.experiment, {"NODE_1": ROLE_REFERENCE})
        directory = os.path.join(self.scratch, "reference")

        references = runner.reference_arguments(self._refresh(), directory, self.stored)

        self.assertEqual(references, [("-r", self.stored)])
        self.assertFalse(os.path.exists(directory))

    def test_a_mixed_reference_is_rendered_into_files(self):
        set_roles(self.experiment, {"IS150": ROLE_JUNCTION_ONLY})
        directory = os.path.join(self.scratch, "reference")

        references = runner.reference_arguments(self._refresh(), directory, self.stored)

        self.assertEqual([flag for flag, _path in references], ["-r", "-c", "-s"])
        for _flag, path in references:
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(path.startswith(directory))

    def test_every_contig_reaches_exactly_one_file(self):
        set_roles(self.experiment, {"IS150": ROLE_JUNCTION_ONLY})
        references = runner.reference_arguments(
            self._refresh(), os.path.join(self.scratch, "reference"), self.stored)

        seen = []
        for _flag, path in references:
            with open(path, encoding="utf-8") as handle:
                seen.extend(line.split("\t")[1] for line in handle
                            if line.startswith("##sequence-region"))
        self.assertEqual(sorted(seen), ["IS150", "NODE_1", "test_ref"])

    def test_the_files_sit_under_the_directory_the_caller_owns(self):
        """For a run that is the run directory, which `post_delete` already removes -- so
        this adds no lifecycle of its own."""
        set_roles(self.experiment, {"IS150": ROLE_JUNCTION_ONLY})
        directory = os.path.join(self.scratch, "run7", "reference")
        references = runner.reference_arguments(self._refresh(), directory, self.stored)
        for _flag, path in references:
            self.assertEqual(os.path.dirname(path), directory)

    def test_an_experiment_with_no_reference_falls_back_to_the_stored_path(self):
        bare = Experiment.objects.create(name="bare", project=self.experiment.project)
        self.assertEqual(
            runner.reference_arguments(bare, os.path.join(self.scratch, "r"), self.stored),
            [("-r", self.stored)])
