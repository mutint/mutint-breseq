"""Running breseq, and importing what it produced.

The second thing in the suite to move off the request path, and the first that had no choice:
coverage derivation was enqueued because 900 seconds is rude, and this is *hours*. It is the
shape of work a queue exists for at all.

**The argument is a primary key, not a model** -- `django.tasks` serializes arguments as JSON,
the same contract `mutint_import.tasks.build_coverage` states. And like that task this one
**re-raises** after recording the failure, rather than swallowing it: the row is what a person
reads and the queue's own record is what says a worker tried and could not, and the two failure
stories are different. A task that returned quietly on error would leave `db_worker` reporting
a clean run of a job that did nothing.

**Nothing runs inside a transaction here.** The breseq call is hours long and the ingest opens
its own per-sample transactions, so wrapping either would hold a connection open across the
whole run for no gain -- and `mutint_import.breseq_folder` already gives each sample its own,
which is what makes a failed one roll back on its own.
"""

import logging
import os
import shutil
import subprocess
import time

from django.conf import settings
from django.tasks import task
from django.utils import timezone

from mutint_common import store
from mutint_common.tools import ToolMissing
from mutint_import import breseq_folder, import_lock
from mutint_import.import_lock import ImportInProgress
from mutint_jobs import jobs, logs, processes

from mutint_breseq import runner
from mutint_breseq.models import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_IMPORTED,
    STATUS_RUNNING,
    BreseqRun,
)

logger = logging.getLogger("mutint_breseq.tasks")

# A whole genome at real coverage is hours. The default is generous rather than tuned: what it
# is protecting against is a run that will never finish -- a wedged subprocess holding a worker
# for ever -- not a slow one, and cutting an honest twelve-hour run off at ten would be the
# expensive kind of wrong.
DEFAULT_TIMEOUT_SECONDS = 12 * 60 * 60

# How long to wait for the import lock before giving up. A web import holds it for the length
# of a drop, so a collision is minutes at worst -- but a run that took four hours must not be
# thrown away because somebody was uploading when it finished, which is what `acquire()`
# raising straight through would do.
LOCK_WAIT_SECONDS = 30 * 60
LOCK_POLL_SECONDS = 5

# `breseq --dry-run` parses the options and stats a handful of paths. It gets a budget of its
# own rather than eating the run's twelve hours, because if it ever fails to return, failing in
# minutes is the useful answer and failing at the end of the day is not.
DRY_RUN_TIMEOUT_SECONDS = 5 * 60


