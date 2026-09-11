"""What a read file's name says the sample is called.

Pure, in the shape `pairing.py` and `runner.py` are: no request, no database, no breseq. This is
the rule most likely to be got wrong by accident -- it decides how twenty dropped files become
ten samples -- and it is testable without any of those.

**It parses no coordinates.** It strips what a read file's name carries *beyond* the sample's --
the extension, the read number, the lane, the chunk index -- and the remainder is handed to
`mutint_import.sample_names.parse_sample_identity` by the same importer that names a dropped
breseq folder. So a sample analyzed from reads and the same sample uploaded as a folder cannot
disagree about where they land, and there is one rule about what a sample is called rather than
two.

**It says nothing about clonality either.** Whether a run is a population sample is the
Population sample checkbox's to answer, for every sample in the drop. A vocabulary of words
meaning *clone* would be a second thing to keep up to date and a silent way for one sample in
twenty to differ from what the form said.

**The grouping is breseq's own.** `pairing.read_file_sets` decides which files are mates, so the
sample a file lands in is decided by the same rule that will build breseq's read groups.
"""

import re
from collections import namedtuple

from mutint_breseq import pairing

#: One sample, and the files that will produce it. `files` keeps the order they were given in,
#: which for a pair is R1 then R2 -- breseq's own ordering, from `read_file_sets`.
DerivedSample = namedtuple("DerivedSample", ["name", "files"])

#: The FASTQ suffixes to take off before anything else is read. `pairing.base_name` strips only
#: `.gz` and `.fastq` -- breseq's own rule, and deliberately not `.fq` -- so a `.fq.gz` file
#: would otherwise carry `.fq` into the sample's name.
_EXTENSIONS = (".fastq.gz", ".fq.gz", ".fastq", ".fq")

#: An Illumina lane field: `L001`. Three digits, because `L1` is a plausible sample name and
#: this is the spelling bcl2fastq actually writes.
_LANE = re.compile(r"^L\d{3}$", re.IGNORECASE)

#: A read-number field on a file with no mate in the drop: `R1`, `read2`. A bare `1` is
#: deliberately **not** here -- `Ara-2_500gen_2` is a sample whose isolate is called 2, and a
#: rule that ate it would file the sample at a coordinate nobody chose. A bare mate number is
#: only ever removed when the pairing found an actual mate, where the differing character says
#: which one it is.
_READ_NUMBER = re.compile(r"^(?:R|read)[12]$", re.IGNORECASE)

#: bcl2fastq's trailing chunk index: `001`. Only ever dropped when a lane or a read number was
#: dropped as well -- on its own, a trailing `001` is as likely to be an isolate.
_CHUNK = re.compile(r"^\d{3}$")

#: Separators that must not be left dangling at either end of a derived name.
#:
#: Taking a decoration out of the middle of a name leaves the separator that introduced it:
#: `SRR37077254.R1` and `SRR37077254.R2` are mates, removing the read number leaves
#: `SRR37077254.`, and that trailing period would become the sample's name and its directory's.
#: Nothing downstream refuses it -- `SAMPLE_NAME_RE` anchors the *first* character only -- so it
#: reached the database, which is how it was found.
_EDGE_SEPARATORS = "._-"


def strip_extension(name):
    """`name` without one trailing FASTQ suffix, whichever of the four it wears."""
    lowered = name.lower()
    for suffix in _EXTENSIONS:
        if lowered.endswith(suffix):
            return name[:-len(suffix)]
    return name


def _difference_removed(first, second):
    """`first` with the one character that differs from `second` taken out, and its `R`.

    breseq pairs two files when their names are the same length and differ in exactly one
    place, where one has a `1` and the other a `2` -- so that position *is* the read number,
    identified by the same rule that will pair them. Reading it off the pair beats matching
    `R1` against the end of the name: it needs no vocabulary and it is right for
    `lane_1.fq`/`lane_2.fq` as well as for `_R1`/`_R2`.

    An `R` immediately before it goes too, so `s_R1`/`s_R2` leaves `s_` rather than `s_R`.
    """
    for index, (left, right) in enumerate(zip(first, second)):
        if left == right:
            continue
        start = index
        if index and first[index - 1] in "Rr":
            start = index - 1
        return first[:start] + first[index + 1:]
    return first


