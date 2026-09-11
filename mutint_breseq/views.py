"""The launcher page, the launch endpoint, and serving what a run left behind.

Shaped like every other page in the suite: function-based views, permission checked inline,
hand-written Bootstrap posting to a `@require_POST` JSON endpoint through `mutintPostJson`. No
Django ``Form`` classes -- there are two fields and a file drop.
"""

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile

from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

import mutint_sample.views.common
from mutint_common import preferences, store
from mutint_common.fileserve import serve_file
from mutint_common.util import get_user_context
from mutint_experiment.models import Experiment
from mutint_experiment.permissions import (
    can_edit_experiment,
    can_view_project,
    experiment_lock_refusal,
)
from mutint_common.tools import ToolMissing
from mutint_import import accessions, reference_store, sample_names, sra, sra_fetch, staging
from mutint_jobs import jobs as jobs_api
from mutint_jobs import processes
from mutint_import.upload_session import UploadError

from mutint_breseq import read_names, runner, tasks
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
# it, and nothing with a separator or a leading dot does.
#
# **A space is allowed**, matching `sample_names._NAME_PART`, which stopped forbidding one for
# the reason recorded there: the parser, the columns and the sample editor never did. A space
# in a directory name is harmless here because `runner.build_argv` produces a list and nothing
# reaches a shell. A *trailing* space would not be harmless -- it is invisible and some
# filesystems drop it -- so the name is stripped before it is matched, and the anchor refuses a
# leading one.
SAMPLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+ -]{0,99}$")

MAX_ARGUMENTS_CHARS = 2000

#: How the form said what this sample is called. Three shapes of the same question, and the
#: menu on the page is the only thing that decides between them.
#:
#: `MODE_PARTS` is the default because it is the contract every existing caller posts, and
#: because a mode nobody named should be the one that composes rather than the one that trusts
#: a string.
MODE_NAME = "name"
MODE_PARTS = "parts"
MODE_READ_NAMES = "read_names"
INPUT_MODES = (MODE_NAME, MODE_PARTS, MODE_READ_NAMES)


#: Which way in this reader used last. One key rather than one per experiment: which half of
#: the form somebody fills in is a habit, and a per-experiment key would start every new
#: experiment remembering nothing. The same reasoning as `import.tab` in core.
INPUT_MODE_PREFERENCE = "breseq.input_mode"


def _embedded_preferences(user):
    """This page's stored choices, for the script to read before its first draw.

    Only for a signed-in reader -- an anonymous one gets localStorage from the client half of
    the store, which is the arrangement `mutint_preferences.js` documents. Absent means "never
    chosen", which the page turns into the default mode rather than into nothing.
    """
    if not user.is_authenticated:
        return {}
    stored = preferences.get_preference(user, INPUT_MODE_PREFERENCE)
    return {} if stored is None else {INPUT_MODE_PREFERENCE: stored}


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
        # Everything the page's script needs, in one `json_script` element. It used to
        # interpolate `experiment_id` and `component` into an inline `<script>`; the script is
        # a static file now and the template engine does not reach it.
        "launch_config": {
            "experiment_id": experiment.id,
            "component": COMPONENT,
            # The Input type menu is remembered per person through core's preference store,
            # the way the Import data page remembers its tab. Embedded so the first draw is
            # already the mode they last used rather than flicking to it after a fetch.
            "authenticated": request.user.is_authenticated,
            "preferences_url": reverse("preferences"),
            "preferences": _embedded_preferences(request.user),
        },
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
            # Which accessions this run fetched reads for, as `{typed, kind, runs: [...]}`
            # -- the plans it was launched with, for the detail row to name.
            "accessions": [{"typed": plan.get("typed", ""), "kind": plan.get("kind", ""),
                            "runs": [run_entry.get("accession", "")
                                     for run_entry in plan.get("runs") or []]}
                           for plan in (run.accessions or [])],
            "trim_reads": run.trim_reads,
            "population_sample": run.population_sample,
            "coverage_limit": run.coverage_limit,
            "status": run.status,
            "queue_status": _queue_status(run),
            "created_at": run.created_at.isoformat(),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "error": run.error,
            # Things worth reading about a run that *worked*, so they are carried separately
            # from `error` and shown whatever the status is.
            "notes": run.notes or [],
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


