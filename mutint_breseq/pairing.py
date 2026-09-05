"""Which read files are a pair, decided exactly the way breseq decides it.

fastp trims a pair in paired-end mode, so this plugin has to know which files are mates before
breseq does -- and if it decided differently, the read groups breseq builds from the trimmed
files would not be the ones it would have built from the originals. So the rule here is
breseq's own, transcribed from `cReadFileSets::Init` in `src/breseq/settings.cpp` (0.50.0), and
nothing more clever:

- the base name is the file's name with the directory removed, then a trailing `.gz`
  removed, then a trailing `.fastq` removed -- so `.fq` stays in the base, harmlessly, because
  both mates carry it;
- a duplicate base name gets `_1`, `_2`, ... appended, in input order, *before* pairing;
- files are visited in input order, and each unused one looks for an unused partner whose base
  name is identical except that one `1` has become a `2` or one `2` a `1`. Exactly one such
  partner makes a pair, R1 being whichever holds the `1`; none, or more than one, leaves the
  file unpaired.

There is no `_R1` regex and no read count check, because breseq has neither. What it does not
know about -- what a FASTQ looks like, and how long a read is -- lives at the bottom of this
file, because those two are what decide whether fastp should see a file at all.
"""

import gzip
import os
from collections import namedtuple

# `files` is one path, or two with R1 first. `name` is what breseq calls the set: the base name
# with an `X` where the mates differ, or the file's own base when it stands alone.
ReadFileSet = namedtuple("ReadFileSet", ["name", "files"])

FASTQ_SUFFIXES = (".fastq", ".fq", ".fastq.gz", ".fq.gz")

# breseq's own `--long-read-trigger-length` default: a file whose longest read reaches this is
# a long-read file to breseq, and is one fastp should leave alone -- its adapter model is
# Illumina's, and brefito never lets it near nanopore reads either.
LONG_READ_TRIGGER_LENGTH = 1000
# How many records to look at before deciding. A file of long reads announces itself in the
# first one; this is only so a sampled short-read file cannot be mistaken for it by one record.
LONG_READ_SAMPLE_RECORDS = 200


def base_name(path):
    """breseq's base name for a read file: no directory, no trailing `.gz`, no trailing `.fastq`."""
    name = path.rsplit("/", 1)[-1]
    if name.endswith(".gz"):
        name = name[:-3]
    if name.endswith(".fastq"):
        name = name[:-6]
    return name


def read_file_sets(paths, paired=True):
    """Group `paths` into read file sets the way breseq will.

    `paired=False` is breseq's `--no-paired-mapping`: every file is its own set.
    """
    files, bases = [], []
    seen_paths, seen_bases = set(), set()
    for path in paths:
        # breseq warns and uses a repeated path once.
        if path in seen_paths:
            continue
        seen_paths.add(path)
        original = base_name(path)
        base, duplicate = original, 0
        while base in seen_bases:
            duplicate += 1
            base = "%s_%d" % (original, duplicate)
        seen_bases.add(base)
        files.append(path)
        bases.append(base)

    if not paired:
        return [ReadFileSet(base, [path]) for base, path in zip(bases, files)]

    index = {base: i for i, base in enumerate(bases)}
    used = [False] * len(files)
    sets = []
    for i, name in enumerate(bases):
        if used[i]:
            continue
        candidates = []
        for k, char in enumerate(name):
            if char not in "12":
                continue
            other = name[:k] + ("2" if char == "1" else "1") + name[k + 1:]
            j = index.get(other)
            if j is not None and not used[j]:
                candidates.append((k, j))
        if len(candidates) == 1:
            k, j = candidates[0]
            first, second = (files[i], files[j]) if name[k] == "1" else (files[j], files[i])
            sets.append(ReadFileSet(name[:k] + "X" + name[k + 1:], [first, second]))
            used[i] = used[j] = True
        else:
            # Zero partners, or several: breseq treats both as unpaired (and warns on several).
            sets.append(ReadFileSet(name, [files[i]]))
            used[i] = True
    return sets


def is_fastq(path):
    """Whether the name says FASTQ, which is the only kind of file fastp can read."""
    return path.lower().endswith(FASTQ_SUFFIXES)


def looks_long_read(path, trigger=LONG_READ_TRIGGER_LENGTH, sample=LONG_READ_SAMPLE_RECORDS):
    """Whether the first `sample` records hold a read of `trigger` bases or more.

    Reads the sequence line of each record and nothing else. A file that cannot be opened or
    is not FASTQ-shaped answers False: fastp then sees it and says what is wrong, which is a
    better report than this function guessing.
    """
    opener = gzip.open if path.lower().endswith(".gz") else open
    try:
        with opener(path, "rb") as handle:
            for number, line in enumerate(handle):
                if number >= sample * 4:
                    break
                if number % 4 == 1 and len(line.rstrip(b"\r\n")) >= trigger:
                    return True
    except (OSError, EOFError, ValueError):
        return False
    return False
