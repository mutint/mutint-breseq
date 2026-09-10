"""The command line, and what counts as usable output.

Pure functions, so no database and no breseq. These are the two rules a person editing this
plugin is most likely to change by accident: what reaches breseq's argv, and what has to be on
disk before the importer is called.
"""

import os
import shutil
import tempfile
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from mutint_breseq import pairing, runner


class ArgvTestCase(SimpleTestCase):
    def build(self, arguments="", reads=("r1.fastq",), processors=None,
              polymorphism=False, coverage_limit=None):
        return runner.build_argv("/bin/breseq", "/out/s1", "/ref.gff3", arguments,
                                 list(reads), processors=processors,
                                 polymorphism=polymorphism, coverage_limit=coverage_limit)

    def test_the_reference_and_output_are_supplied(self):
        argv = self.build()
        self.assertEqual(argv[0], "/bin/breseq")
        self.assertIn("-o", argv)
        self.assertEqual(argv[argv.index("-o") + 1], "/out/s1")
        self.assertIn("-r", argv)
        self.assertEqual(argv[argv.index("-r") + 1], "/ref.gff3")

    def test_reads_come_last_and_in_order(self):
        # They are positional, so anything after them would be read as another read file.
        argv = self.build(arguments="-p", reads=("a.fastq", "b.fastq"))
        self.assertEqual(argv[-2:], ["a.fastq", "b.fastq"])

    def test_typed_arguments_are_split_not_shelled(self):
        argv = self.build(arguments="-p --polymorphism-minimum-variant-coverage 4")
        self.assertIn("--polymorphism-minimum-variant-coverage", argv)
        self.assertIn("4", argv)
        # One token per word, not one string containing spaces.
        self.assertNotIn("-p --polymorphism-minimum-variant-coverage 4", argv)

    def test_quoted_arguments_survive_as_one_token(self):
        argv = self.build(arguments='--name "two words"')
        self.assertIn("two words", argv)

    def test_unbalanced_quotes_are_a_value_error(self):
        # Caught at launch so it is a message beside the box, not a run that fails hours later.
        with self.assertRaises(ValueError):
            runner.split_arguments('--name "unterminated')

    def test_processors_are_injected_when_asked(self):
        argv = self.build(processors=8)
        self.assertEqual(argv[argv.index("-j") + 1], "8")

    def test_the_population_checkbox_puts_p_on_the_command_line(self):
        argv = self.build(polymorphism=True)
        self.assertIn("-p", argv)
        # Before the read files, which are positional and would otherwise swallow it.
        self.assertLess(argv.index("-p"), argv.index("r1.fastq"))

    def test_nothing_is_added_when_the_box_is_not_ticked(self):
        self.assertNotIn("-p", self.build())

    def test_a_typed_short_flag_is_not_repeated(self):
        argv = self.build(arguments="-p", polymorphism=True)
        self.assertEqual(argv.count("-p"), 1)

    def test_a_typed_long_flag_suppresses_the_injection(self):
        # The whole reason POLYMORPHISM_FLAGS carries both spellings: breseq would take either
        # happily, and a command line saying it twice reads as the page ignoring the box.
        argv = self.build(arguments="--polymorphism-prediction", polymorphism=True)
        self.assertNotIn("-p", argv)
        self.assertIn("--polymorphism-prediction", argv)

    def test_a_coverage_limit_is_passed_as_l(self):
        argv = self.build(coverage_limit=80)
        self.assertEqual(argv[argv.index("-l") + 1], "80")

    def test_a_whole_number_does_not_reach_breseq_as_a_float(self):
        # The column is a float, and `str(80.0)` would put `80.0` on every command line and
        # in every log.
        self.assertEqual(runner.format_coverage_limit(80.0), "80")
        self.assertEqual(runner.format_coverage_limit(62.5), "62.5")

    def test_a_large_limit_is_not_written_in_exponent_notation(self):
        # `%g` would hand breseq `1e+06`.
        self.assertEqual(runner.format_coverage_limit(1000000), "1000000")

    def test_no_limit_is_the_default_and_adds_nothing(self):
        # None means every read, which is breseq's own default -- so there is nothing to say.
        self.assertNotIn("-l", self.build())

    def test_a_typed_limit_wins_in_either_spelling(self):
        # Unlike -p this one takes a value, so a second would be a real ambiguity.
        short = self.build(arguments="-l 40", coverage_limit=80)
        self.assertEqual(short.count("-l"), 1)
        self.assertEqual(short[short.index("-l") + 1], "40")

        long = self.build(arguments="--limit-fold-coverage 40", coverage_limit=80)
        self.assertNotIn("-l", long)

    def test_a_typed_processor_count_wins(self):
        # breseq takes the last -j silently, so injecting ours as well would make the box
        # look like it did nothing.
        argv = self.build(arguments="-j 2", processors=8)
        self.assertEqual(argv.count("-j"), 1)
        self.assertEqual(argv[argv.index("-j") + 1], "2")

    def test_the_long_processor_flag_counts_too(self):
        argv = self.build(arguments="--num-processors 3", processors=8)
        self.assertNotIn("-j", argv)

    def test_nothing_is_injected_without_a_count(self):
        self.assertNotIn("-j", self.build(processors=None))


