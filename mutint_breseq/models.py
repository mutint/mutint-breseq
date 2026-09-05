"""One breseq run: what was asked for, how it went, and what it produced.

A row exists because the work outlives the request that asked for it by hours, and there are
three things nothing else in the suite can answer afterwards -- what arguments were used, what
breseq said, and which sample came out. `django_tasks_db` stores a status and a traceback, but
it is a queue's record of a job rather than the product's record of an analysis: it is keyed
by a UUID nobody sees, it is reaped, and it knows nothing about experiments.

The relationship between the two is worth stating, because it is the thing that makes the page
honest. `status` here is what *this* believes; `task_result_id` is how to ask the queue. They
disagree in exactly one useful way -- a row that says `queued` while the queue says the task
has never been picked up means **no worker is running**, which is this design's easiest
failure and is otherwise indistinguishable from a slow start.
"""

import logging
import os
import shutil

from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver

from aledb_common import store

logger = logging.getLogger("mutint_breseq.models")

COMPONENT = "mutint_breseq"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_IMPORTED = "imported"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

STATUS_CHOICES = [
    (STATUS_QUEUED, "Queued"),
    (STATUS_RUNNING, "Running"),
    (STATUS_IMPORTED, "Imported"),
    (STATUS_FAILED, "Failed"),
    (STATUS_CANCELLED, "Cancelled"),
]

# `cancelled` was once absent, with a note saying nothing here could stop a running breseq
# because the process belongs to the worker -- and a status the product cannot enforce is a
# button that lies. That was true of the *queue*, which still offers no way to interrupt a
# running task, and it stopped being true of this plugin when the run loop began asking. The
# task stops itself; see runner.run_breseq_process. Keep the two joined: if the polling ever
# goes, so must this status.

FINISHED_STATUSES = (STATUS_IMPORTED, STATUS_FAILED, STATUS_CANCELLED)

# breseq's output is verbose and its tail is the part that says what went wrong. Kept on the
# row rather than in a file so it survives the directory being cleaned up after a success.
MAX_LOG_CHARS = 20000


class BreseqRun(models.Model):
    """One launch: some reads, a sample name, and whatever breseq made of them."""

    experiment = models.ForeignKey("aledb_experiment.Experiment",
                                   on_delete=models.CASCADE, related_name="breseq_runs")
    # SET_NULL rather than CASCADE: deleting a person must not delete the record of an
    # analysis, which is a fact about the data rather than about them.
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    # Becomes the breseq output directory's name, which is what `find_sample_dirs` reads the
    # sample name from and what `sample_names.parse_sample_identity` reads the ALE, flask and
    # isolate out of. Validated against SAMPLE_NAME_RE in views.py before it is ever a path.
    sample_name = models.CharField(max_length=200)
    # The one box, verbatim as typed. Split with shlex at run time, never handed to a shell.
    arguments = models.TextField(blank=True)
    # The basenames dropped, so the run list can say what it was given after the reads are
    # deleted. Not paths: nothing resolves these, they are for a person to read.
    read_files = models.JSONField(default=list)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    # django.tasks' own result id, for `run_breseq.get_result(...)`. Blank when the enqueue
    # itself failed, which is a state the page has to render rather than assume away.
    task_result_id = models.CharField(max_length=64, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # The tail of breseq's combined stdout/stderr, whether it succeeded or not.
    log = models.TextField(blank=True)
    error = models.TextField(blank=True)

    # The sample the import produced. SET_NULL because deleting the sample -- through the
    # sample editor, or by re-importing over it -- must not delete the record of the run that
    # made it; the row then says what happened and no longer points anywhere.
    sample = models.ForeignKey("aledb_sample.Sample", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="breseq_runs")
    # Whether `report/` holds breseq's own HTML. False for a run that failed before writing
    # one, and for a successful run whose output was later removed by hand.
    report_stored = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return "BreseqRun %s (%s, %s)" % (self.pk, self.sample_name, self.status)

    @property
    def is_finished(self):
        return self.status in FINISHED_STATUSES

    def directory(self):
        """This run's own work area. Everything it writes lives under here."""
        return store.component_dir(COMPONENT, self.pk)

    def reads_dir(self):
        return os.path.join(self.directory(), "reads")

    def output_dir(self):
        """breseq's ``-o``. **Named for the sample**, which is not decoration.

        ``breseq_folder.find_sample_dirs`` walks looking for ``<dir>/data/output.gd`` and takes
        the sample's name from that directory's basename, so making the output directory the
        sample name is what lets the ingest be `import_samples_into` with no reshaping and no
        second rule about what a sample is called.
        """
        return os.path.join(self.directory(), self.sample_name)

    def report_dir(self):
        """Where breseq's own HTML is kept after a successful import."""
        return os.path.join(self.directory(), "report")

    def truncated_log(self, text):
        if len(text) <= MAX_LOG_CHARS:
            return text
        return "…(earlier output trimmed)…\n" + text[-MAX_LOG_CHARS:]


@receiver(post_delete, sender=BreseqRun)
def _remove_run_directory(sender, instance, **kwargs):
    """A run's files go when its row does.

    `store.component_dir` is deliberately not reaped by core -- it cannot know what a component
    keeps there -- so this receiver is the whole lifecycle. It is also what makes deleting an
    *experiment* reach the reads and the report, since the FK above cascades: without it a
    purged experiment would leave gigabytes behind with nothing left pointing at them.
    """
    try:
        shutil.rmtree(store.component_dir(COMPONENT, instance.pk), ignore_errors=True)
    except (ValueError, TypeError):
        # An unsaved instance has no pk to build a path from. Nothing to remove.
        logger.debug("no directory to remove for an unsaved BreseqRun")
