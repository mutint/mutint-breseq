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


def build_argv(breseq, output_dir, reference, arguments, reads, processors=None):
    """The command line for one run.

    Order matters only in that `-o` and `-r` must precede the read files, which are
    positional. The typed arguments go between, so anything they set overrides the defaults
    ahead of them and nothing they set can be mistaken for a read file.
    """
    typed = split_arguments(arguments)

    argv = [breseq]
    # Injected only when the box does not already say. breseq's default is one processor,
    # which on a real genome is the difference between an evening and a week -- and a default
    # that cannot be overridden is worse than no default, so the box wins.
    if processors and not any(flag in typed for flag in PROCESSOR_FLAGS):
        argv += ["-j", str(processors)]
    argv += ["-o", output_dir, "-r", reference]
    argv += typed
    argv += list(reads)
    return argv


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
                "breseq produced no %s. Its output is below; the run directory has been kept."
                % relative)


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
