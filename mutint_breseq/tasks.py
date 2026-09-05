"""Running breseq, and importing what it produced.

The second thing in the suite to move off the request path, and the first that had no choice:
coverage derivation was enqueued because 900 seconds is rude, and this is *hours*. It is the
shape of work a queue exists for at all.

**The argument is a primary key, not a model** -- `django.tasks` serializes arguments as JSON,
the same contract `aledb_import.tasks.build_coverage` states. And like that task this one
**re-raises** after recording the failure, rather than swallowing it: the row is what a person
reads and the queue's own record is what says a worker tried and could not, and the two failure
stories are different. A task that returned quietly on error would leave `db_worker` reporting
a clean run of a job that did nothing.

**Nothing runs inside a transaction here.** The breseq call is hours long and the ingest opens
its own per-sample transactions, so wrapping either would hold a connection open across the
whole run for no gain -- and `aledb_import.breseq_folder` already gives each sample its own,
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

from aledb_common import store
from aledb_common.tools import ToolMissing
from aledb_import import breseq_folder, import_lock
from aledb_import.import_lock import ImportInProgress
from aledb_jobs import jobs

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
        run.log = run.truncated_log(log)
    run.save(update_fields=["status", "error", "finished_at", "log"])

    shutil.rmtree(run.reads_dir(), ignore_errors=True)
    shutil.rmtree(run.output_dir(), ignore_errors=True)
    logger.info("breseq run %s cancelled", run.pk)


def _fail(run, message, log=None):
    """Record why, keep the directory, and hand the exception on."""
    run.status = STATUS_FAILED
    run.error = message
    run.finished_at = timezone.now()
    if log is not None:
        run.log = run.truncated_log(log)
    run.save(update_fields=["status", "error", "finished_at", "log"])


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


@task()
def run_breseq(run_id):
    """Run breseq for one `BreseqRun`, then import its output folder."""
    run = BreseqRun.objects.filter(pk=run_id).select_related("experiment").first()
    if run is None:
        # Deleted between enqueue and execution. Not an error: there is nothing to run, and
        # the post_delete receiver has already taken the reads with it.
        logger.info("breseq run %s is gone; nothing to do", run_id)
        return None

    # Asked before anything is done. A job cancelled while it sat on the queue is still handed
    # to a worker -- `jobs.request_cancel` deliberately never touches the queue row -- so this
    # is where that cancellation actually takes effect.
    if jobs.is_cancelled(run.task_result_id):
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
        # db_worker started outside ./mutint has no ALEDB_TOOLS_DIR, so it finds no breseq and
        # would otherwise fail every run with nothing saying why.
        _fail(run, str(missing))
        raise

    reads = sorted(
        os.path.join(run.reads_dir(), name) for name in os.listdir(run.reads_dir()))
    argv = runner.build_argv(breseq, run.output_dir(), reference, run.arguments, reads,
                             processors=runner.default_processors())
    logger.info("breseq run %s starting: %s", run.pk, " ".join(argv))

    try:
        returncode, output = runner.run_breseq_process(
            argv, runner.tool_environment(), _timeout(),
            is_cancelled=lambda: jobs.is_cancelled(run.task_result_id))
    except runner.Cancelled:
        # The process and its whole group are already gone by here; `run_breseq_process`
        # stops them before it raises. Nothing is re-raised: a cancellation is not a
        # failure, and letting it out would record the job as FAILED on the queue and put a
        # traceback in front of somebody who got exactly what they asked for.
        _cancelled(run)
        return None
    except subprocess.TimeoutExpired:
        _fail(run, "breseq did not finish within %d seconds." % _timeout())
        raise
    except OSError as exc:
        _fail(run, "breseq could not be started: %s" % exc)
        raise

    run.log = run.truncated_log(output)
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
        _wait_for_import_lock(holder, run.task_result_id)
    except jobs.JobCancelled:
        _cancelled(run, log=output)
        return None
    try:
        # The run directory holds exactly one sample folder, named for the sample, so
        # `find_sample_dirs` finds that one and takes its name from the basename -- which is
        # why nothing here passes a sample name. One rule about what a sample is called, and
        # it is aledb-core's.
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
    from aledb_sample.models import Sample

    return Sample.objects.filter(
        population__experiment=experiment,
        source_name=sample_name).order_by("-pk").first()
