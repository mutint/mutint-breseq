"""Building a breseq command line, and checking what it produced.

Pure, in the shape `mutint_sample/locus.py` and `functional_change.py` are: no request, no
worker, no database. That is what lets the two rules most likely to be got wrong -- what goes
on the command line, and what the output has to look like for the importer to read it -- be
tested without running breseq at all.

**Nothing here runs anything.** `run_breseq_process` did, and it is
`mutint_jobs.processes.run_tool` now: polling a cancellation flag and signalling a process
group is what any task shelling out from a worker needs, and none of it was breseq's. What is
breseq's stayed -- the argv, the PATH those binaries have to be found on, and what counts as
output the importer can read.
"""

import os
import shlex
import shutil
from collections import namedtuple

from mutint_common import tools

from mutint_breseq import pairing

BRESEQ = "breseq"
FASTP = "fastp"

# brefito's fastp settings, and only those. `--disable_quality_filtering` because quality
# trimming was found there to gut old Solexa data sets; adapter trimming is fastp's default and
# is the whole point. A pair also gets `--detect_adapter_for_pe`, which reads the adapter off
# the overlap between mates rather than guessing it from one side.
FASTP_OPTIONS = ("--disable_quality_filtering",)
FASTP_PAIRED_OPTIONS = ("--detect_adapter_for_pe",)
# fastp refuses more than this.
FASTP_MAX_THREADS = 16

# breseq's own spelling of "how many processors", both forms. Matched so that a `-j` typed in
# the arguments box wins over the default below rather than being handed to breseq twice --
# which breseq accepts, silently taking the last one, so the failure would be a page whose
# box does nothing.
PROCESSOR_FLAGS = ("-j", "--num-processors")

# breseq's own spelling of "this sample is not clonal", both forms, matched for the same reason
# `-j` is: the Population sample checkbox injects `-p` only when the box does not already say
# so. A second `-p` would not trouble breseq -- it is a boolean flag -- but a command line
# showing it twice reads as the page having ignored what was typed.
POLYMORPHISM_FLAGS = ("-p", "--polymorphism-prediction")

# breseq's "analyze only enough reads for this fold coverage", both spellings, matched for the
# same reason as the two above. Unlike `-p` this one takes a value, so a duplicate would be a
# genuine ambiguity rather than merely untidy.
COVERAGE_FLAGS = ("-l", "--limit-fold-coverage")

# breseq validates the options and every path, then exits without running and without creating
# anything -- 0 if it is happy, nonzero if not. Added in breseq-prerelease g23736ada, which
# `tools.txt` pins for this reason.
#
# **It checks that options are known and that paths are usable, not that values make sense**:
# measured, `-j notanumber` passes. So this catches the typo somebody makes in the arguments
# box and a path nothing wrote, and does not pretend to be more.
DRY_RUN_FLAG = "--dry-run"

# What `mutint_import.breseq_folder` requires of a sample directory, and therefore what a run
# must have produced before it is worth calling the importer. breseq 0.50 writes all five
# itself, which is why nothing here reshapes anything -- but the check is still made, because
# the alternative is the importer reporting "has no data/output.gd" for a run that in truth
# ran out of disk in stage 5.
REQUIRED_OUTPUT = (
    os.path.join("data", "output.gd"),
    os.path.join("data", "reference.gff3"),
    os.path.join("data", "reference.fasta"),
    os.path.join("data", "reference.bam"),
    os.path.join("data", "reference.bam.bai"),
)



class BreseqUnusable(Exception):
    """breseq ran, and what it left cannot be imported. Names the missing file."""


def split_arguments(text):
    """The arguments box as argv. Raises ValueError on unbalanced quotes.

    `shlex.split`, and **nothing is ever handed to a shell**. The box is free text from a
    person with write access, which is not the same as trusted: `; rm -rf ~` in a field that
    reached `shell=True` would be a remote shell, and the feature would still look like it
    worked. With a list and no shell there is nothing to escape and nothing to get wrong.
    """
    return shlex.split(text or "")


