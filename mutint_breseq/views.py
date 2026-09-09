"""The launcher page, the launch endpoint, and serving what a run left behind.

Shaped like every other page in the suite: function-based views, permission checked inline,
hand-written Bootstrap posting to a `@require_POST` JSON endpoint through `mutintPostJson`. No
Django ``Form`` classes -- there are two fields and a file drop.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

import mutint_sample.views.common
from mutint_common import store
from mutint_common.fileserve import serve_file
from mutint_common.util import get_user_context
from mutint_experiment.models import Experiment
from mutint_experiment.permissions import (
    can_edit_experiment,
    can_view_project,
    experiment_lock_refusal,
)
from mutint_common.tools import ToolMissing
from mutint_import import reference_store, sample_names, staging
from mutint_jobs import jobs as jobs_api
from mutint_jobs import processes
from mutint_import.upload_session import UploadError

from mutint_breseq import runner, tasks
from mutint_breseq.models import (
    COMPONENT,
    STATUS_QUEUED,
    STATUS_RUNNING,
    BreseqRun,
)

logger = logging.getLogger("mutint_breseq.views")

# What a sample may be called. Deliberately narrow, and it is doing two jobs at once: this
# string becomes a **directory name** under the store, and it is what
# `mutint_import.sample_names.parse_sample_identity` reads the ALE, flask and isolate out of.
# Both shapes that parser understands -- `3-30000-1-1` and `Ara-2_500gen_763A` -- fit inside
# it, and nothing with a separator, a space or a leading dot does.
SAMPLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")

MAX_ARGUMENTS_CHARS = 2000


def _experiment_or_none(request):
    try:
        return mutint_sample.views.common.get_experiment(request)
    except (Experiment.DoesNotExist, ValueError):
        return None


@ensure_csrf_cookie
def breseq(request):
    """The launcher: drop reads, name the sample, run breseq. Lists this experiment's runs.

    Signed in first, and only then the experiment. Running breseq is creating data, so the rule
    is the one `project_new` and `experiment_new` state: you must be somebody. It is checked
    before the experiment is resolved, so a signed-out visitor is told the actual reason rather
    than being sent to a page about an experiment they were never going to be able to use.
    """
    if not request.user.is_authenticated:
        return render(request, "403.html", get_user_context(request.user), status=403)

    context = get_user_context(request.user)
    try:
        experiment = mutint_sample.views.common.get_experiment(request)
    except Experiment.DoesNotExist:
        return mutint_sample.views.common.no_experiment_selected(
            request, context, logger, "breseq")

    context.update(experiment.experiment_context())
    context.update({
        "experiment": experiment,
        "experiment_id": experiment.id,
        # The form is replaced by a banner without one. A reference is not a nicety here: it
        # is breseq's `-r`, so there is nothing to call mutations against and a Launch button
        # would refuse every time it was pressed.
        "has_reference": reference_store.has_reference(experiment),
        # Governs whether the form renders at all, and carries the lock: this page writes a
        # sample everybody sees, so a locked experiment offers no form.
        "can_launch": can_edit_experiment(request.user, experiment),
        "lock_refusal": experiment_lock_refusal(experiment),
        "component": COMPONENT,
        "runs": _run_rows(experiment),
        # What the Population and Time point boxes offer. Put in the context rather than
        # fetched, which is what every other picker in the suite does -- and these change
        # only when a sample is imported, which reloads the page anyway.
        "population_names": mutint_sample.views.common.get_population_names(experiment.id),
        "time_points": mutint_sample.views.common.get_time_points(experiment.id),
        # For the "this replaces an existing sample" warning; see `_existing_samples`.
        "existing_samples": _existing_samples(experiment),
    })
    return render(request, "breseq/launch.html", context)


def _supersede_in_flight(experiment, sample_name, user):
    """Stop any run already queued or running for this sample. Returns how many were stopped.

    Two runs writing one sample race, and the loser is whichever finishes first: the import
    supersedes a sample's calls rather than adding to them, so the older run's output is
    about to be overwritten whatever happens. Letting it finish costs hours of CPU to produce
    something the new run discards.

    **Cooperative, like every other cancellation here.** The flag is set through
    `mutint_jobs`; a running task sees it between slices of breseq's output, and one still
    queued sees it at entry -- `request_cancel` deliberately never touches the queue row. So
    this asks, and the task is what records the cancellation and throws the work away.
    """
    in_flight = BreseqRun.objects.filter(
        experiment=experiment, sample_name=sample_name,
        status__in=(STATUS_QUEUED, STATUS_RUNNING)).exclude(task_result_id="")
    stopped = jobs_api.request_cancel_for(
        list(in_flight.values_list("task_result_id", flat=True)), by=user)
    if stopped:
        logger.info("superseding %d in-flight breseq run(s) for %s in experiment %s",
                    stopped, sample_name, experiment.id)
    return stopped


def _existing_samples(experiment):
    """The coordinates and names a new run could land on top of.

    **A collision is not refused and does not make a second sample: it supersedes the first.**
    `gd_import.import_document_as_sample` reuses the sample at a coordinate and
    `_database_gd_mutations` clears its calls before writing the new ones -- which is how a
    corrected breseq run replaces the one it supersedes, and is a thing people do on purpose.
    So the page warns and does not block; this is what it warns from.

    Both keys, because the importer uses both: a placed name matches on the coordinate, and a
    name carrying none matches on `source_name`.

    The ancestor is included -- `include_ancestor=True` -- because a collision with it is
    still a collision, and it is the one sample every listing otherwise hides.
    """
    from mutint_experiment.coordinates import format_time_point
    from mutint_sample.util import get_ordered_sample_queryset

    rows = []
    for sample in (get_ordered_sample_queryset(experiment.id, include_ancestor=True)
                   .select_related("population")):
        rows.append({
            "population": sample.population.name if sample.population_id else "",
            "time_point": ("" if sample.time_point is None
                           else str(format_time_point(sample.time_point))),
            "sample": sample.name,
            "source_name": sample.source_name or "",
            "label": sample.label,
        })
    return rows


def _queue_status(run):
    """What the queue thinks, or "" when there is nothing to ask.

    This is the whole reason `task_result_id` is a column. A row saying `queued` is either a
    job a worker has not reached yet or a job **no worker will ever reach**, and those look
    identical from the row alone -- so the page asks. `db_worker` not running is this design's
    easiest failure by a distance, and the one a person is least equipped to guess at.
    """
    if not run.task_result_id or run.is_finished:
        return ""
    try:
        return tasks.run_breseq.get_result(run.task_result_id).status.value
    except Exception:
        # A result the backend has reaped, or a backend that cannot answer. Neither is worth
        # a broken page -- the row's own status is still true, just less specific.
        return ""


def _run_rows(experiment):
    runs = list(BreseqRun.objects.filter(experiment=experiment).select_related("sample"))
    # One query for every run's log link, through core rather than by querying `Job` here: a
    # plugin holds the queue's result id, which is what it was handed, and `mutint_jobs` owns
    # the mapping from that to a page. Runs whose job has written nothing are simply absent.
    log_urls = jobs_api.log_urls(run.task_result_id for run in runs)

    rows = []
    for run in runs:
        rows.append({
            "id": run.pk,
            "sample_name": run.sample_name,
            "arguments": run.arguments,
            "read_files": run.read_files,
            "trim_reads": run.trim_reads,
            "status": run.status,
            "queue_status": _queue_status(run),
            "created_at": run.created_at.isoformat(),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "error": run.error,
            "log": run.log,
            "sample_id": run.sample_id,
            # Core's viewer for the sample this run produced, not a route of our own. The
            # report is stored under the sample by mutint-core's importer, so the link exists
            # exactly when the sample does.
            "report_url": ("/mutations/report/%d/" % run.sample_id
                           if run.sample_id else None),
            # What the run's tools printed, whole and while they are still printing it. The
            # `log` field beside this is the tail, and only after the run has finished.
            "log_url": log_urls.get(run.task_result_id, ""),
        })
    return rows


def runs(request):
    """The run list as JSON, for the page's poll."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "You must be signed in."}, status=403)

    experiment = _experiment_or_none(request)
    if experiment is None:
        return JsonResponse({"error": "Unknown experiment."}, status=404)
    return JsonResponse({"runs": _run_rows(experiment)})


