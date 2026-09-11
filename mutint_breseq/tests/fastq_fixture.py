"""Writing FASTQ files that are actually FASTQ.

Extracted from `test_pairing.WhatFastpSeesTestCase._fastq`, which was the only realistic record
writer in the suite and was private to one test case. The run and launch tests write the four
bytes `ACGT` and call it a read file -- which is fine while nothing reads them, and stopped
being fine when `mate_check` started counting records.
"""

import gzip
import os


def write(path, count=1, length=150, name="r", start=1, mate=None, gz=None):
    """Write `count` well-formed records to `path` and return it.

    `mate` appends breseq's `/1` or `/2` to each identifier; `gz` defaults to whatever the name
    says, so a `.gz` path is gzipped without the caller having to say so twice.
    """
    if gz is None:
        gz = path.lower().endswith(".gz")
    opener = gzip.open if gz else open
    with opener(path, "wt") as handle:
        for number in range(start, start + count):
            identifier = "%s%d" % (name, number)
            if mate is not None:
                identifier += "/%d" % mate
            handle.write("@%s\n%s\n+\n%s\n"
                         % (identifier, "A" * length, "I" * length))
    return path


def write_pair(directory, stem="s", counts=(5, 5), length=150, suffix=".fastq", names=None):
    """A `_R1`/`_R2` pair, optionally of different lengths. Returns both paths.

    `counts` is what this exists for: two files that pair by name and hold different numbers of
    reads is the case nothing used to notice.
    """
    paths = []
    for index, count in enumerate(counts):
        default = "%s_R%d%s" % (stem, index + 1, suffix)
        path = os.path.join(directory, (names[index] if names else default))
        paths.append(write(path, count=count, length=length, mate=index + 1))
    return paths