def format_coverage_limit(value):
    """breseq's `-l` value as text: `80` rather than `80.0`.

    The column is a float because breseq accepts one, and almost every value anybody types is
    a whole number -- so a bare `str()` would put `80.0` on every command line and in every
    log. Formatted through `%f` and trimmed rather than through `%g`, which switches to
    exponent notation past six digits and would hand breseq `1e+06`.
    """
    text = ("%f" % float(value)).rstrip("0").rstrip(".")
    return text or "0"


def build_argv(breseq, output_dir, reference, arguments, reads, processors=None,
               dry_run=False, polymorphism=False, coverage_limit=None):
    """The command line for one run.

    Order matters only in that `-o` and `-r` must precede the read files, which are
    positional. The typed arguments go between, so anything they set overrides the defaults
    ahead of them and nothing they set can be mistaken for a read file.

    `polymorphism` is the Population sample checkbox, and adds `-p` unless the box already
    names it in either spelling. `coverage_limit` is the Limit coverage box and adds `-l`
    under the same rule; **None means every read**, which is breseq's own default and is why
    nothing is added for it rather than a value meaning "no limit" being invented.

    Both follow `processors`: injected only where the arguments box does not already say, and
    placed with the other injected defaults rather than after the typed arguments, so the box
    remains the last word on everything.

    `dry_run` adds `--dry-run`, and **the preflight is otherwise this same command line** --
    one builder, deliberately, because a preflight assembled separately would validate
    something other than what runs. The single difference it cannot avoid is the read files:
    the check happens before trimming, so it names the untrimmed reads, and at launch it names
    a throwaway one because the upload has not been claimed yet. What it is checking is the
    option string, which is identical in all three.
    """
    typed = split_arguments(arguments)

    argv = [breseq]
    if dry_run:
        argv.append(DRY_RUN_FLAG)
    # Injected only when the box does not already say. breseq's default is one processor,
    # which on a real genome is the difference between an evening and a week -- and a default
    # that cannot be overridden is worse than no default, so the box wins.
    if processors and not any(flag in typed for flag in PROCESSOR_FLAGS):
        argv += ["-j", str(processors)]
    if polymorphism and not any(flag in typed for flag in POLYMORPHISM_FLAGS):
        argv.append("-p")
    if coverage_limit is not None and not any(flag in typed for flag in COVERAGE_FLAGS):
        argv += ["-l", format_coverage_limit(coverage_limit)]
    argv += ["-o", output_dir, "-r", reference]
    argv += typed
    argv += list(reads)
    return argv


def refusal_from(output):
    """The part of a failed dry run worth putting in front of a person.

    breseq says why in one of two shapes, both measured against g23736ada:

    - an option it does not know prints the **whole help** and then
      `Unknown command argument option: no-such-flag` -- the useful line is the last one, and
      the 45 before it are a manual nobody asked for;
    - a path it cannot use prints `---> ERROR Input file for ... does not exist: ...` among the
      paths it checked, then a summary line at the end.

    So: every `ERROR` line, plus the last line. Falls back to the last line alone, which is
    what a shape neither of those covers would still most likely put the reason on. The whole
    output is in the job log either way -- this is only what fits in a sentence.
    """
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if not lines:
        return ""
    wanted = [line for line in lines if "ERROR" in line]
    if lines[-1] not in wanted:
        wanted.append(lines[-1])
    return "\n".join(wanted)


def default_processors():
    """All of them. `os.cpu_count()` can answer None, in which case say nothing."""
    return os.cpu_count() or None


def tool_environment(env=None):
    """`os.environ` with the managed tools ahead of it on PATH.

    **breseq shells out to its own toolchain by bare name** -- bowtie2, samtools, gnuplot --
    so an absolute path to breseq is not enough on its own. Without this it stops with
    `Required executable "bowtie2" not found`, and it does so **exiting 0**, so a returncode
    check alone would call that run a success and hand the importer an empty directory.

    Falls back to the environment unchanged when nothing is managed, which is the developer
    who installed breseq themselves -- `tool_path` already prefers `env/tools/bin` over PATH,
    so the two agree about which breseq is being run.
    """
    env = dict(os.environ if env is None else env)
    directory = tools.tools_dir()
    if not directory:
        return env
    bin_dir = os.path.join(directory, "bin")
    existing = env.get("PATH", "")
    env["PATH"] = bin_dir + (os.pathsep + existing if existing else "")
    return env


