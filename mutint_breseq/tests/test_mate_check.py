"""Whether two files that pair by name really are mates.

Pure, so no database and no tools. What is being pinned is a rule neither breseq nor fastp
applies: both accept an unequal pair, truncate to the shorter and exit 0.
"""

import os
import shutil
import tempfile

from django.test import SimpleTestCase

from mutint_breseq import mate_check
from mutint_breseq.tests import fastq_fixture


class CountingTestCase(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _path(self, name):
        return os.path.join(self.dir, name)

    def test_records_are_counted_plain_and_gzipped(self):
        for name in ("a.fastq", "a.fastq.gz"):
            with self.subTest(name=name):
                count = mate_check.count_records(fastq_fixture.write(self._path(name), count=7))
                self.assertEqual(count.records, 7)
                self.assertTrue(count.whole)

    def test_a_record_missing_a_line_is_not_whole(self):
        path = fastq_fixture.write(self._path("cut.fastq"), count=3)
        with open(path) as handle:
            lines = handle.read().splitlines(True)
        with open(path, "w") as handle:
            handle.write("".join(lines[:-1]))   # 11 lines: the last record has no quality line

        count = mate_check.count_records(path)
        self.assertFalse(count.whole)

    def test_truncation_inside_a_line_is_not_detected(self):
        """The limit of counting lines, stated rather than left to be discovered.

        A download cut off mid-quality-line still leaves four lines in that record, so the
        count divides evenly and nothing here objects. What it does catch is a record missing a
        line outright, which is what a cut between records looks like -- and the counts of the
        two mates disagreeing, which is the case this module exists for.
        """
        path = fastq_fixture.write(self._path("mid.fastq"), count=3)
        with open(path) as handle:
            text = handle.read()
        with open(path, "w") as handle:
            handle.write(text[:-20])

        self.assertTrue(mate_check.count_records(path).whole)

    def test_a_last_line_with_no_newline_still_counts(self):
        path = self._path("nonewline.fastq")
        with open(path, "w") as handle:
            handle.write("@r1\nAC\n+\nII")     # four lines, no trailing newline
        count = mate_check.count_records(path)
        self.assertEqual(count.records, 1)
        self.assertTrue(count.whole)

    def test_an_empty_file_is_nothing_rather_than_broken(self):
        path = self._path("empty.fastq")
        open(path, "w").close()
        self.assertEqual(mate_check.count_records(path), (0, True))

    def test_a_file_that_cannot_be_read_answers_none(self):
        """None means *say nothing*: fastp will meet it and describe it in its own words."""
        self.assertIsNone(mate_check.count_records(self._path("missing.fastq")))
        broken = self._path("broken.fastq.gz")
        with open(broken, "w") as handle:
            handle.write("not gzip")
        self.assertIsNone(mate_check.count_records(broken))


class ReadIdentifierTestCase(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _with_header(self, name, header):
        path = os.path.join(self.dir, name)
        with open(path, "w") as handle:
            handle.write("%s\nACGT\n+\nIIII\n" % header)
        return path

    def test_the_three_conventions_all_reduce_to_the_same_identifier(self):
        for header, expected in (
                ("@read1/1", "read1"),
                ("@read1/2", "read1"),
                ("@A00123:45:HXXX:1:1101:1000:1000 1:N:0:ACGT",
                 "A00123:45:HXXX:1:1101:1000:1000"),
                ("@A00123:45:HXXX:1:1101:1000:1000 2:N:0:ACGT",
                 "A00123:45:HXXX:1:1101:1000:1000"),
                ("@SRR37077254.1 1 length=100", "SRR37077254.1")):
            with self.subTest(header=header):
                path = self._with_header("h.fastq", header)
                self.assertEqual(mate_check.first_read_id(path), expected)

    def test_something_that_is_not_a_header_answers_none(self):
        self.assertIsNone(mate_check.first_read_id(self._with_header("x.fastq", "ACGT")))
        self.assertIsNone(mate_check.first_read_id(os.path.join(self.dir, "missing.fastq")))


class MismatchTestCase(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_a_real_pair_has_nothing_to_say(self):
        first, second = fastq_fixture.write_pair(self.dir, counts=(50, 50))
        self.assertIsNone(mate_check.mismatch_reason(first, second))

    def test_a_gzipped_real_pair_has_nothing_to_say(self):
        first, second = fastq_fixture.write_pair(self.dir, counts=(4, 4), suffix=".fastq.gz")
        self.assertIsNone(mate_check.mismatch_reason(first, second))

    def test_unequal_counts_are_named_with_both_numbers(self):
        """The case measured against both tools: each truncates to the shorter and exits 0."""
        first, second = fastq_fixture.write_pair(self.dir, counts=(1200, 1000))

        reason = mate_check.mismatch_reason(first, second)

        self.assertIn("different numbers of reads", reason)
        self.assertIn("1,200", reason)
        self.assertIn("1,000", reason)

    def test_equal_counts_are_not_proof_and_the_reads_are_asked(self):
        """Two different samples' R1 can pair by name and match in size."""
        first = fastq_fixture.write(
            os.path.join(self.dir, "x_R1.fastq"), count=5, name="SRR37077254.")
        second = fastq_fixture.write(
            os.path.join(self.dir, "x_R2.fastq"), count=5, name="SRR37099999.")

        reason = mate_check.mismatch_reason(first, second)

        self.assertIn("start with different reads", reason)

    def test_a_truncated_file_is_named(self):
        first, second = fastq_fixture.write_pair(self.dir, counts=(5, 5))
        with open(second, "a") as handle:
            handle.write("@r6\nACGT\n")        # half a record

        reason = mate_check.mismatch_reason(first, second)

        self.assertIn("part-way through a record", reason)
        self.assertIn("s_R2.fastq", reason)

    def test_a_file_that_cannot_be_read_says_so_rather_than_guessing(self):
        first, _ = fastq_fixture.write_pair(self.dir, counts=(5, 5))
        self.assertEqual(mate_check.mismatch_reason(first, os.path.join(self.dir, "gone.fastq")),
                         "one of them could not be read")


class UnpairedNameTestCase(SimpleTestCase):
    def test_the_marker_goes_before_the_suffix_so_the_file_is_still_fastq(self):
        from mutint_breseq import pairing

        for path, expected in (
                ("/r/s_R2.fastq", "/r/s_R2.unpaired.fastq"),
                ("/r/s_R2.fastq.gz", "/r/s_R2.unpaired.fastq.gz"),
                ("/r/s_R2.fq.gz", "/r/s_R2.unpaired.fq.gz"),
                ("/r/s_R2.FASTQ", "/r/s_R2.unpaired.FASTQ")):
            with self.subTest(path=path):
                renamed = mate_check.unpaired_name(path)
                self.assertEqual(renamed, expected)
                self.assertTrue(pairing.is_fastq(renamed))

    def test_the_renamed_file_no_longer_pairs(self):
        """The contract, asserted through breseq's own rule rather than by reading the name."""
        from mutint_breseq import pairing

        first, second = "s_R1.fastq", "s_R2.fastq"
        self.assertEqual(len(pairing.read_file_sets([first, second])[0].files), 2)

        sets = pairing.read_file_sets([first, mate_check.unpaired_name(second)])

        self.assertEqual([len(one.files) for one in sets], [1, 1])