class DryRunArgvTestCase(TestCase):
    """The preflight has to be the real command line plus one flag, or it checks the wrong
    thing. One builder is what guarantees that; this is what says so."""

    def test_it_is_the_real_argv_plus_the_flag(self):
        common = dict(output_dir="/out", reference="/ref.gff3",
                      arguments="-p --polymorphism-minimum-variant-coverage 4",
                      reads=["/r1.fastq", "/r2.fastq"], processors=8)
        real = runner.build_argv("breseq", **common)
        dry = runner.build_argv("breseq", dry_run=True, **common)

        self.assertEqual([arg for arg in dry if arg != runner.DRY_RUN_FLAG], real)
        self.assertIn(runner.DRY_RUN_FLAG, dry)

    def test_it_is_absent_by_default(self):
        argv = runner.build_argv("breseq", "/out", "/ref.gff3", "", ["/r1.fastq"])
        self.assertNotIn(runner.DRY_RUN_FLAG, argv)


class RefusalTestCase(TestCase):
    """What of a failed dry run reaches a person. Both shapes are breseq's, measured."""

    def test_an_unknown_option_is_the_last_line(self):
        # Real breseq prints its whole help first -- 45 lines of it -- and the reason last.
        output = ("  -j,--num-processors  Number of processors\n"
                  "  -p,--polymorphism-prediction  The sample is not clonal\n"
                  "\n"
                  "Unknown command argument option: no-such-flag\n")

        self.assertEqual("Unknown command argument option: no-such-flag",
                         runner.refusal_from(output))

    def test_a_bad_path_keeps_the_error_lines_and_the_summary(self):
        output = ("Checking input and output paths\n"
                  "---> -o/--output :: /out [will be created under /]\n"
                  "---> ERROR Input file for Read file does not exist: /nope.fastq\n"
                  "\n"
                  "Could not validate the file and folder arguments listed above.\n")

        self.assertEqual(
            "---> ERROR Input file for Read file does not exist: /nope.fastq\n"
            "Could not validate the file and folder arguments listed above.",
            runner.refusal_from(output))

    def test_nothing_at_all_is_empty_rather_than_an_exception(self):
        self.assertEqual("", runner.refusal_from(""))
        self.assertEqual("", runner.refusal_from(None))


class FastpArgvTestCase(SimpleTestCase):
    def test_a_pair_gets_both_mates_and_the_paired_adapter_flag(self):
        read_set = pairing.ReadFileSet("s_RX", ["/reads/s_R1.fastq.gz", "/reads/s_R2.fastq.gz"])
        argv = runner.build_fastp_argv("/bin/fastp", read_set, "/run/trimmed", threads=4)
        self.assertEqual("/bin/fastp", argv[0])
        # brefito's options, and no others: adapter trimming with quality filtering off.
        self.assertIn("--disable_quality_filtering", argv)
        self.assertIn("--detect_adapter_for_pe", argv)
        self.assertEqual("4", argv[argv.index("--thread") + 1])
        self.assertEqual("/reads/s_R1.fastq.gz", argv[argv.index("-i") + 1])
        self.assertEqual("/reads/s_R2.fastq.gz", argv[argv.index("-I") + 1])
        self.assertEqual("/run/trimmed/s_R1.fastq.gz", argv[argv.index("-o") + 1])
        self.assertEqual("/run/trimmed/s_R2.fastq.gz", argv[argv.index("-O") + 1])
        self.assertEqual("/run/trimmed/s_RX.fastp.json", argv[argv.index("-j") + 1])
        self.assertEqual("/run/trimmed/s_RX.fastp.html", argv[argv.index("-h") + 1])

    def test_a_single_file_gets_neither(self):
        argv = runner.build_fastp_argv(
            "/bin/fastp", pairing.ReadFileSet("s", ["/reads/s.fastq"]), "/run/trimmed")
        self.assertNotIn("-I", argv)
        self.assertNotIn("--detect_adapter_for_pe", argv)
        self.assertNotIn("--thread", argv)

    def test_nothing_that_would_filter_reads_is_passed(self):
        argv = runner.build_fastp_argv(
            "/bin/fastp", pairing.ReadFileSet("s", ["/reads/s.fastq"]), "/run/trimmed")
        for flag in ("--length_required", "--cut_right", "--cut_front", "--cut_tail",
                     "--dedup", "-q", "-u", "-n"):
            self.assertNotIn(flag, argv)

    def test_threads_are_capped_where_fastp_caps_them(self):
        with mock.patch.object(runner, "default_processors", return_value=64):
            self.assertEqual(16, runner.fastp_threads())
        with mock.patch.object(runner, "default_processors", return_value=None):
            self.assertIsNone(runner.fastp_threads())