def _is_decoration(token):
    return bool(_LANE.match(token) or _READ_NUMBER.match(token) or _CHUNK.match(token))


def strip_decorations(name):
    """One file's name with the extension, lane, read number and chunk index removed.

    Per file and by explicit pattern, and **that ordering is the whole of why this works**.
    The obvious alternative is to pair the files first and read the read number off the one
    character that differs between mates -- which needs no vocabulary and is breseq's own rule.
    It is also wrong here, measured: with two lanes in the drop, `S12_L001_R1_001` can be
    paired with its own R2 *or* with `S12_L002_R1_001`, and which of the two breseq settles on
    depends on the order the files arrive in. The differing character is then the *lane* digit,
    and the derived name comes out as `S12_L00`.

    So the lane goes first, by name, and mates are found afterwards -- see `derive_samples`.

    **Any of `.`, `-` and `_` separates a token**, and the separator goes with whatever it
    introduced. Underscores alone were not enough: single-end reads are real and
    `SRR37077254.R1.fastq.gz` arrives with no mate to compare against, so the `.R1` has to be
    recognised by name or not at all.

    **Only a trailing run of decorations is removed**, which is what keeps that widening safe.
    A read number sits at the end of a filename -- or beside the chunk index, which is also at
    the end -- and a rule that removed one from anywhere would eat the *population* out of
    `R1_500gen_x`. The scan stops at the first token that is not a decoration, and at least one
    token always survives.
    """
    base = strip_extension(name.rsplit("/", 1)[-1])

    # Separators are kept so the survivors rejoin exactly as they arrived: splitting on
    # `[._-]` and rejoining with one of them would rewrite `Ara-2_500gen_763A`.
    pieces = re.split(r"([._-])", base)
    tokens, separators = pieces[0::2], pieces[1::2]

    end = len(tokens)
    while end > 1 and _is_decoration(tokens[end - 1]):
        end -= 1

    # A chunk index is only a chunk index in the company of a lane or a read number. On its
    # own a trailing `001` is as likely to be an isolate, so the whole tail goes back.
    tail = tokens[end:]
    if tail and not any(_LANE.match(token) or _READ_NUMBER.match(token) for token in tail):
        end = len(tokens)

    kept = tokens[:end]
    rejoined = kept[0] if kept else ""
    for index in range(1, len(kept)):
        rejoined += separators[index - 1] + kept[index]
    return rejoined or base


def derive_samples(names, paired=True):
    """`names` grouped into samples, in the order they first appear.

    Two things bring several files together, and they are asked in this order:

    **The stripped name.** Everything that strips to `S12` is one sample -- which is what a
    lane split is, `S12_L001_*` and `S12_L002_*` being one library sequenced twice.

    **Then breseq's mate rule, over the stripped names.** `lane_1.fq` and `lane_2.fq` are mates
    with no `R` to say so, and nothing but the pairing rule can tell that trailing `1` from the
    isolate in `Ara-2_500gen_2`. Applying it *after* stripping is what keeps it from mistaking
    a lane digit for a read number: by then there is no lane left to mistake.

    `paired` is breseq's `--no-paired-mapping`, threaded through so a drop previews the way it
    will actually run.
    """
    order = []
    files_by_stripped = {}
    for name in names:
        stripped = strip_decorations(name)
        if stripped not in files_by_stripped:
            order.append(stripped)
            files_by_stripped[stripped] = []
        files_by_stripped[stripped].append(name)

    derived = []
    for read_set in pairing.read_file_sets(order, paired=paired):
        if len(read_set.files) == 2:
            name = _difference_removed(read_set.files[0], read_set.files[1])
        else:
            name = read_set.files[0]
        files = []
        for stripped in read_set.files:
            files.extend(files_by_stripped[stripped])
        # Both ends, and every separator: a name is the one the sample wears everywhere in
        # MutInt and the one its directory takes, so it should not end in the punctuation that
        # used to introduce something this stripped out.
        derived.append(
            DerivedSample(name.strip(_EDGE_SEPARATORS) or read_set.files[0], files))
    return derived
