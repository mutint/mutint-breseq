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

from mutint_common import store

logger = logging.getLogger("mutint_breseq.models")

COMPONENT = "mutint_breseq"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_IMPORTED = "imported"
#: A curation check's success. `imported` would be a lie about a run that imported nothing
#: -- a check leaves the sample's mutations alone and replaces its evidence.
STATUS_CHECKED = "checked"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

STATUS_CHOICES = [
    (STATUS_QUEUED, "Queued"),
    (STATUS_RUNNING, "Running"),
    (STATUS_IMPORTED, "Imported"),
    (STATUS_CHECKED, "Checked"),
    (STATUS_FAILED, "Failed"),
    (STATUS_CANCELLED, "Cancelled"),
]

#: What a run is for. An *analysis* runs breseq on reads and imports the result as a sample;
#: a *check* runs breseq on the same reads with the sample's curated mutations applied first
#: (`--apply-check`) and keeps what is left as the sample's evidence. Everything from the
#: reads to breseq's exit is the same task; `kind` decides what happens to the output.
KIND_ANALYSIS = "analysis"
KIND_CHECK = "check"
KIND_CHOICES = [(KIND_ANALYSIS, "Analysis"), (KIND_CHECK, "Curation check")]

# `cancelled` was once absent, with a note saying nothing here could stop a running breseq
# because the process belongs to the worker -- and a status the product cannot enforce is a
# button that lies. That was true of the *queue*, which still offers no way to interrupt a
# running task, and it stopped being true of this plugin when the run loop began asking. The
# task stops itself; see mutint_jobs.processes.run_tool. Keep the two joined: if the polling ever
# goes, so must this status.

FINISHED_STATUSES = (STATUS_IMPORTED, STATUS_CHECKED, STATUS_FAILED, STATUS_CANCELLED)

# breseq's output is verbose and its tail is the part that says what went wrong. Kept on the
# row rather than in a file so it survives the directory being cleaned up after a success.
MAX_LOG_CHARS = 20000