#: One FASTQ record, written only so `--dry-run` has an input file to find.
#:
#: breseq checks that every input file *exists*; it does not read them under `--dry-run`, and a
#: minimal record is measured to satisfy it. A genuinely absent path would fail the check
#: rather than pass it, which is why this is written rather than invented.
PREFLIGHT_FASTQ = "@preflight\nACGTACGTAC\n+\nIIIIIIIIII\n"

#: The preflight is option parsing and a handful of `stat` calls -- measured in milliseconds.
#: A minute is not a budget, it is a guard against a breseq that never returns holding up a
#: web request.
PREFLIGHT_TIMEOUT_SECONDS = 60


def _preflight(experiment, arguments):
    """Ask breseq whether it would accept this command line. Returns a refusal, or None.

    **Before the upload is claimed**, which is the whole point of doing it here: a rejected
    launch must cost the person nothing, and by the time the reads have been moved out of
    staging the only way to try again is to upload them again. So this names a throwaway FASTQ
    of its own rather than the reads -- see `PREFLIGHT_FASTQ`.

    The worker checks again before it trims (`tasks.run_breseq`), and that one is not
    redundant: options cannot change in between, but `db_worker` may be on another host, and
    the dry run is also what asks whether bowtie2, samtools and gnuplot are there. This one
    asks about the box; that one asks about the machine.
    """
    try:
        breseq = runner.breseq_path()
    except ToolMissing as missing:
        return str(missing)

    reference = store.experiment_reference_path(experiment.id, store.REFERENCE_GFF3)

    with tempfile.TemporaryDirectory() as scratch:
        reads = os.path.join(scratch, "preflight.fastq")
        with open(reads, "w") as handle:
            handle.write(PREFLIGHT_FASTQ)
        argv = runner.build_argv(breseq, os.path.join(scratch, "output"), reference,
                                 arguments, [reads],
                                 processors=runner.default_processors(), dry_run=True)
        # `run_tool` writes to a file and hands back a returncode, so the output is captured by
        # giving it one in the scratch directory. A launch has no job and so no job log, and
        # this needs no capture mode in core.
        log_path = os.path.join(scratch, "preflight.log")
        try:
            with open(log_path, "wb") as log:
                returncode = processes.run_tool(
                    argv, log, env=runner.tool_environment(),
                    timeout=PREFLIGHT_TIMEOUT_SECONDS, what="breseq --dry-run")
        except subprocess.TimeoutExpired:
            return ("breseq did not answer within %d seconds when asked to check these "
                    "options." % PREFLIGHT_TIMEOUT_SECONDS)
        except OSError as exc:
            return "breseq could not be started: %s" % exc

        if returncode == 0:
            return None

        with open(log_path, "r") as handle:
            output = handle.read()

    logger.info("breseq refused a command line for experiment %s: %r",
                experiment.id, arguments)
    return ("breseq will not accept this command line:\n\n%s"
            % (runner.refusal_from(output) or "it exited %d without saying why." % returncode))