def _check_name_is_usable(name, field):
    """Raise SampleNameError unless `name` can be this sample's name and its directory.

    The one check both single-sample modes share, and the only thing standing between a typed
    string and a path under the store.
    """
    if not SAMPLE_NAME_RE.match(name):
        if len(name) > 100:
            raise sample_names.SampleNameError(
                field,
                "That name is too long: %d characters, and the limit is 100. It becomes a "
                "directory name as well as this sample's name." % len(name))
        raise sample_names.SampleNameError(
            field,
            "A sample name may use letters, digits, spaces, dot, underscore, plus and hyphen, "
            "and must start with a letter or digit. It becomes a directory name as well as "
            "this sample's name.")
    return name


def _single_sample_name(mode, payload):
    """The one sample's name, from whichever half of the form was filled in.

    **The two modes take different contracts, and that is the point of having modes.**

    `MODE_PARTS` posts a population, a time point and a sample, and
    `sample_names.compose_sample_name` decides how the three become one string -- so that
    convention can change without this form changing with it, and the composer refuses the
    combinations no name can spell while saying which field is at fault.

    `MODE_NAME` posts the name itself and it is stored **verbatim**. Composing it would mean
    rebuilding a name from the coordinate read out of it, which is not a round trip: `3-30000-1-1`
    parses to population 3, time point 30000, label `1-1` and composes back to `3_30000_1-1`,
    so a person who typed a perfectly good A-F-I-R name got a different one. A mode called
    *metadata from a name* has to leave the name alone; the coordinate is read from it by the
    importer exactly as it would be from a dropped folder of that name.
    """
    if mode == MODE_NAME:
        name = (payload.get("sample_name") or "").strip()
        if not name:
            raise sample_names.SampleNameError("sample_name", "A sample name is required.")
        return _check_name_is_usable(name, "sample_name")

    name = sample_names.compose_sample_name(
        payload.get("population"), payload.get("time_point"), payload.get("sample"))
    # The composer keeps each part inside this pattern, so reaching here means the parts were
    # individually fine and the whole is not -- a name past 100 characters.
    return _check_name_is_usable(name, "sample")


class CoverageLimitError(Exception):
    """The Limit coverage box did not hold a number this can be run with."""


