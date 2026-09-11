"""Whether two files that pair by name really are mates.

Pure, in the shape `pairing.py` and `read_names.py` are: no request, no database, no tools.

**Nothing checked this, and neither tool complains usefully.** `pairing.read_file_sets` is
breseq's own rule and decides mates from the *names* alone -- same length, differing at exactly
one `1`/`2` -- which is all breseq itself knows too. Handed two files that pair by name and hold
different numbers of reads, both tools **truncate to the shorter and exit 0**. Measured against
the pinned versions:

- fastp 1.3.6, given 5 reads and 3, writes 3 and 3 and prints
  `WARNING: different read numbers of the 0 pack`;
- breseq 0.50, given 400 and 250, reports `num_reads=250` for *both* files in its own
  `summary.json` and prints `Warning: R2 file ended before R1 in paired FASTQ processing.`

Each warns, into hundreds of lines nobody reads, with a zero exit code -- so the run succeeds,
the sample imports, and the reads are gone. That is the same shape as the missing-bowtie2 trap
this plugin already documents: what the run *produced* decides, not what it exited with.

Since both tools do it, the check belongs to the data rather than to fastp, and runs whether or
not trimming is on.
"""

import gzip
import os
import re
from collections import namedtuple

from mutint_breseq import pairing

#: How much to decompress at a time. Counting newlines a block at a time rather than iterating
#: lines is what makes this affordable: measured at 2 000 000 records in 0.21s, against a breseq
#: run of hours.
_BLOCK = 1024 * 1024

#: Longest first, so `.fastq.gz` is not matched as `.gz`-less `.fastq`. `pairing.FASTQ_SUFFIXES`
#: holds the same four in the order breseq lists them, which is not the order to match in.
_SUFFIXES = tuple(sorted(pairing.FASTQ_SUFFIXES, key=len, reverse=True))

#: What is inserted before the suffix to stop a file pairing. See `unpaired_name`.
UNPAIRED_MARKER = ".unpaired"

#: A trailing mate number on a read identifier: `@read1/1`. Illumina and SRA put theirs after a
#: space instead, which is already gone by the time this applies.
_MATE_SUFFIX = re.compile(r"/[12]$")

#: Records in a file, and whether its line count divided evenly by four -- a record missing a
#: line outright, which is what a download cut between records looks like. Free once the lines
#: are counted.
#:
#: **It does not see truncation inside a line.** A file cut mid-quality-line still has four
#: lines in that record and divides evenly. What catches that case is the other check: the two
#: mates then disagree about how many reads they hold.
RecordCount = namedtuple("RecordCount", ["records", "whole"])


def _opener(path):
    """gzip by suffix, exactly as `pairing.looks_long_read` decides it."""
    return gzip.open if path.lower().endswith(".gz") else open


def count_records(path):
    """`RecordCount` for a FASTQ file, or None if it cannot be read.

    None rather than an exception, and None means *say nothing*: a file this cannot open is one
    fastp is about to complain about in its own words, which is a better report than a guess
    from here. The same posture `looks_long_read` takes.
    """
    lines = 0
    last = b"\n"
    try:
        with _opener(path)(path, "rb") as handle:
            while True:
                block = handle.read(_BLOCK)
                if not block:
                    break
                lines += block.count(b"\n")
                last = block[-1:]
    except (OSError, EOFError, ValueError):
        return None
    if last != b"\n":
        # A final line with no newline after it is still a line, and a file that ends that way
        # is exactly the truncation worth catching.
        lines += 1
    return RecordCount(lines // 4, lines % 4 == 0)


def first_read_id(path):
    """The identifier of the first read, without its mate number, or None.

    Three conventions are in use and all three reduce to the same thing once whatever follows
    the first whitespace is dropped:

        @read1/1                                  -> read1
        @A00123:45:HXXX:1:1101:1000:1000 1:N:0:AC -> A00123:45:HXXX:1:1101:1000:1000
        @SRR37077254.1 1 length=100               -> SRR37077254.1

    Equal counts are not proof two files are mates -- two different samples' R1 can pair by name
    and match in size -- and one record each is what tells them apart.
    """
    try:
        with _opener(path)(path, "rb") as handle:
            header = handle.readline()
    except (OSError, EOFError, ValueError):
        return None

    text = header.decode("utf-8", "replace").strip()
    if not text.startswith("@"):
        return None
    fields = text[1:].split()
    if not fields:
        return None
    return _MATE_SUFFIX.sub("", fields[0]) or None


def mismatch_reason(first, second):
    """Why these two are not mates, as a clause, or None if nothing says they are not.

    The clause completes *"`a` and `b` pair by name but ..."*, so the caller owns the sentence
    and, more to the point, owns what was *done* about it. This module decides nothing.

    Ordered cheapest-to-most-specific in what it tells a person: a file that cannot be read at
    all, then one that ends mid-record, then counts, then the reads themselves.
    """
    counts = [count_records(first), count_records(second)]
    if any(count is None for count in counts):
        return "one of them could not be read"

    for path, count in zip((first, second), counts):
        if not count.whole:
            return "%s ends part-way through a record" % os.path.basename(path)

    if counts[0].records != counts[1].records:
        return "hold different numbers of reads ({:,} and {:,})".format(
            counts[0].records, counts[1].records)

    identifiers = [first_read_id(first), first_read_id(second)]
    if all(identifiers) and identifiers[0] != identifiers[1]:
        return "start with different reads (%s and %s)" % tuple(identifiers)

    return None


def uploaded_name(path):
    """The basename as it was uploaded, with `.unpaired` taken back out if it is there.

    The inverse of `unpaired_name`, for saying what a run was made from: the file breseq read
    may carry a marker this plugin added, and a person should be shown the name they dropped
    rather than one invented mid-run. The *grouping* still reflects what actually happened.

    Removed only where `unpaired_name` would have put it -- immediately before the FASTQ
    suffix -- so a file somebody genuinely named `batch.unpaired.fastq` is left alone.
    """
    name = os.path.basename(path)
    lowered = name.lower()
    for suffix in _SUFFIXES:
        if lowered.endswith(UNPAIRED_MARKER + suffix):
            return name[:-len(UNPAIRED_MARKER + suffix)] + name[len(name) - len(suffix):]
    return name


def unpaired_name(path):
    """`path` renamed so that nothing will pair it: `.unpaired` before the FASTQ suffix.

    `s_R2.fastq.gz` becomes `s_R2.unpaired.fastq.gz`, which is a different *length* from
    `s_R1.fastq.gz` -- and breseq's rule pairs only names of equal length. The suffix is kept so
    the file is still FASTQ to `pairing.is_fastq` and to both tools.

    **Renaming the file rather than telling the tools anything** is what makes this hold on both
    sides: fastp is told which files are mates by `runner.build_fastp_argv`, and breseq works it
    out for itself from the names it is given. One rule, applied to the bytes on disk, and the
    two cannot disagree.
    """
    directory, name = os.path.split(path)
    lowered = name.lower()
    for suffix in _SUFFIXES:
        if lowered.endswith(suffix):
            name = name[:-len(suffix)] + UNPAIRED_MARKER + name[len(name) - len(suffix):]
            break
    else:
        name += UNPAIRED_MARKER
    return os.path.join(directory, name)