def _timeout():
    return getattr(settings, "MUTINT_BRESEQ_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)


def _cancelled(run, log=None):
    """Record that somebody stopped this, and throw the work away.

    **The one respect in which cancelled differs from failed**, which keeps everything: a
    failure is something to diagnose and a cancellation is not. Somebody decided they did not
    want this, so keeping gigabytes of half-finished analysis serves nobody.
    """
    run.status = STATUS_CANCELLED
    run.error = ""
    run.finished_at = timezone.now()
    if log is not None:
        run.log = log
    run.save(update_fields=["status", "error", "finished_at", "log"])

    shutil.rmtree(run.reads_dir(), ignore_errors=True)
    shutil.rmtree(run.trimmed_dir(), ignore_errors=True)
    shutil.rmtree(run.output_dir(), ignore_errors=True)
    logger.info("breseq run %s cancelled", run.pk)


def _fail(run, message, log=None):
    """Record why, keep the directory, and hand the exception on.

    `log` is stored as given: callers pass `_tail`'s answer, already cut to size.
    """
    run.status = STATUS_FAILED
    run.error = message
    run.finished_at = timezone.now()
    if log is not None:
        run.log = log
    run.save(update_fields=["status", "error", "finished_at", "log"])


def _tail(queue_id, run):
    """The end of this run's job log, cut to what the row will hold.

    `BreseqRun.log` stays, and stays the tail. The whole log is the file under
    `components/mutint_jobs/` and is read at `/jobs/<pk>/log`; this column is what the failure
    messages embed and what the run list folds open, and it outlives the `Job` row -- which
    `./mutint reap_jobs` may remove long before anybody deletes the run.

    This used to join fastp's account to breseq's by hand, in that order. Both tools append to
    one file now, so the order is simply the order they ran.
    """
    text, _truncated = logs.read_tail(queue_id)
    return run.truncated_log(text)


class _FastpFailed(Exception):
    """fastp exited nonzero. The run's log has its account; this carries only the reason."""


def _trim_reads(run, fastp, reads, paired, deadline, log, queue_id):
    """Run fastp over the reads, set by set. Returns what breseq should read.

    The sets are breseq's own -- see `pairing.py` -- so a pair is trimmed as a pair and the
    trimmed files, keeping their names, pair again when breseq sees them. Sets fastp should
    not touch are passed through as the original path, and the log says so.

    Raises `processes.Cancelled`, `subprocess.TimeoutExpired`, `OSError` as the breseq stage
    does, and `_FastpFailed` for a nonzero exit.

    `log` is the job's, and fastp writes into it directly. The lines this adds are the ones
    fastp cannot: which sets were skipped and why, which is a decision this made rather than
    anything fastp printed.
    """
    out_dir = store.ensure_dir(run.trimmed_dir())
    env = runner.tool_environment()
    replacement = {}
    for plan in runner.plan_trimming(reads, paired=paired):
        names = ", ".join(os.path.basename(path) for path in plan.read_set.files)
        if not plan.trim:
            logs.write(log, "fastp: left %s untrimmed (%s)" % (names, plan.reason))
            continue
        argv = runner.build_fastp_argv(fastp, plan.read_set, out_dir,
                                       threads=runner.fastp_threads())
        logger.info("breseq run %s trimming: %s", run.pk, " ".join(argv))
        returncode = processes.run_tool(
            argv, log, env=env, timeout=max(1, deadline - time.monotonic()),
            is_cancelled=lambda: jobs.is_cancelled(queue_id), what="fastp")
        if returncode != 0:
            raise _FastpFailed(
                "fastp exited %d on %s. Its output is in this job's log; the run directory "
                "has been kept." % (returncode, names))
        for path in plan.read_set.files:
            replacement[path] = runner.trimmed_path(out_dir, path)
    return [replacement.get(path, path) for path in reads]


def _wait_for_import_lock(holder, task_result_id=None):
    """Hold the import lock, waiting rather than failing if somebody else has it.

    `import_lock.acquire()` refuses immediately by design -- the web path would rather answer
    409 than hold a request open for somebody else's drop. Out here the calculus is the
    opposite: nobody is waiting on this response, and what is at stake is hours of finished
    work that would otherwise be discarded over a few seconds of overlap.
    """
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        # Cancellable here too. A run whose breseq has finished may still wait half an hour
        # behind a web import, and half an hour is long enough that somebody may change their
        # mind -- a wait that cannot be given up on is the same dead end as a job that cannot
        # be stopped.
        jobs.check_cancelled(task_result_id, "This run was cancelled while it waited to be "
                                             "imported.")
        try:
            import_lock.acquire(holder=holder)
            return
        except ImportInProgress:
            if time.monotonic() >= deadline:
                raise
            time.sleep(LOCK_POLL_SECONDS)


def _queue_id(context, run):
    """This task's own queue result id.

    `BreseqRun.task_result_id` is the usual answer and is what the row carries -- but the view
    writes it *after* `jobs.enqueue` returns, so a backend that runs the task inside `enqueue`
    executes the whole thing before that column is set. The `TaskContext` knows either way,
    which is the mechanism `mutint_import.tasks.build_coverage` uses for the same reason.

    Everything keyed by this id degrades the same way when there is none: cancellation cannot
    be asked for and the log is written nowhere, which is what a direct call outside any queue
    should do.
    """
    from_context = getattr(getattr(context, "task_result", None), "id", None)
    return str(from_context) if from_context else (run.task_result_id or "")


@task(takes_context=True)
def run_breseq(context, run_id):
    """Run breseq for one `BreseqRun`, then import its output folder.

    `takes_context` for the id above; a caller invoking this directly passes `None`.
    """
    run = BreseqRun.objects.filter(pk=run_id).select_related("experiment").first()
    if run is None:
        # Deleted between enqueue and execution. Not an error: there is nothing to run, and
        # the post_delete receiver has already taken the reads with it.
        logger.info("breseq run %s is gone; nothing to do", run_id)
        return None

    queue_id = _queue_id(context, run)

    # Asked before anything is done. A job cancelled while it sat on the queue is still handed
    # to a worker -- `jobs.request_cancel` deliberately never touches the queue row -- so this
    # is where that cancellation actually takes effect.
    if jobs.is_cancelled(queue_id):
        _cancelled(run)
        return None

    experiment = run.experiment
    run.status = STATUS_RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])

    reference = store.experiment_reference_path(experiment.id, store.REFERENCE_GFF3)
    if not os.path.isfile(reference):
        # Checked again here and not only at launch: a reference can be replaced, or an
        # experiment created and emptied, in the hours between enqueue and execution.
        message = ("%s has no stored reference genome, so there is nothing to call "
                   "mutations against." % experiment.name)
        _fail(run, message)
        raise RuntimeError(message)

    try:
        breseq = runner.breseq_path()
    except ToolMissing as missing:
        # The likeliest failure of all, and the reason this task does not swallow errors: a
        # db_worker started outside ./mutint has no MUTINT_TOOLS_DIR, so it finds no breseq and
        # would otherwise fail every run with nothing saying why.
        _fail(run, str(missing))
        raise

    reads = sorted(
        os.path.join(run.reads_dir(), name) for name in os.listdir(run.reads_dir()))
    # One budget for the whole run. fastp is minutes against breseq's hours, so it is not
    # given a clock of its own; what it uses comes off what breseq is then allowed.
    deadline = time.monotonic() + _timeout()

    # One log for the whole run, opened once and written by fastp, by breseq and by the lines
    # below -- so `/jobs/<pk>/log` shows the run's account in the order it happened, while it
    # is still happening. Everything after this point is inside it.
    with logs.open_log(queue_id) as log:
        # Asked before anything is trimmed, and asked again here having already been asked at
        # launch. Not redundant: `views._preflight` checks the box on the web host, and this
        # checks the machine that will actually do the work -- a `db_worker` started outside
        # `./mutint` has no MUTINT_TOOLS_DIR and finds none of breseq's toolchain. It is also
        # the check that catches breseq stopping for a missing bowtie2 **exiting 0**, which
        # `check_output` otherwise only notices after hours of not running.
        #
        # `reads` is still the untrimmed list here, which is what the dry run wants: the
        # trimmed copies do not exist yet, and breseq checks that its inputs do.
        argv = runner.build_argv(breseq, run.output_dir(), reference, run.arguments, reads,
                                 processors=runner.default_processors(), dry_run=True,
                                 polymorphism=run.population_sample,
                                 coverage_limit=run.coverage_limit)
        try:
            returncode = processes.run_tool(
                argv, log, env=runner.tool_environment(),
                timeout=DRY_RUN_TIMEOUT_SECONDS,
                is_cancelled=lambda: jobs.is_cancelled(queue_id), what="breseq --dry-run")
        except processes.Cancelled:
            _cancelled(run, log=_tail(queue_id, run))
            return None
        except subprocess.TimeoutExpired:
            _fail(run, "breseq did not answer within %d seconds when asked to check this "
                       "command line." % DRY_RUN_TIMEOUT_SECONDS, log=_tail(queue_id, run))
            raise
        except OSError as exc:
            _fail(run, "breseq could not be started: %s" % exc, log=_tail(queue_id, run))
            raise

        if returncode != 0:
            output = _tail(queue_id, run)
            _fail(run, "breseq will not accept this command line:\n\n%s"
                       % (runner.refusal_from(output)
                          or "it exited %d without saying why." % returncode),
                  log=output)
            raise RuntimeError("breseq refused the command line for run %s" % run.pk)

        if run.trim_reads:
            try:
                fastp = runner.fastp_path()
            except ToolMissing as missing:
                _fail(run, str(missing))
                raise
            # `--no-paired-mapping` makes breseq treat every file as its own set, so fastp
            # must too.
            paired = "--no-paired-mapping" not in runner.split_arguments(run.arguments)
            try:
                reads = _trim_reads(run, fastp, reads, paired, deadline, log, queue_id)
            except processes.Cancelled:
                _cancelled(run, log=_tail(queue_id, run))
                return None
            except subprocess.TimeoutExpired:
                _fail(run, "fastp did not finish within %d seconds." % _timeout(),
                      log=_tail(queue_id, run))
                raise
            except OSError as exc:
                _fail(run, "fastp could not be started: %s" % exc, log=_tail(queue_id, run))
                raise
            except _FastpFailed as failed:
                _fail(run, str(failed), log=_tail(queue_id, run))
                raise RuntimeError("fastp failed for run %s: %s" % (run.pk, failed))
            # The fourth place that polls. A cancel that landed while fastp ran its last file
            # would otherwise start an hours-long breseq that nobody wants.
            if jobs.is_cancelled(queue_id):
                _cancelled(run, log=_tail(queue_id, run))
                return None

        argv = runner.build_argv(breseq, run.output_dir(), reference, run.arguments, reads,
                                 processors=runner.default_processors(),
                                 polymorphism=run.population_sample,
                                 coverage_limit=run.coverage_limit)
        logger.info("breseq run %s starting: %s", run.pk, " ".join(argv))

        try:
            returncode = processes.run_tool(
                argv, log, env=runner.tool_environment(),
                timeout=max(1, deadline - time.monotonic()),
                is_cancelled=lambda: jobs.is_cancelled(queue_id), what="breseq")
        except processes.Cancelled:
            # The process and its whole group are already gone by here; `run_tool` stops them
            # before it raises. Nothing is re-raised: a cancellation is not a failure, and
            # letting it out would record the job as FAILED on the queue and put a traceback
            # in front of somebody who got exactly what they asked for.
            _cancelled(run, log=_tail(queue_id, run))
            return None
        except subprocess.TimeoutExpired:
            _fail(run, "breseq did not finish within %d seconds." % _timeout(),
                  log=_tail(queue_id, run))
            raise
        except OSError as exc:
            _fail(run, "breseq could not be started: %s" % exc, log=_tail(queue_id, run))
            raise

    # The tools are done, so the file is complete and the row can take its tail.
    output = _tail(queue_id, run)
    run.log = output
    run.save(update_fields=["log"])

    if returncode != 0:
        _fail(run, "breseq exited %d. Its output is below; the run directory has been kept."
                   % returncode, log=output)
        raise RuntimeError("breseq exited %d for run %s" % (returncode, run.pk))

    # breseq can stop having printed an error and still exit 0 -- a missing bowtie2 does
    # exactly that -- so the returncode is not the test. What the output *is* decides.
    try:
        runner.check_output(run.output_dir())
    except runner.BreseqUnusable as unusable:
        _fail(run, str(unusable), log=output)
        raise

    holder = "mutint_breseq run %s" % run.pk
    try:
        _wait_for_import_lock(holder, queue_id)
    except jobs.JobCancelled:
        _cancelled(run, log=output)
        return None
    try:
        # The run directory holds exactly one sample folder, named for the sample, so
        # `find_sample_dirs` finds that one and takes its name from the basename -- which is
        # why nothing here passes a sample name. One rule about what a sample is called, and
        # it is mutint-core's.
        # `user=` so the coverage job core enqueues from inside that import is attributed to
        # whoever launched this run, and appears on their /jobs/ page rather than only in the
        # superuser-only unattributed list. It is the one thing this call contributes beyond
        # handing over a directory.
        summary = breseq_folder.import_samples_into(experiment, run.directory(),
                                                    user=run.created_by)
    except Exception as exc:
        _fail(run, "breseq finished but its output could not be imported: %s" % exc,
              log=output)
        raise
    finally:
        import_lock.release()

    entry = (summary.get("files") or [{}])[0]
    if entry.get("error"):
        _fail(run, entry["error"], log=output)
        raise RuntimeError("import refused run %s: %s" % (run.pk, entry["error"]))

    sample = _imported_sample(experiment, run.sample_name)
    if run.population_sample:
        _mark_population_sample(sample)
    # Everything worth keeping -- data/'s four files and breseq's HTML report -- is already in
    # the store under the sample, put there by the importer above.
    runner.cleanup_after_import(run.directory(), run.output_dir())

    run.status = STATUS_IMPORTED
    run.sample = sample
    run.finished_at = timezone.now()
    run.error = ""
    run.save(update_fields=["status", "sample", "finished_at", "error"])
    logger.info("breseq run %s imported %s mutations as sample %s",
                run.pk, entry.get("mutations"), getattr(sample, "pk", None))
    return entry.get("mutations")


