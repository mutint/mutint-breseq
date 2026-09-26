"""This plugin's own step on a sample's reads: trimming them with fastp.

Registered through `mutint_common.read_step_registry` like any other component's step, so the
task runs it the way it runs a FastQC step it knows nothing about -- one loop, one set of
endings, one checkbox per step on the launch page.

A `transform` step: it returns the reads breseq should read, which are the trimmed copies
where a set was trimmed and the original path where it was not. Every `inspect` step --
quality control -- has therefore already seen the reads as they arrived.
"""

import logging
import os

from mutint_common.read_step_registry import ReadStepFailed
from mutint_common.tools import ToolMissing, tool_path

from mutint_breseq import runner

logger = logging.getLogger("mutint_breseq.steps")

#: The step's name: what a run stores and what the launch page posts.
TRIM = "trim"


def trim_available():
    """`(True, '')` when fastp can be run here, else `(False, <why>)`."""
    if tool_path(runner.FASTP):
        return True, ""
    try:
        runner.fastp_path()
    except ToolMissing as missing:
        return False, str(missing)
    return False, "fastp is not installed."


def trim(ctx):
    """Run fastp over the reads, set by set. Returns what breseq should read.

    The sets are breseq's own -- see `pairing.py` -- so a pair is trimmed as a pair and the
    trimmed files, keeping their names, pair again when breseq sees them. Sets fastp should
    not touch are passed through as the original path, and the log says so.

    Raises `ReadStepFailed` when fastp is missing or exits nonzero: breseq on reads fastp could
    not read is not a run anybody wants. `processes.Cancelled`, `subprocess.TimeoutExpired`
    and `OSError` go to the task as they come, which maps them as it maps breseq's own.
    """
    try:
        fastp = runner.fastp_path()
    except ToolMissing as missing:
        raise ReadStepFailed(str(missing))

    out_dir = ctx.scratch_dir(TRIM)
    env = runner.tool_environment()
    replacement = {}
    for plan in runner.plan_trimming(ctx.reads, paired=ctx.paired):
        names = ", ".join(os.path.basename(path) for path in plan.read_set.files)
        if not plan.trim:
            ctx.write("fastp: left %s untrimmed (%s)" % (names, plan.reason))
            continue
        argv = runner.build_fastp_argv(fastp, plan.read_set, out_dir,
                                       threads=runner.fastp_threads())
        logger.info("%s trimming: %s", ctx.producer, " ".join(argv))
        try:
            returncode = ctx.run_tool(argv, what="fastp", env=env)
        except OSError as exc:
            raise ReadStepFailed("fastp could not be started: %s" % exc)
        if returncode != 0:
            raise ReadStepFailed("fastp exited %d on %s. Its output is in this job's log."
                                 % (returncode, names))
        for path in plan.read_set.files:
            replacement[path] = runner.trimmed_path(out_dir, path)
    return [replacement.get(path, path) for path in ctx.reads]
