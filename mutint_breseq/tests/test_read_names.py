"""What a drop of read files is read as: which files are one sample, and what it is called.

Pure, so no database and no breseq. The table below is the same one the user documentation
carries, which is the point of writing it as a table: the parser and the prose cannot drift.
"""

from django.test import SimpleTestCase

from mutint_breseq import read_names
from mutint_import.sample_names import parse_sample_identity, sample_label


#: `(dropped files, sample name, coordinate or None)`. The coordinate is what mutint-core's own
#: parser reads back out of the derived name -- asserted here rather than assumed, because the
#: whole design is that this module strips and core's parser places.
CASES = [
    (["Ara-2_500gen_763A_R1.fastq.gz", "Ara-2_500gen_763A_R2.fastq.gz"],
     "Ara-2_500gen_763A", ("Ara-2", 500, "763A")),
    (["pop3_day7_clone2_R1.fq.gz", "pop3_day7_clone2_R2.fq.gz"],
     "pop3_day7_clone2", ("pop3", 7, "clone2")),
    (["3-30000-1-1_R1.fastq.gz", "3-30000-1-1_R2.fastq.gz"],
     "3-30000-1-1", ("3", 30000, "1-1")),
    (["S12_L001_R1_001.fastq.gz", "S12_L001_R2_001.fastq.gz"],
     "S12", None),
    # Mates named the breseq way rather than the Illumina way.
    (["lane_1.fq", "lane_2.fq"], "lane", None),
    # One file, no mate: nothing is stripped but the extension.
    (["Ara-2_500gen_763A.fastq.gz"], "Ara-2_500gen_763A", ("Ara-2", 500, "763A")),
    # Mates separated by a period rather than an underscore -- an SRA accession, as downloaded.
    (["SRR37077254.R1.fastq.gz", "SRR37077254.R2.fastq.gz"], "SRR37077254", None),
    # The same thing single-ended, which is the case with no mate to compare against.
    (["SRR37077254.R1.fastq.gz"], "SRR37077254", None),
]