def breseq_path():
    """Absolute path to breseq, or ToolMissing naming what installs it."""
    return tools.require(BRESEQ)


def fastp_path():
    """Absolute path to fastp, or ToolMissing naming what installs it."""
    return tools.require(FASTP)


def fastp_threads():
    """As many as breseq gets, capped where fastp caps itself."""
    count = default_processors()
    return min(count, FASTP_MAX_THREADS) if count else None


def trimmed_path(out_dir, read):
    """Where a read file's trimmed copy goes: same name, different directory.

    The name is what breseq pairs on and names read groups from, and it is also how fastp
    decides whether to write gzip -- so keeping it is what keeps a `.gz` a `.gz` and a pair a
    pair.
    """
    return os.path.join(out_dir, os.path.basename(read))


def build_fastp_argv(fastp, read_set, out_dir, threads=None):
    """fastp's command line for one read file set, single or paired."""
    argv = [fastp] + list(FASTP_OPTIONS)
    if threads:
        argv += ["--thread", str(threads)]
    argv += ["-j", os.path.join(out_dir, read_set.name + ".fastp.json"),
             "-h", os.path.join(out_dir, read_set.name + ".fastp.html")]
    first = read_set.files[0]
    argv += ["-i", first, "-o", trimmed_path(out_dir, first)]
    if len(read_set.files) == 2:
        second = read_set.files[1]
        argv += list(FASTP_PAIRED_OPTIONS)
        argv += ["-I", second, "-O", trimmed_path(out_dir, second)]
    return argv


# One read file set and whether fastp should see it. `reason` says why not, for the run log.
TrimPlan = namedtuple("TrimPlan", ["read_set", "trim", "reason"])


def plan_trimming(reads, paired=True):
    """Which of `reads` fastp trims, grouped the way breseq will group them.

    A set is left alone when any file in it is not FASTQ by name -- aligned SAM, say -- or
    holds long reads, which fastp's Illumina adapter model has no business touching. The
    decision is per set rather than per file so a pair is never half trimmed.
    """
    plans = []
    for read_set in pairing.read_file_sets(reads, paired=paired):
        if not all(pairing.is_fastq(path) for path in read_set.files):
            plans.append(TrimPlan(read_set, False, "not a FASTQ file"))
        elif any(pairing.looks_long_read(path) for path in read_set.files):
            plans.append(TrimPlan(read_set, False, "long reads, %d bp or more"
                                  % pairing.LONG_READ_TRIGGER_LENGTH))
        else:
            plans.append(TrimPlan(read_set, True, ""))
    return plans


def check_output(output_dir):
    """Raise BreseqUnusable unless `output_dir` is something the importer can read.

    Checked rather than assumed because the two failures look identical from outside: breseq
    that stopped early leaves a directory the importer describes as *"has no
    data/output.gd"*, which reads as the plugin having looked in the wrong place. Naming the
    missing file here puts the blame where it belongs, next to breseq's own log.
    """
    for relative in REQUIRED_OUTPUT:
        if not os.path.isfile(os.path.join(output_dir, relative)):
            raise BreseqUnusable(
                "breseq produced no %s. Its output is below." % relative)


def cleanup_after_import(run_dir, output_dir):
    """Throw the whole run away. The importer has already kept everything worth keeping.

    That is `data/`'s four files **and breseq's HTML report**, both copied into the store
    under the sample's own primary key by `mutint_import.breseq_folder`. This used to move
    `output/` aside into a `report/` of its own and serve it from here, which made two homes
    for the same bytes -- and the wrong one, because a report is a property of the *sample*
    that was produced and a sample outlives the run row that made it.

    Deliberately not `shutil.rmtree(run_dir)`: a run directory holds only what this plugin put
    there, but a rule that deletes a whole tree should name what it expects to find rather
    than trusting that.
    """
    shutil.rmtree(os.path.join(run_dir, "reads"), ignore_errors=True)
    shutil.rmtree(os.path.join(run_dir, "trimmed"), ignore_errors=True)
    shutil.rmtree(output_dir, ignore_errors=True)