def _coverage_limit(raw):
    """The Limit coverage box as a float, or None for blank. Raises CoverageLimitError.

    **Blank is a value and it is the default**: no limit, every read, which is what breseq
    does when `-l` is absent. So the empty box has to be told apart from a bad one rather than
    both collapsing to "nothing to do".

    `float()` is not the whole check. It accepts `nan` and `inf`, which would reach breseq's
    command line as words and be refused hours later by a worker rather than here; and a
    limit of zero or less is a number that means nothing anybody wants.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise CoverageLimitError(
            "Limit coverage has to be a number, or blank to use every read.")
    if not math.isfinite(value) or value <= 0:
        raise CoverageLimitError(
            "Limit coverage has to be greater than zero, or blank to use every read.")
    return value


def _preflight(experiment, arguments, polymorphism=False, coverage_limit=None):
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
                                 processors=runner.default_processors(), dry_run=True,
                                 polymorphism=polymorphism, coverage_limit=coverage_limit)
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
    """Take a staged drop, a list of SRA accessions, or both, and start a run.

    Body: {upload_id, accessions, input_mode, sample_name | population, time_point, sample,
    arguments, trim_reads, population_sample, coverage_limit}; `trim_reads` defaults to true,
    `population_sample` to false, and `coverage_limit` is blank for every read. `upload_id`
    is blank when nothing was dropped -- the page opens no session for a launch that has no
    bytes to stage -- and `accessions` is blank when nothing was typed; one of them is not.

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

    mode = payload.get("input_mode") or MODE_PARTS
    if mode not in INPUT_MODES:
        # Named rather than fallen back from: a mode this does not know is a page and a server
        # that disagree about what was asked for, and guessing "parts" would silently launch
        # one sample for a drop somebody meant as ten.
        return JsonResponse(
            {"error": "Unknown input type %r." % mode, "field": "input_mode"}, status=400)

    if mode != MODE_READ_NAMES:
        try:
            sample_name = _single_sample_name(mode, payload)
        except sample_names.SampleNameError as refusal:
            return JsonResponse({"error": str(refusal), "field": refusal.field}, status=400)

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

    # The Population sample checkbox. It decides two things at once -- `-p` on breseq's
    # command line, and the clonality the imported sample is recorded with -- so it is read
    # here, before the preflight, and stored on the row rather than derived later from the
    # arguments string.
    population_sample = bool(payload.get("population_sample", False))

    # Checked here rather than left to breseq, which takes `-l notanumber` past its own dry
    # run -- measured -- and fails on it hours later. `field` so the page can point at the box.
    try:
        coverage_limit = _coverage_limit(payload.get("coverage_limit"))
    except CoverageLimitError as refusal:
        return JsonResponse({"error": str(refusal), "field": "coverage_limit"}, status=400)

    # SRA accessions, resolved against ENA now and not in the worker, for the reason the
    # preflight below runs before the claim: a typo should cost the round trip that caught it
    # and nothing else -- no run row, no reads moved, the session still open. After the
    # permission check, deliberately: a visitor who may not write here must not be able to
    # make the installation ask ENA on their behalf. What is stored is the resolved plan, so
    # the worker downloads what this resolved rather than forming a second opinion.
    try:
        plans = _resolve_accessions(payload.get("accessions"))
    except (accessions.AccessionError, sra_fetch.FetchError) as refusal:
        return JsonResponse({"error": str(refusal), "field": "accessions"}, status=400)

    # Asked of breseq itself, and asked here rather than after the upload is claimed so that a
    # typo costs nothing: no run row, no reads moved, the session still open to launch again.
    refusal = _preflight(experiment, arguments, polymorphism=population_sample,
                         coverage_limit=coverage_limit)
    if refusal:
        return JsonResponse({"error": refusal}, status=400)

    # **No session is no files, and is not an error.** The page opens a staging session only
    # when something was dropped: a session exists to receive bytes, and a launch whose reads
    # are all fetched by accession has none to stage. So every refusal from here on abandons
    # a session only if there is one, and "nothing at all" is refused below in one sentence.
    session = None
    staged = {}
    upload_id = (payload.get("upload_id") or "").strip()
    if upload_id:
        session, error = staging.session_for(request, upload_id, COMPONENT)
        if error:
            return error
        if session.experiment_id != experiment.id:
            # Two ids arrive from the client and nothing else stops one experiment's session
            # being launched against another's reference.
            return JsonResponse({"error": "That upload belongs to another experiment."},
                                status=409)

        try:
            staged_root = staging.claim(session)
        except UploadError as exc:
            return JsonResponse({"error": str(exc)}, status=409)
        staged = _staged_files(staged_root)

    def abandon():
        if session is not None:
            staging.abandon(session)

    if not staged and not plans:
        abandon()
        return JsonResponse(
            {"error": "Drop read files, or type an SRA accession."}, status=400)

    # A dropped file may not sit where a download is about to land: both go into one
    # `reads/`, and the download would overwrite the drop. Named now, while the answer is
    # still "rename it", rather than as a checksum failure hours later.
    clashing = sorted(set(staged) & set(sra.filenames_by_run(plans)))
    if clashing:
        abandon()
        return JsonResponse(
            {"error": "%s is also the name of a file %s would download. Rename it, or "
                      "drop it on its own." % (clashing[0], _run_for(plans, clashing[0])),
             "field": "upload"}, status=409)

    # The plan: one (sample, its files, the accession plans whose runs are its) per run row.
    # In the two single-sample modes everything -- dropped and fetched alike -- is that one
    # sample's reads; in read-names mode each accession is a sample of its own beside the
    # samples the dropped names derive, named by ENA's alias or by the accession.
    if mode == MODE_READ_NAMES:
        paired = "--no-paired-mapping" not in runner.split_arguments(arguments)
        plan = []
        for sample in read_names.derive_samples(sorted(staged), paired=paired):
            try:
                _check_name_is_usable(sample.name, "upload")
            except sample_names.SampleNameError as refusal:
                abandon()
                return JsonResponse(
                    {"error": "%s could not be a sample name: %s" % (sample.name, refusal),
                     "field": "upload"}, status=400)
            plan.append((sample, []))
        plan.extend(_accession_samples(plans))
        clash = _name_clash(plan)
        if clash:
            abandon()
            return JsonResponse({"error": clash, "field": "accessions"}, status=400)
    else:
        plan = [(read_names.DerivedSample(
                    sample_name, sorted(staged) + sorted(sra.filenames_by_run(plans))),
                 [p.as_dict() for p in plans])]

    # **Every name superseded before any row exists.** Called inside the loop below, run two's
    # supersede would cancel run one from this same launch -- it matches on the sample name and
    # knows nothing about which launch a row came from.
    superseded = sum(_supersede_in_flight(experiment, sample.name, request.user)
                     for sample, _plans in plan)

    runs = []
    try:
        for sample, plan_dicts in plan:
            run = BreseqRun.objects.create(
                experiment=experiment,
                created_by=request.user if request.user.is_authenticated else None,
                sample_name=sample.name,
                arguments=arguments,
                trim_reads=bool(payload.get("trim_reads", True)),
                population_sample=population_sample,
                coverage_limit=coverage_limit,
                accessions=plan_dicts,
                status=STATUS_QUEUED)
            runs.append(run)
            # Only the dropped share is moved; the rest is fetched by the worker into the
            # same directory. `read_files` lists both, being what a person is shown.
            taken = _take_reads(staged, run, [name for name in sample.files if name in staged])
            run.read_files = sorted(set(taken) | set(sample.files))
            run.save(update_fields=["read_files"])
    except Exception as exc:
        # **All of them, not the one that failed.** Each row owns its own directory and its
        # post_delete receiver takes the files with it, so unwinding the whole launch leaves
        # nothing behind -- and a half-launched drop is worse than none, because the samples
        # that did start would have to be found and cancelled by hand.
        logger.exception("could not stage reads for a breseq launch in experiment %s",
                         experiment.id)
        for run in runs:
            run.delete()
        abandon()
        return JsonResponse({"error": "The uploaded reads could not be stored: %s" % exc},
                            status=500)

    # The staged copy is gone by here; the reads live under each run's own directory, which
    # that run's post_delete receiver owns. Nothing is left for core's reaper to be racing.
    if session is not None:
        staging.close(session)

    for run in runs:
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

    return JsonResponse({"run_ids": [run.pk for run in runs], "superseded": superseded,
                         "runs": _run_rows(experiment)})