class DerivationTestCase(SimpleTestCase):
    def test_the_documented_cases(self):
        for files, expected_name, expected_coordinate in CASES:
            with self.subTest(files=files):
                derived = read_names.derive_samples(files)
                self.assertEqual(len(derived), 1, "expected one sample")
                self.assertEqual(derived[0].name, expected_name)
                self.assertEqual(sorted(derived[0].files), sorted(files))

                identity = parse_sample_identity(derived[0].name)
                if expected_coordinate is None:
                    self.assertIsNone(identity, "unexpectedly placed")
                else:
                    self.assertEqual(
                        (identity.population, identity.time_point,
                         sample_label(identity.name, identity.replicate)),
                        expected_coordinate)

    def test_several_samples_come_back_in_the_order_they_appear(self):
        derived = read_names.derive_samples([
            "b_500gen_1_R1.fastq", "b_500gen_1_R2.fastq",
            "a_500gen_1_R1.fastq", "a_500gen_1_R2.fastq"])

        self.assertEqual([sample.name for sample in derived],
                         ["b_500gen_1", "a_500gen_1"])

    def test_two_lanes_of_one_library_are_one_sample(self):
        """breseq's pairing cannot see this and is not wrong to.

        With both lanes present, `S12_L001_R1_001` can be paired with its own R2 *or* with
        `S12_L002_R1_001` -- two ways, which breseq calls ambiguous and treats as unpaired. All
        four files therefore arrive as four sets, and it is the derived name that makes them
        one sample.
        """
        files = ["S12_L001_R1_001.fastq.gz", "S12_L001_R2_001.fastq.gz",
                 "S12_L002_R1_001.fastq.gz", "S12_L002_R2_001.fastq.gz"]

        derived = read_names.derive_samples(files)

        self.assertEqual([sample.name for sample in derived], ["S12"])
        self.assertEqual(sorted(derived[0].files), sorted(files))

    def test_a_bare_mate_number_is_only_stripped_when_a_mate_was_found(self):
        """`Ara-2_500gen_2` is a sample whose isolate is called 2.

        Nothing but an actual pair says a trailing digit is a read number, so a lone file keeps
        it -- and keeps its coordinate with it. Stripping on the strength of the digit alone
        would file the sample at a time point with no isolate.
        """
        alone = read_names.derive_samples(["Ara-2_500gen_2.fastq.gz"])
        self.assertEqual(alone[0].name, "Ara-2_500gen_2")

        paired = read_names.derive_samples(
            ["Ara-2_500gen_2_1.fastq.gz", "Ara-2_500gen_2_2.fastq.gz"])
        self.assertEqual(paired[0].name, "Ara-2_500gen_2")

    def test_a_trailing_chunk_index_needs_the_company_of_a_lane_or_a_read_number(self):
        # On its own, `001` is as likely to be an isolate as a bcl2fastq chunk.
        derived = read_names.derive_samples(["Ara-2_500gen_001.fastq.gz"])
        self.assertEqual(derived[0].name, "Ara-2_500gen_001")

    def test_a_name_never_ends_in_the_separator_its_decoration_hung_off(self):
        """`SRR37077254.R1` and `.R2` are mates; taking the read number out leaves the period.

        Found in real use, and nothing downstream would have caught it: `SAMPLE_NAME_RE`
        anchors the first character only, so `SRR37077254.` was a perfectly acceptable sample
        name and a perfectly acceptable directory name.
        """
        for files, expected in (
                (["SRR37077254.R1.fastq.gz", "SRR37077254.R2.fastq.gz"], "SRR37077254"),
                (["s-R1.fastq", "s-R2.fastq"], "s"),
                (["s_R1.fastq", "s_R2.fastq"], "s")):
            with self.subTest(files=files):
                self.assertEqual(read_names.derive_samples(files)[0].name, expected)

    def test_a_read_number_is_recognised_whatever_separates_it(self):
        """Single-ended, so there is no mate to find the read number by comparison.

        Underscores alone were not enough: SRA downloads arrive as `SRR….R1.fastq.gz`, and with
        no `.R2` beside them the `.R1` has to be recognised by name or not at all.
        """
        for name in ("SRR37077254.R1.fastq.gz", "SRR37077254_R1.fastq.gz",
                     "SRR37077254-R1.fastq.gz", "SRR37077254.read2.fq"):
            with self.subTest(name=name):
                self.assertEqual(read_names.derive_samples([name])[0].name, "SRR37077254")

    def test_only_a_trailing_run_of_decorations_is_removed(self):
        """What keeps `.` and `-` safe to split on.

        A read number sits at the end of a filename, so the scan stops at the first token that
        is not a decoration. Removing one from anywhere would eat the *population* out of
        `R1_500gen_x`, which is a perfectly good coordinate whose population is called R1.
        """
        self.assertEqual(
            read_names.derive_samples(["R1_500gen_x.fastq.gz"])[0].name, "R1_500gen_x")

    def test_splitting_on_hyphens_does_not_rewrite_a_name(self):
        """Separators are kept and the survivors rejoin exactly as they arrived -- splitting on
        `[._-]` and rejoining with one of them would turn `Ara-2_500gen_763A` into mush."""
        derived = read_names.derive_samples(
            ["Ara-2_500gen_763A_R1.fastq.gz", "Ara-2_500gen_763A_R2.fastq.gz"])
        self.assertEqual(derived[0].name, "Ara-2_500gen_763A")

    def test_a_name_that_is_nothing_but_decoration_keeps_something(self):
        # Better a sample called `R1` than one called "".
        derived = read_names.derive_samples(["R1.fastq.gz"])
        self.assertEqual(derived[0].name, "R1")

    def test_no_paired_mapping_is_honoured(self):
        """`--no-paired-mapping` makes every file its own read set for breseq, so the preview
        has to group the same way or it would promise something else."""
        derived = read_names.derive_samples(
            ["s_R1.fastq", "s_R2.fastq"], paired=False)

        self.assertEqual([sample.name for sample in derived], ["s"])
        self.assertEqual(len(derived[0].files), 2)