class ToolEnvironmentTestCase(TestCase):
    def test_the_tools_bin_leads_the_path(self):
        # breseq shells out to bowtie2, samtools and gnuplot by bare name and exits 0 when it
        # cannot find them, so this is what stands between a run and a silent no-op.
        with override_settings(MUTINT_TOOLS_DIR="/managed/tools"):
            env = runner.tool_environment({"PATH": "/usr/bin"})
        self.assertEqual(env["PATH"], os.path.join("/managed/tools", "bin") + os.pathsep + "/usr/bin")

    def test_an_unmanaged_environment_is_left_alone(self):
        with override_settings(MUTINT_TOOLS_DIR=None):
            env = runner.tool_environment({"PATH": "/usr/bin"})
        self.assertEqual(env["PATH"], "/usr/bin")


class CheckOutputTestCase(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, relative):
        path = os.path.join(self.dir, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("x")

    def test_a_complete_output_passes(self):
        for relative in runner.REQUIRED_OUTPUT:
            self._write(relative)
        runner.check_output(self.dir)          # does not raise

    def test_a_missing_file_is_named(self):
        # The message matters: without it the importer reports "has no data/output.gd", which
        # reads as the plugin having looked in the wrong place rather than as breseq stopping.
        for relative in runner.REQUIRED_OUTPUT[1:]:
            self._write(relative)
        with self.assertRaises(runner.BreseqUnusable) as caught:
            runner.check_output(self.dir)
        self.assertIn("output.gd", str(caught.exception))

    def test_an_empty_directory_is_refused(self):
        with self.assertRaises(runner.BreseqUnusable):
            runner.check_output(self.dir)


class CleanupTestCase(SimpleTestCase):
    def setUp(self):
        self.run_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.run_dir, True)
        self.output_dir = os.path.join(self.run_dir, "s1")
        self.report_dir = os.path.join(self.run_dir, "report")

        os.makedirs(os.path.join(self.output_dir, "data"))
        os.makedirs(os.path.join(self.output_dir, "output", "evidence"))
        os.makedirs(os.path.join(self.run_dir, "reads"))
        os.makedirs(os.path.join(self.run_dir, "trimmed"))
        for path in (os.path.join(self.output_dir, "data", "reference.bam"),
                     os.path.join(self.output_dir, "output", "index.html"),
                     os.path.join(self.output_dir, "output", "evidence", "e.html"),
                     os.path.join(self.run_dir, "reads", "r1.fastq"),
                     os.path.join(self.run_dir, "trimmed", "r1.fastq")):
            with open(path, "w") as handle:
                handle.write("x")

    def test_everything_is_thrown_away(self):
        """The importer has already kept what matters, under the sample.

        This used to move `output/` aside into a `report/` of its own and return whether it
        had -- two homes for the same bytes, and the wrong one: a report describes the sample
        that was produced, and the sample outlives the run row. mutint-core stores it now.
        """
        runner.cleanup_after_import(self.run_dir, self.output_dir)

        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "reads")))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "trimmed")))
        self.assertFalse(os.path.exists(self.output_dir))

    def test_a_run_with_no_output_directory_is_fine(self):
        shutil.rmtree(self.output_dir)
        runner.cleanup_after_import(self.run_dir, self.output_dir)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "reads")))
        self.assertFalse(os.path.exists(self.output_dir))