@require_POST
def launch(request):
    """Take a staged drop and start a run.

    Body: {upload_id, sample_name, arguments, trim_reads}; `trim_reads` defaults to true.

    Gated on `can_edit_experiment` and **not** `can_edit_project`: what this eventually writes
    is a sample everybody sees, so a locked experiment has to refuse it, and a predicate handed
    the project cannot see a flag on the experiment. That is the call the suite's CLAUDE.md
    notes no plugin had yet had to make. Signed in as well, stated rather than left to be
    inferred from three files -- see `mutint_import.staging.create_staging_session`.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "You must be signed in."}, status=403)

    experiment = _experiment_or_none(request)
    if experiment is None:
        return JsonResponse({"error": "Unknown experiment."}, status=404)
    if not can_edit_experiment(request.user, experiment):
        return JsonResponse(
            {"error": experiment_lock_refusal(experiment)
                      or "You cannot add data to this experiment."}, status=403)

    if not reference_store.has_reference(experiment):
        return JsonResponse(
            {"error": "This experiment has no reference genome yet. breseq calls mutations "
                      "against one, so establish it on the Add Data page first."}, status=400)

    payload = _payload(request)

    # **The three parts, not a joined name.** How a population, a time point and a sample
    # become one string is `sample_names.compose_sample_name`'s to decide -- the form sends
    # what it knows and the convention can change without the form changing with it. That
    # composer also refuses the combinations that cannot be a name, and says which field is
    # at fault so the page can point at it.
    try:
        sample_name = sample_names.compose_sample_name(
            payload.get("population"), payload.get("time_point"), payload.get("sample"))
    except sample_names.SampleNameError as refusal:
        return JsonResponse({"error": str(refusal), "field": refusal.field}, status=400)

    if not SAMPLE_NAME_RE.match(sample_name):
        # The composer keeps each part inside this pattern, so reaching here means the parts
        # were individually fine and the whole is not -- a name past 100 characters.
        return JsonResponse(
            {"error": "That name is too long: %d characters, and the limit is 100. It "
                      "becomes a directory name as well as this sample's name."
                      % len(sample_name), "field": "sample"}, status=400)

    arguments = (payload.get("arguments") or "").strip()
    if len(arguments) > MAX_ARGUMENTS_CHARS:
        return JsonResponse({"error": "That is a very long argument list."}, status=400)
    try:
        # Parsed now rather than in the worker, so an unbalanced quote is a message beside the
        # box rather than a run that fails four hours later having done nothing.
        runner.split_arguments(arguments)
    except ValueError as exc:
        return JsonResponse({"error": "Those arguments could not be read: %s" % exc},
                            status=400)

    # Asked of breseq itself, and asked here rather than after the upload is claimed so that a
    # typo costs nothing: no run row, no reads moved, the session still open to launch again.
    refusal = _preflight(experiment, arguments)
    if refusal:
        return JsonResponse({"error": refusal}, status=400)

    session, error = staging.session_for(request, payload.get("upload_id"), COMPONENT)
    if error:
        return error
    if session.experiment_id != experiment.id:
        # Two ids arrive from the client and nothing else stops one experiment's session being
        # launched against another's reference.
        return JsonResponse({"error": "That upload belongs to another experiment."},
                            status=409)

    try:
        staged_root = staging.claim(session)
    except UploadError as exc:
        return JsonResponse({"error": str(exc)}, status=409)

    # Before the new row exists, so it cannot cancel itself. Anything already in flight for
    # this sample is about to have its output overwritten -- see `_supersede_in_flight`.
    superseded = _supersede_in_flight(experiment, sample_name, request.user)

    run = BreseqRun.objects.create(
        experiment=experiment,
        created_by=request.user if request.user.is_authenticated else None,
        sample_name=sample_name,
        arguments=arguments,
        trim_reads=bool(payload.get("trim_reads", True)),
        status=STATUS_QUEUED)

    try:
        reads = _take_reads(staged_root, run)
    except Exception as exc:
        logger.exception("could not stage reads for breseq run %s", run.pk)
        staging.abandon(session)
        run.delete()
        return JsonResponse({"error": "The uploaded reads could not be stored: %s" % exc},
                            status=500)

    if not reads:
        staging.abandon(session)
        run.delete()
        return JsonResponse({"error": "No read files were uploaded."}, status=400)

    run.read_files = reads
    run.save(update_fields=["read_files"])
    # The staged copy is gone by here; the reads live under the run's own directory, which the
    # run's post_delete receiver owns. Nothing is left for core's reaper to be racing.
    staging.close(session)

    # Through mutint_jobs rather than `task.enqueue` directly, which is what puts the run on
    # /jobs/ with a name and an owner and makes it stoppable. `cancellable=True` is a promise
    # the task keeps -- see mutint_jobs.processes.run_tool, which polls while the tool runs.
    job = jobs_api.enqueue(
        tasks.run_breseq, run.pk,
        user=request.user,
        label="breseq \u2014 %s" % run.sample_name,
        component=COMPONENT,
        experiment=experiment,
        cancellable=True)
    run.task_result_id = job.task_result_id
    run.save(update_fields=["task_result_id"])

    return JsonResponse({"run_id": run.pk, "superseded": superseded,
                         "runs": _run_rows(experiment)})


def _take_reads(staged_root, run):
    """Move every staged file into the run's own reads directory. Returns their basenames.

    Flattened deliberately: a drop may arrive with directory structure (a run folder from a
    sequencing core), and breseq takes read files positionally with no notion of where they
    sat. Basenames are made unique by the directories they came from, so two lanes' `R1.fastq`
    do not collide.
    """
    reads_dir = store.ensure_dir(run.reads_dir())
    names = []
    for dirpath, _dirnames, filenames in os.walk(staged_root):
        for filename in sorted(filenames):
            source = os.path.join(dirpath, filename)
            relative = os.path.relpath(source, staged_root)
            flat = relative.replace(os.sep, "__")
            shutil.move(source, os.path.join(reads_dir, flat))
            names.append(flat)
    return sorted(names)


def _payload(request):
    try:
        return json.loads((request.body or b"{}").decode("utf-8")) or {}
    except (ValueError, UnicodeDecodeError):
        return {}


@require_POST
def run_delete(request, pk):
    """Forget a run and remove its files. The row's post_delete receiver does the second."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "You must be signed in."}, status=403)

    run = BreseqRun.objects.filter(pk=pk).select_related("experiment").first()
    if run is None:
        return JsonResponse({"error": "Unknown run."}, status=404)
    if not can_edit_experiment(request.user, run.experiment):
        return JsonResponse(
            {"error": experiment_lock_refusal(run.experiment)
                      or "You cannot change this experiment."}, status=403)

    if not run.is_finished:
        # Refused, because deleting is destructive in a way that is invisible from here: the
        # post_delete receiver rmtrees the run directory, which for a *running* run pulls the
        # reads and the output out from under the live subprocess. breseq then fails on its
        # own, minutes later, with an error naming neither the cause nor the person who caused
        # it. Cancelling stops it properly and leaves the row deletable.
        return JsonResponse(
            {"error": "That run has not finished. Cancel it on the Jobs page first, then "
                      "delete it."}, status=409)

    # Deliberately does not delete the imported Sample. A run is a record of how a sample was
    # made; removing that record must not remove the data, which is deleted through the sample
    # editor by somebody who means to.
    experiment = run.experiment
    run.delete()
    return JsonResponse({"deleted": True, "runs": _run_rows(experiment)})
