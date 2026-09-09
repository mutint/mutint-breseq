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
from mutint_import import reference_store, staging
from mutint_jobs import jobs as jobs_api
from mutint_import.upload_session import UploadError

from mutint_breseq import runner, tasks
from mutint_breseq.models import (
    COMPONENT,
    STATUS_QUEUED,
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
    })
    return render(request, "breseq/launch.html", context)


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

    sample_name = (payload.get("sample_name") or "").strip()
    if not SAMPLE_NAME_RE.match(sample_name):
        return JsonResponse(
            {"error": "A sample name may use letters, digits, dot, underscore, plus and "
                      "hyphen, and must start with a letter or digit. It becomes this "
                      "sample's name everywhere in MutInt."}, status=400)

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

    return JsonResponse({"run_id": run.pk, "runs": _run_rows(experiment)})


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
