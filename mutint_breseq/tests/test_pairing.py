"""breseq's pairing rule, reproduced -- and what fastp should be kept away from.

Pure. Every case here is one breseq itself decides in `cReadFileSets::Init`, and the point of
the file is that this plugin decides the same way, because fastp trims mates together and
breseq then has to see the same mates in the trimmed files.
"""

import gzip
import os
import shutil
import tempfile

from django.test import SimpleTestCase

from mutint_breseq import pairing, runner


def names(sets):
    return [(s.name, [os.path.basename(f) for f in s.files]) for s in sets]


class BaseNameTestCase(SimpleTestCase):
    def test_directory_gz_and_fastq_are_stripped_in_that_order(self):
        self.assertEqual("s_R1", pairing.base_name("/reads/s_R1.fastq.gz"))
        self.assertEqual("s_R1", pairing.base_name("s_R1.fastq"))

    def test_fq_is_not_stripped_because_breseq_does_not(self):
        # Harmless: both mates carry it, so they still differ in exactly one position.
        self.assertEqual("s_R1.fq", pairing.base_name("s_R1.fq.gz"))


class PairingTestCase(SimpleTestCase):
    def test_mates_pair_with_r1_first_whatever_the_order_given(self):
        sets = pairing.read_file_sets(["/r/s_R2.fastq", "/r/s_R1.fastq"])
        self.assertEqual([("s_RX", ["s_R1.fastq", "s_R2.fastq"])], names(sets))

    def test_underscore_digit_and_gz_pair_too(self):
        sets = pairing.read_file_sets(["a_1.fastq.gz", "a_2.fastq.gz"])
        self.assertEqual([("a_X", ["a_1.fastq.gz", "a_2.fastq.gz"])], names(sets))

    def test_fq_residue_still_pairs(self):
        sets = pairing.read_file_sets(["a_1.fq.gz", "a_2.fq.gz"])
        self.assertEqual([("a_X.fq", ["a_1.fq.gz", "a_2.fq.gz"])], names(sets))

    def test_a_flattened_nested_drop_pairs_as_breseq_would(self):
        # views._take_reads turns L1/r.fastq into L1__r.fastq. breseq sees exactly those names
        # and pairs them -- one position, 1 against 2 -- so this does the same, deliberately.
        sets = pairing.read_file_sets(["L1__r.fastq", "L2__r.fastq"])
        self.assertEqual([("LX__r", ["L1__r.fastq", "L2__r.fastq"])], names(sets))

    def test_a_file_with_two_possible_mates_is_left_unpaired(self):
        # breseq warns "could be paired with multiple other read files" and treats it as
        # unpaired; the others then find their partner already spoken for, or absent.
        sets = pairing.read_file_sets(["a1_R1.fastq", "a1_R2.fastq", "a2_R1.fastq"])
        self.assertEqual([("a1_R1", ["a1_R1.fastq"]),
                          ("a1_R2", ["a1_R2.fastq"]),
                          ("a2_R1", ["a2_R1.fastq"])], names(sets))

    def test_a_lone_file_is_its_own_set(self):
        self.assertEqual([("s", ["s.fastq"])], names(pairing.read_file_sets(["s.fastq"])))

    def test_duplicate_base_names_are_renamed_before_pairing(self):
        # Two lanes' r.fastq: the second becomes r_1, and r and r_1 differ in length, so they
        # do not pair. Exactly breseq's behaviour, including the order of the two steps.
        sets = pairing.read_file_sets(["x/r.fastq", "y/r.fastq"])
        self.assertEqual([("r", ["r.fastq"]), ("r_1", ["r.fastq"])], names(sets))

    def test_a_repeated_path_is_used_once(self):
        sets = pairing.read_file_sets(["s_R1.fastq", "s_R1.fastq", "s_R2.fastq"])
        self.assertEqual([("s_RX", ["s_R1.fastq", "s_R2.fastq"])], names(sets))

    def test_no_paired_mapping_means_every_file_alone(self):
        sets = pairing.read_file_sets(["s_R1.fastq", "s_R2.fastq"], paired=False)
        self.assertEqual([("s_R1", ["s_R1.fastq"]), ("s_R2", ["s_R2.fastq"])], names(sets))


class WhatFastpSeesTestCase(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _fastq(self, name, lengths, gz=False):
        path = os.path.join(self.dir, name)
        opener = gzip.open if gz else open
        with opener(path, "wt") as handle:
            for i, length in enumerate(lengths):
                handle.write("@r%d\n%s\n+\n%s\n" % (i, "A" * length, "I" * length))
        return path

    def test_fastq_is_recognized_by_suffix_in_any_case(self):
        for name in ("a.fastq", "a.fq", "a.FASTQ.GZ", "a.fq.gz"):
            self.assertTrue(pairing.is_fastq(name), name)
        for name in ("a.sam", "a.bam", "a.fasta", "a.txt"):
            self.assertFalse(pairing.is_fastq(name), name)

    def test_short_reads_are_not_long(self):
        self.assertFalse(pairing.looks_long_read(self._fastq("s.fastq", [150] * 5)))

    def test_one_read_at_breseqs_trigger_length_is_long(self):
        # 1000 is breseq's own --long-read-trigger-length default, so the two agree about
        # which files hold long reads.
        self.assertTrue(pairing.looks_long_read(
            self._fastq("n.fastq.gz", [300, 1000, 200], gz=True)))

    def test_only_the_first_records_are_sampled(self):
        path = self._fastq("late.fastq", [100] * 200 + [5000])
        self.assertFalse(pairing.looks_long_read(path, sample=200))
        self.assertTrue(pairing.looks_long_read(path, sample=201))

    def test_an_unreadable_file_is_not_called_long(self):
        # fastp then meets it and says what is wrong, which beats guessing here.
        self.assertFalse(pairing.looks_long_read(os.path.join(self.dir, "missing.fastq")))
        broken = os.path.join(self.dir, "broken.fastq.gz")
        with open(broken, "w") as handle:
            handle.write("not gzip")
        self.assertFalse(pairing.looks_long_read(broken))

    def test_the_plan_trims_pairs_and_singles_and_skips_the_rest(self):
        r1 = self._fastq("s_R1.fastq", [150])
        r2 = self._fastq("s_R2.fastq", [150])
        lone = self._fastq("lane3.fastq.gz", [150], gz=True)
        nano = self._fastq("ont.fastq", [4000])
        sam = os.path.join(self.dir, "aligned.sam")
        open(sam, "w").close()

        plans = runner.plan_trimming([r1, r2, lone, nano, sam])
        by_name = {plan.read_set.name: plan for plan in plans}
        self.assertTrue(by_name["s_RX"].trim)
        self.assertEqual([r1, r2], by_name["s_RX"].read_set.files)
        self.assertTrue(by_name["lane3"].trim)
        self.assertFalse(by_name["ont"].trim)
        self.assertIn("long reads", by_name["ont"].reason)
        self.assertFalse(by_name["aligned.sam"].trim)
        self.assertIn("not a FASTQ", by_name["aligned.sam"].reason)

    def test_a_pair_with_one_long_read_file_is_left_whole(self):
        # Per set, not per file: half a pair trimmed would be a pair no longer.
        r1 = self._fastq("p_1.fastq", [150])
        r2 = self._fastq("p_2.fastq", [2000])
        plans = runner.plan_trimming([r1, r2])
        self.assertEqual(1, len(plans))
        self.assertFalse(plans[0].trim)