class BreseqRun(models.Model):
    """One launch: some reads, a sample name, and whatever breseq made of them."""

    experiment = models.ForeignKey("mutint_experiment.Experiment",
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
    # The read steps ticked at launch, by name -- `mutint_common.read_step_registry`'s, so this
    # plugin's own `trim` (fastp) and any installed component's (a FastQC report) alike, run in
    # the registry's order before breseq sees the reads. Recorded per run rather than read from
    # a setting so a row says what was actually done to its reads.
    read_steps = models.JSONField(default=list)
    # Whether this was launched as a population -- a whole evolving population sequenced
    # together -- rather than a clone. Two effects, deliberately joined: `-p` goes on breseq's
    # command line, and the sample the import produces is recorded `is_clonal=False`. Kept per
    # run for the reason `read_steps` is: the row says what was actually asked for. Runs from
    # before the option existed carry False, which is the truth about them.
    population_sample = models.BooleanField(default=False)
    # breseq's `-l`: analyze only enough reads to reach this fold coverage. **Null is a real
    # value** -- it means every read, which is breseq's own default -- so this is nullable
    # rather than 0, which would be a coverage limit of nothing. A float because breseq takes
    # one; whole numbers are the realistic case and `runner.format_coverage_limit` is what
    # keeps `80` from reaching the command line as `80.0`.
    coverage_limit = models.FloatField(null=True, blank=True)
    # What a `metadata.csv` row said about the sample beyond where it sits -- its treatment,
    # description and flags, as `mutint_import.metadata.details_of` shapes them -- applied to
    # the sample once the import has made it, through core's `apply_sample_details`. Carried
    # on the row because the CSV is read at launch and the sample exists hours later; empty
    # for a launch with no CSV, or a row that said nothing beyond the coordinate.
    sample_details = models.JSONField(default=dict)
    # The basenames dropped, so the run list can say what it was given after the reads are
    # deleted. Not paths: nothing resolves these, they are for a person to read.
    read_files = models.JSONField(default=list)
    # The SRA accessions this run fetches its reads from, as the *resolved* plans --
    # `mutint_import.sra.Plan.as_dict()` entries, restricted to the runs that are this row's --
    # rather than the text somebody typed, so the worker downloads what the launch resolved and
    # gets no second opinion about what was meant. Empty is a run whose reads were all dropped.
    # `read_files` above still lists every filename, ENA's basenames included, so the run list
    # needs no second reader.
    accessions = models.JSONField(default=list)

    # Things a person should read about a run that **worked**. `error` is the failure message
    # and the run list only folds the log open for a failure, so neither is a home for "this
    # succeeded, and not quite as asked": a pair split because its mates disagreed still
    # produces a sample, and replacing a silent truncation with a silent unpairing would be no
    # better than the bug. A list because one run is one sample and a sample may hold several
    # read sets, so there may be several things to say.
    notes = models.JSONField(default=list)

    # Analysis or check -- see `KIND_ANALYSIS`. Decides what the task does with breseq's
    # output and which tense `sample` below is read in.
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=KIND_ANALYSIS)
    # What a check was handed and what it found: `exported` and `polymorphic_skipped` (how
    # many mutations the curated `.gd` carried, and how many of those breseq applies no
    # polymorphism for), `reads_source` (`kept`, `sra` or `drop`), and afterwards
    # `residual_mutations`, `evidence` and `carried_dismissals` from the ingest. Shown on the
    # run list; nothing queries it.
    check_summary = models.JSONField(default=dict)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    # django.tasks' own result id, for `run_breseq.get_result(...)`. Blank when the enqueue
    # itself failed, which is a state the page has to render rather than assume away.
    task_result_id = models.CharField(max_length=64, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # The tail of breseq's combined stdout/stderr, whether it succeeded or not.
    log = models.TextField(blank=True)
    error = models.TextField(blank=True)

    # The sample the import produced -- or, for a check, **the sample being checked**, set
    # at creation. One column read in two tenses, and `kind` says which; a second FK for the
    # check would be the same SET_NULL pointing at the same table. SET_NULL because deleting
    # the sample -- through the sample editor, or by re-importing over it -- must not delete
    # the record of the run that made it; the row then says what happened and no longer
    # points anywhere.
    #
    # It is also where breseq's HTML report went. This model carried a `report_stored` flag
    # and a `report/` directory of its own, and both are gone: mutint-core keeps the report
    # under the *sample*, which is where it belongs -- a report describes the sample that was
    # produced, and the sample outlives this row.
    sample = models.ForeignKey("mutint_sample.Sample", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="breseq_runs")

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

    def steps_dir(self):
        """Where the read steps write -- `ReadStepContext.scratch_dir` makes one directory per
        step under here. Removed at every ending, as `reads/` is."""
        return os.path.join(self.directory(), "steps")

    def trimmed_dir(self):
        """Where fastp writes. Same file names as `reads/`, so breseq pairs them identically."""
        from mutint_breseq.steps import TRIM
        return os.path.join(self.steps_dir(), TRIM)

    @property
    def is_check(self):
        return self.kind == KIND_CHECK

    def curated_gd_path(self):
        """Where a check's curated GenomeDiff is written at launch, for `--apply-check`.

        Kilobytes, kept through every ending -- it says what was checked, which the row's
        summary counts but does not reproduce.
        """
        return os.path.join(self.directory(), "curated.gd")

    def producer_key(self):
        """What a read step keys its rows by until it is told which sample they became."""
        return "%s:%s" % (COMPONENT, self.pk)

    def output_dir(self):
        """breseq's ``-o``. **Named for the sample**, which is not decoration.

        ``breseq_folder.find_sample_dirs`` walks looking for ``<dir>/data/output.gd`` and takes
        the sample's name from that directory's basename, so making the output directory the
        sample name is what lets the ingest be `import_samples_into` with no reshaping and no
        second rule about what a sample is called.
        """
        return os.path.join(self.directory(), self.sample_name)

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


#: Where a row of evidence came from: the sample's own import, or a curation check.
ORIGIN_IMPORT = "import"
ORIGIN_CHECK = "check"
ORIGIN_CHOICES = [(ORIGIN_IMPORT, "Import"), (ORIGIN_CHECK, "Curation check")]

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_DISMISSED = "dismissed"


class Evidence(models.Model):
    """One thing breseq saw in a sample's reads that no mutation accounts for.

    breseq's index page lists it as *unassigned evidence*: a missing-coverage region, a new
    junction, a copy-number change, a soft-clipped or discordant pile of reads -- each an
    item of evidence (`MC`, `JC`, `CN`, `SC`, `DP`, `MP`, `PD`) whose id no mutation line
    names in its evidence column and that carries no `reject=`. Core's importer keeps the
    mutations and the MC regions out of a `.gd` and throws the rest away; this table keeps
    exactly that list, per sample, so a curator can see from MutInt what breseq could not
    explain and record what they did about it.

    **A curation check writes the same table from the other side.** breseq re-run with the
    sample's curated mutations applied (`--apply-check`) predicts whatever those mutations
    did not account for, and every entry carries `original_*` coordinates -- the position in
    the ancestor's frame, the one the curated file is in. Its residual *mutations* are rows
    here too (`evidence_type` is then a mutation type, `SNP` or `DEL`), because to a curator
    they are the same thing as a junction nobody explained: a loose end. `origin` says which
    run a row came from, and a check replaces the import's rows outright.

    **Positions are the ancestor's.** `seq_id`/`position`/`end` -- and the second side of a
    junction -- are read from `original_*` where the record carries them, so a row from a
    check lines up with the sample's stored mutations without arithmetic. A position inside
    sequence an applied mutation inserted has no base of its own in the original; breseq
    reports the last original base before it and an `original_<field>_offset`, which stays
    in `record` and is shown as `+N`.

    **Resolved is derived, not stored.** `resolved_mutation` names the curated mutation that
    accounts for the row; the row *is* resolved while that mutation still has a call in this
    sample. Delete the call on core's Delete tab and the row re-opens with no hook; restore
    it and the row is resolved again. The FK is SET_NULL for a swept mutation, which is the
    same answer. A dismissal is stored, with who and why: it is a person's decision about
    the evidence rather than a fact derived from the mutations.
    """

    EVIDENCE_TYPES = ("MC", "JC", "CN", "SC", "DP", "MP", "PD")

    sample = models.ForeignKey("mutint_sample.Sample", on_delete=models.CASCADE,
                               related_name="breseq_evidence")
    # `MC`, `JC`, ... for evidence; a mutation type (`SNP`, `DEL`, ...) for a residual
    # prediction a check made. Three letters either way.
    evidence_type = models.CharField(max_length=3)
    seq_id = models.CharField(max_length=200)
    position = models.IntegerField()
    end = models.IntegerField(null=True, blank=True)
    # Side 2 of a junction-shaped item (`JC`, `DP`, `PD`); null for everything else.
    seq_id_2 = models.CharField(max_length=200, blank=True, default="")
    position_2 = models.IntegerField(null=True, blank=True)
    # breseq's own entry id, which names its page in the stored report:
    # `evidence.html#JC_<id>.html`, `#MC_PLOT_<id>.html` (`views._PAGE_PREFIX`). Null for a
    # record with no id.
    source_id = models.IntegerField(null=True, blank=True)
    origin = models.CharField(max_length=8, choices=ORIGIN_CHOICES, default=ORIGIN_IMPORT)
    # The record's attributes, verbatim -- `original_*` and `*_offset` included -- the way
    # `Mutation.genome_diff` keeps a mutation's. Read whole, per row, to draw a summary.
    record = models.JSONField(default=dict)

    resolved_mutation = models.ForeignKey("mutint_sample.Mutation", null=True, blank=True,
                                          on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismissed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    dismiss_note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["seq_id", "position", "pk"]
        verbose_name_plural = "evidence"

    def __str__(self):
        return "%s %s:%s (sample %s)" % (self.evidence_type, self.seq_id, self.position,
                                         self.sample_id)

    @property
    def is_junction(self):
        return self.evidence_type in ("JC", "DP", "PD")

    @property
    def is_dismissed(self):
        return self.dismissed_at is not None

    @property
    def is_resolved(self):
        """Whether the mutation this was resolved against still has a call in the sample.

        One query per row, asked only where a page draws the row; the list view asks it in
        bulk through `evidence.statuses_for`.
        """
        if self.resolved_mutation_id is None:
            return False
        from mutint_sample.models import MutationCall
        return MutationCall.objects.filter(
            sample_id=self.sample_id, mutation_id=self.resolved_mutation_id).exists()

    @property
    def status(self):
        if self.is_dismissed:
            return STATUS_DISMISSED
        return STATUS_RESOLVED if self.is_resolved else STATUS_OPEN

    def match_key(self):
        """What a check matches a row of the last run on, to carry a dismissal forward."""
        return (self.evidence_type, self.seq_id, self.position, self.end,
                self.seq_id_2, self.position_2)