#: A drop bigger than this is not previewed row by row. Nothing breaks past it -- the launch
#: itself is unbounded -- but a table of a thousand rows is not something anybody reads, and
#: the request that builds it is on the page's critical path.
MAX_PREVIEW_FILES = 500


@require_POST
def preview(request):
    """What a drop of read files, or a list of accessions, would be read as -- before a byte
    of it is uploaded or downloaded.

    Body: `{names: [...], accessions, arguments}`. Answers one row per sample the drop would
    produce: its name, the files that make it, the coordinate the importer will read out of
    that name, and the sample it would replace. Rows for accessions carry what ENA said as
    well -- the alias and title the name came from, the runs and their size -- so a person
    can see that the accession is the one they meant before hours are spent on it.

    **This is why there is no JavaScript copy of the derivation.** The suite already carries
    one such copy -- `mutint_sample_names.js` -- and its own agreement test states the cost: it
    can check that the two *specifications* match and never that the JS implementation matches
    its own table. That copy earns its place because a name box needs an answer per keystroke.
    A file drop is a discrete event, so it can afford a round trip, and one derivation with two
    callers cannot drift from itself.

    No session, no upload, no state: names in, rows out. It is gated all the same, because the
    reply names the experiment's existing samples.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "You must be signed in."}, status=403)

    experiment = _experiment_or_none(request)
    if experiment is None:
        return JsonResponse({"error": "Unknown experiment."}, status=404)
    if not can_view_project(request.user, experiment.project):
        return JsonResponse({"error": "You cannot see this experiment."}, status=403)

    payload = _payload(request)
    typed_accessions = (payload.get("accessions") or "").strip()
    if typed_accessions and not can_edit_experiment(request.user, experiment):
        # Resolving talks to ENA. A reader may see what this experiment holds; making the
        # installation ask another service on their behalf is a writer's privilege, the rule
        # `upload_session.create_upload_session` states for NCBI.
        return JsonResponse(
            {"error": experiment_lock_refusal(experiment)
                      or "You cannot add data to this experiment."}, status=403)

    names = payload.get("names") or []
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        return JsonResponse({"error": "Expected a list of file names."}, status=400)
    if len(names) > MAX_PREVIEW_FILES:
        return JsonResponse(
            {"error": "That is %d files; the preview stops at %d."
                      % (len(names), MAX_PREVIEW_FILES)}, status=400)

    try:
        typed = runner.split_arguments(payload.get("arguments") or "")
    except ValueError:
        # The arguments box is refused properly at launch; here an unreadable one only means
        # the pairing rule cannot be told which way to group, so it takes the default.
        typed = []
    paired = "--no-paired-mapping" not in typed

    from mutint_experiment.coordinates import format_time_point

    # The names the *worker* will see, which is what the derivation runs over. A drop with
    # folders in it flattens, and a preview over the unflattened names would promise samples
    # named for files that will not exist by then.
    flat = [name.replace("/", "__").replace(os.sep, "__") for name in names]

    try:
        plans = _resolve_accessions(typed_accessions)
    except (accessions.AccessionError, sra_fetch.FetchError) as refusal:
        return JsonResponse({"error": str(refusal), "field": "accessions"}, status=400)

    existing = _existing_samples(experiment)
    rows = []
    for sample in read_names.derive_samples(sorted(flat), paired=paired):
        rows.append(_preview_row(existing, sample))
    for sample, plan_dicts in _accession_samples(plans):
        row = _preview_row(existing, sample)
        row.update(_accession_detail(plan_dicts))
        rows.append(row)
    return JsonResponse({"samples": rows})


def _preview_row(existing, sample):
    from mutint_experiment.coordinates import format_time_point

    identity = sample_names.parse_sample_identity(sample.name)
    return {
        "name": sample.name,
        "files": sample.files,
        "population": identity.population if identity else "",
        "time_point": ("" if identity is None
                       else str(format_time_point(identity.time_point))),
        "sample": (sample_names.sample_label(identity.name, identity.replicate)
                   if identity else sample.name),
        "placed": identity is not None,
        "replaces": _replaced_label(existing, sample.name, identity),
    }


# --- accessions -----------------------------------------------------------------------------

def _resolve_accessions(text):
    """`sra.Plan`s for what was typed in the accessions box; `[]` for nothing.

    Core's parser and core's resolver, so the plugin holds no rule about what an accession
    looks like and no knowledge of ENA. Raises `AccessionError` or `FetchError`, both of
    which are a sentence naming the token at fault.
    """
    tokens = accessions.parse(text or "")
    return sra_fetch.resolve(tokens) if tokens else []


def _accession_samples(plans):
    """`[(DerivedSample, [plan dict]), ...]`: one sample per accession, as a launch or a
    preview would produce it.

    Which runs are one sample is core's rule (`sra.samples_in`); what it is called is ENA's
    alias when that alias could be a sample name *here* -- `SAMPLE_NAME_RE` is this plugin's
    rule, passed in as the `usable` test -- and the accession otherwise. The plan dicts are
    restricted to the sample's own runs, so a study's rows each download their own share.
    The files are ENA's basenames, which are what the worker will find in `reads/` and what
    the mate rule is then applied to.
    """
    samples = []
    for sample in sra.samples_in(plans):
        name = sra.sample_name_for(sample, usable=lambda n: bool(SAMPLE_NAME_RE.match(n)))
        files = sorted(name for run in sample.runs for name in run.filenames)
        plan_dicts = [sra.Plan(sample.typed, sample.kind, sample.runs).as_dict()]
        samples.append((read_names.DerivedSample(name, files), plan_dicts))
    return samples


def _accession_detail(plan_dicts):
    """What the preview says about an accession sample beyond its name and files."""
    plans = sra.as_plans(plan_dicts)
    first_run = plans[0].runs[0] if plans and plans[0].runs else None
    return {
        "accession": plans[0].typed if plans else "",
        "kind": plans[0].kind if plans else "",
        "alias": first_run.alias if first_run else "",
        "title": first_run.title if first_run else "",
        "biosample": first_run.sample_accession if first_run else "",
        "runs": [{"accession": run.accession, "files": run.filenames, "bytes": run.bytes,
                  "layout": run.layout}
                 for plan in plans for run in plan.runs],
        "bytes": sum(plan.total_bytes for plan in plans),
    }


def _name_clash(plan):
    """A sentence if two samples of one launch share a name, else None.

    Two dropped files cannot derive one name -- `derive_samples` merges them into one
    sample -- but an accession's alias can equal a dropped file's derived name, or two
    accessions can share an alias. Two rows with one name would supersede each other: the
    second import replaces the first's calls, and the run list shows two runs for one sample
    with no sign that one of them is gone.
    """
    seen = {}
    for sample, plan_dicts in plan:
        source = plan_dicts[0]["typed"] if plan_dicts else "the dropped files"
        if sample.name in seen:
            return ("%s would be the name of two samples in this launch, from %s and from "
                    "%s. Give one of them a different name -- or launch them separately."
                    % (sample.name, seen[sample.name], source))
        seen[sample.name] = source
    return None


def _run_for(plans, filename):
    return sra.filenames_by_run(plans).get(filename, "an accession")


def _replaced_label(existing, name, identity):
    """The label of the sample this one would supersede, or "".

    Both keys, because the importer uses both: a placed name matches on the coordinate, and a
    name carrying none matches on `source_name`. The same rule `_existing_samples` documents.
    """
    from mutint_experiment.coordinates import format_time_point

    for row in existing:
        if identity is not None:
            if (row["population"] == identity.population
                    and row["time_point"] == str(format_time_point(identity.time_point))
                    and row["sample"] == sample_names.sample_label(
                        identity.name, identity.replicate)):
                return row["label"]
        elif row["source_name"] == name:
            return row["label"]
    return ""


def _flat_name(staged_root, path):
    """The name a staged file takes once it is under a run's flat `reads/` directory.

    Flattened deliberately: a drop may arrive with directory structure (a run folder from a
    sequencing core), and breseq takes read files positionally with no notion of where they
    sat. Basenames are made unique by the directories they came from, so two lanes' `R1.fastq`
    do not collide.

    **One definition, three callers** -- taking the reads, deriving the samples, and the
    preview the page draws. The derivation runs over these names, so a second spelling here
    would make the preview promise names the run would not produce.
    """
    return os.path.relpath(path, staged_root).replace(os.sep, "__")


def _staged_files(staged_root):
    """`{flat name: source path}` for everything in the drop."""
    found = {}
    for dirpath, _dirnames, filenames in os.walk(staged_root):
        for filename in sorted(filenames):
            source = os.path.join(dirpath, filename)
            found[_flat_name(staged_root, source)] = source
    return found


def _take_reads(staged, run, names):
    """Move this run's share of the drop into its own reads directory. Returns the names.

    **Its share, not all of it** -- one launch can produce several runs, and each owns a
    directory of its own because `BreseqRun`'s post_delete receiver rmtrees the whole thing.
    The shares are disjoint, so the files are moved rather than copied and nothing is stored
    twice.
    """
    reads_dir = store.ensure_dir(run.reads_dir())
    for name in names:
        shutil.move(staged[name], os.path.join(reads_dir, name))
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