def _mark_population_sample(sample):
    """Record that this sample is a population rather than a clone.

    **Core's importer already has a rule for this and it is not enough on its own.**
    `gd_import` reads ` -p` out of the `.gd`'s `#=COMMAND` line, which is the right rule for a
    folder somebody analysed elsewhere and dropped on the Import data page -- but it writes
    `is_clonal` inside a `get_or_create(defaults=...)`, so **a run over a sample the experiment
    already holds never reaches it**. Re-running breseq with better options is exactly what
    this page is for, and the launcher says so. It also matches only the short spelling, and
    only where breseq wrote a `#=COMMAND` at all.

    So the checkbox is asserted here, where the run knows what was asked for.

    **It never writes True.** An unticked box is not a claim that the sample is a clone: the
    person may have typed `-p` into the arguments box themselves, in which case core's rule has
    already marked it and this must not undo that. Same reason the write is skipped when the
    sample is already mixed -- there is nothing to say.

    `sample` may be None, which `_imported_sample` documents as a real answer: the import
    happened and the row could not be found afterwards. There is nothing to mark and nothing
    to fail over.
    """
    if sample is None or not sample.is_clonal:
        return
    sample.is_clonal = False
    sample.save(update_fields=["is_clonal"])


def _imported_sample(experiment, sample_name):
    """The Sample the import just wrote, or None.

    Looked up by `source_name`, which is what `gd_import` writes the folder's name into, and
    **not** by `name` -- that is the isolate label, defaults to "1", and would match dozens of
    samples in a real experiment. Scoped through the population to the experiment, because
    source names are only unique within one.

    Looked up rather than returned by the importer, which answers a summary rather than rows.
    None is a real answer and not a failure: the run imported, and what is missing is the link
    on the page -- a sample renamed by hand between the import and this line, say.
    """
    from mutint_sample.models import Sample

    return Sample.objects.filter(
        population__experiment=experiment,
        source_name=sample_name).order_by("-pk").first()
