"""Building a breseq command line, and checking what it produced.

Pure, in the shape `aledb_sample/locus.py` and `functional_change.py` are: no request, no
worker, no database. That is what lets the two rules most likely to be got wrong -- what goes
on the command line, and what the output has to look like for the importer to read it -- be
tested without running breseq at all.
"""

import os
import shlex
import shutil
import signal
import subprocess
import time

from aledb_common import tools

BRESEQ = "breseq"

# How often the run loop asks whether somebody has cancelled. One indexed read against a
# unique column, against a subprocess measured in hours -- the interval is about how long a
# person waits after pressing the button, not about cost.
CANCEL_POLL_SECONDS = 2

# Between asking the process group to stop and insisting. breseq traps nothing, so this is
# only ever the time bowtie2 or samtools take to notice; it is not a shutdown protocol.
KILL_GRACE_SECONDS = 10


class Cancelled(Exception):
    """The run was stopped because somebody asked it to."""

# breseq's own spelling of "how many processors", both forms. Matched so that a `-j` typed in
# the arguments box wins over the default below rather than being handed to breseq twice --
# which breseq accepts, silently taking the last one, so the failure would be a page whose
# box does nothing.
PROCESSOR_FLAGS = ("-j", "--num-processors")

# What `aledb_import.breseq_folder` requires of a sample directory, and therefore what a run
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


def run_breseq_process(argv, env, timeout, is_cancelled=None,
                       poll_seconds=CANCEL_POLL_SECONDS):
    """Run breseq, watching for cancellation. Returns (returncode, combined output).

    `subprocess.run` cannot do this: it blocks until the process exits, so there is no moment
    at which anything could be asked whether the job is still wanted. The loop is the whole
    difference, and it is why cancellation is possible at all -- `django_tasks_db` offers no
    way to interrupt a running task, so the task has to interrupt itself.

    Raises `Cancelled` after stopping the process, or `subprocess.TimeoutExpired`.

    **`start_new_session=True`, and the signal goes to the process group.** This is the part
    that is easy to get wrong and looks correct when it is: breseq spawns bowtie2 and samtools
    as children, so `process.kill()` reaps the parent and leaves them running with no parent
    at all. The job would report itself cancelled while the machine stayed saturated, which is
    worse than not offering the button. A new session makes the whole run one process group
    with one thing to signal.
    """
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        start_new_session=True)

    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                output, _ = process.communicate(timeout=poll_seconds)
                return process.returncode, (output or b"").decode("utf-8", "replace")
            except subprocess.TimeoutExpired:
                pass

            if is_cancelled is not None and is_cancelled():
                _stop(process)
                raise Cancelled("breseq was cancelled.")

            if time.monotonic() >= deadline:
                _stop(process)
                raise subprocess.TimeoutExpired(argv, timeout)
    finally:
        # communicate() on the way out, or the pipe is left open and the child can block
        # writing to a buffer nobody drains.
        if process.poll() is None:
            _stop(process)
        try:
            process.communicate(timeout=KILL_GRACE_SECONDS)
        except Exception:
            pass


def _stop(process):
    """SIGTERM the whole process group, then SIGKILL what is left."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            process.wait(timeout=KILL_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


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
    under the sample's own primary key by `aledb_import.breseq_folder`. This used to move
    `output/` aside into a `report/` of its own and serve it from here, which made two homes
    for the same bytes -- and the wrong one, because a report is a property of the *sample*
    that was produced and a sample outlives the run row that made it.

    Deliberately not `shutil.rmtree(run_dir)`: a run directory holds only what this plugin put
    there, but a rule that deletes a whole tree should name what it expects to find rather
    than trusting that.
    """
    shutil.rmtree(os.path.join(run_dir, "reads"), ignore_errors=True)
    shutil.rmtree(output_dir, ignore_errors=True)
