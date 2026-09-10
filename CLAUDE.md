# CLAUDE.md — mutint-breseq

Guidance for Claude Code working in this repository.

It is a **submodule of `mutint`**. Edit it **here**, in the suite-root checkout, never in
`mutint/mutint-breseq` — that copy is on a detached HEAD and a commit made there is reachable
only by SHA inside that one clone. See the suite `CLAUDE.md`.

---

## What this is

A page that runs breseq. Drop an experiment's FASTQ reads on `/breseq/`, name the sample, and
breseq runs in the background against that experiment's stored reference; its output folder is
then handed to **mutint-core's own `mutint_import.breseq_folder`**, so the sample that lands is
indistinguishable from one analyzed elsewhere and dropped on the Add Data page.

That last clause is the design. This plugin does not import anything itself — it *produces the
input* to core's importer and gets out of the way. Nothing here parses a `.gd`, writes a
`Mutation`, or knows what a sample's coordinate is.

**The name no longer sets it apart, and used to.** Every other plugin was `aledb-*` and this
one was named for the deployment that asked for it; the platform is MutInt now, so the name
says nothing about where it is installed. It is deliberately absent from `aledb/.gitmodules`,
and that is the whole of what keeps it out of ALEdb — omitting it from
a project's `.gitmodules` is how a component is not installed.

---

## The four pieces

| file | what |
|---|---|
| `runner.py` | pure: both argvs (fastp's and breseq's), the PATH, what counts as usable output, what to keep. It runs nothing -- `mutint_jobs.processes.run_tool` does |
| `pairing.py` | pure: breseq's rule for which read files are mates, and which files fastp must not touch |
| `tasks.py` | the `@task` — trim, run breseq, check, import, clean up |
| `views.py` | the page, the launch endpoint, the run list |
| `models.py` | `BreseqRun`, and the receiver that owns its directory |

`runner.py` is pure on purpose, in the shape `mutint_sample/locus.py` is: the two rules most
likely to be changed by accident are what reaches breseq's argv and what has to be on disk
before the importer is called, and both are testable without breseq, a worker or a request.

---

## Things that are load-bearing

### breseq needs its own toolchain on PATH, and fails *exiting 0* without it

breseq shells out to `bowtie2`, `samtools` and `gnuplot` **by bare name**. An absolute path to
breseq is therefore not enough: without `env/tools/bin` on `PATH` it stops with
`Required executable "bowtie2" not found` — and it does so with **returncode 0**, so a
returncode check alone calls that run a success and hands the importer an empty directory.

`runner.tool_environment()` is what builds that PATH, and `runner.check_output()` is the second
half of the answer: what the run *produced* decides, not what it exited with. Both have tests.
This was found by running it.

### The channel lives in `tools.txt`, not in the entry script

`breseq-prerelease` is published from the lab's own channel, and the entry scripts install with
`-c conda-forge -c bioconda` — three byte-identical copies of a file with no tests. micromamba
accepts `channel::package` with a URL channel, so the spec carries its own source:

```
https://barricklab.github.io/conda/::breseq-prerelease
```

Verified against the real channel: it resolves, and its dependencies come from bioconda, which
is already on the list. **Do not "tidy" this into a bare package name and a channel flag** —
that is an edit to three entry scripts for one component's dependency.

The spec is unpinned because every prerelease build reports a git-derived version
(`0.40.1.dev417+g1a30aed3`); `rm -rf env/tools` is how you move it.

### The output directory is named for the sample, and that is what does the naming

`-o` is `<component_dir>/<sample_name>`. `breseq_folder.find_sample_dirs` walks looking for
`<dir>/data/output.gd` and takes the sample's name from that directory's basename — so naming
the output directory after the sample is what lets the ingest be a bare
`import_samples_into(experiment, run.directory())` with **no reshaping and no second rule about
what a sample is called**. `sample_names.parse_sample_identity` then reads the ALE, flask and
isolate out of it exactly as it would for an uploaded folder.

That is also why `SAMPLE_NAME_RE` in `views.py` is narrow: the string is a directory name
*and* a coordinate. Both shapes that parser understands — `3-30000-1-1` and
`Ara-2_500gen_763A` — fit inside it, and a pattern narrowed further would quietly stop samples
landing on their ALE.

### breseq 0.50 writes `data/output.gd` itself

Checked against a real run: `data/` holds `output.gd`, `annotated.gd`, `output.vcf`,
`reference.{fasta,fasta.fai,gff3}`, `reference.bam{,.bai}` and `summary.json` — every file
`breseq_folder` requires. **Nothing here reshapes anything**, and `check_output` exists to name
a missing file rather than to move one. Older breseq wrote the `.gd` only to `output/`; if that
ever needs supporting, `check_output` is where it goes.

The canonical GFF3 in the store round-trips: breseq reads it, re-emits its own into `data/`,
and `reference.sequence_set_digest` of the two is equal — which is what stops
`_establish_or_check_reference` rejecting every run as a reference mismatch. Verified before
this was written.

### The name is four boxes, and the three parts are what is posted

**Full Name** plus **Population**, **Time point** and **Sample**, kept in step both ways:
typing a name splits it, editing a part rebuilds it. The split is
`mutint_common/staticfiles/js/mutint_sample_names.js`, core's transcription of
`mutint_import/sample_names.py`.

**The endpoint takes the three parts, not the joined name**, and
`sample_names.compose_sample_name` makes the name. That is deliberate: how a coordinate
becomes a string is one rule in one place, and it can change without this form changing with
it. The composer refuses what cannot be a name -- a population without a time point, a space
in a part, a fractional time point -- and says *which field* is at fault, so the page can
point at the box rather than at the form.

**The preview can be wrong and the import cannot.** The JS decides nothing; the server composes
and the importer parses, both in Python. That is the whole argument for having a second copy of
the rule at all, and the JS header records the two ways it is known to under-read.

**A blank Population and Time point is the unplaced case**, and the page says so: the sample
lands on `Unspecified` with no time point rather than on a population called `1`.

**A collision warns and does not block.** Importing a sample the experiment already holds
*supersedes* it -- `_database_gd_mutations` clears its calls before writing the new ones -- and
that is a thing people do on purpose after fixing a command line. So the page names the sample
that would be replaced and lets the launch through. `views._existing_samples` is what it warns
from, keyed both ways because the importer matches a placed name on the coordinate and an
unplaced one on `source_name`.

**Relaunching a sample whose run is still in flight stops the earlier run**
(`views._supersede_in_flight`). Two runs writing one sample race, and the older one's output is
about to be overwritten whatever happens. Cooperative like every cancellation here: the flag is
set through `mutint_jobs.request_cancel_for` and the task is what acts on it. The ground for
cancelling is not that the job is yours -- it is that this component has superseded its own
earlier work.

**A launch clears the files and nothing else.** A batch is one population and one time point
with a different sample each time, so emptying those would make the common case the one that
costs the most typing.

### Population sample is one checkbox and two assertions

**Population sample (run with `-p`)** sits above the arguments box, and ticking it does two
things that have to travel together: `-p` goes on breseq's command line, and the sample the
import produces is recorded `is_clonal=False`. Either one alone is a half-answer -- polymorphism
mode with the sample filed as a clone, or a sample called a population that breseq never called
polymorphisms in.

**The argv half mirrors `-j` exactly.** `POLYMORPHISM_FLAGS` carries both spellings and
`build_argv(polymorphism=True)` injects `-p` only where the box does not already say. breseq
would take a duplicate happily -- it is a boolean flag, unlike `-j`, where the last one silently
wins -- so the reason not to add one is that a command line saying `-p` twice reads as the page
having ignored what was typed. It is injected with the other defaults, ahead of the typed
arguments, so the box stays the last word.

**The clonality half is asserted here, and core already has a rule for it.**
`gd_import` reads ` -p` out of the `.gd`'s `#=COMMAND` line, which is the right rule for a
folder analysed elsewhere and dropped on the Import data page. It is not enough for this page,
for a reason that is not a defect in it: it writes `is_clonal` inside a
`get_or_create(defaults=...)`, so **a run over a sample the experiment already holds never
reaches it** -- and re-running breseq with better options, superseding the sample, is exactly
what this page advertises. It also matches only the short spelling, and only where breseq wrote
a `#=COMMAND` at all. So `tasks._mark_population_sample` says it outright.

**It never writes `True`.** An unticked box is not a claim that the sample is a clone: somebody
may have typed `-p` into the arguments box, in which case core's rule has already marked it and
writing True would undo that. The checkbox asserts one thing in one direction, which is the
whole of what it knows.

**This is a shared write**, so it belongs behind `can_edit_experiment` -- which `launch`
already checks, and which is the only gate a run has.

### Limit coverage is a box because the value is a decision, not a flag

`-l` is the second option promoted out of the arguments box, and the third injection rule in
`build_argv` after `-j` and `-p`. It follows them exactly -- `COVERAGE_FLAGS` carries both
spellings and the box wins -- with one difference that matters: **`-l` takes a value**, so a
duplicate would be a real ambiguity rather than merely untidy.

**Blank is a value.** It means every read, which is breseq's own default, so the column is
nullable and `None` adds nothing to the argv rather than a "no limit" sentinel being invented.
Zero would have been the wrong spelling of it -- a coverage limit of nothing.

**A checkbox says whether there is a limit; the box says how much.** An empty box carrying
both reads as a field somebody has not filled in yet rather than as a decision -- and since a
`type="number"` input reports an unparseable entry as *empty*, a typo was indistinguishable
from it. **Nothing posts the checkbox**: unticked simply sends blank, which is already how the
server spells "every read", so the two cannot arrive disagreeing and no boolean joins
`coverage_limit` on the model. Same rule as `locked_at` in core -- the value is the flag, with
nothing beside it to hold a second opinion.

Two things fall out of the checkbox and both are in the page. Ticking fills in `80` rather
than enabling an empty box, because enabled-and-empty would be a third state saying nothing;
unticking leaves the number where it is, visibly disabled and unsent, so changing your mind
twice costs no typing. And the submit guard has to ask the checkbox **before** `checkValidity`,
since a disabled input is barred from constraint validation and always reports itself valid.

**The number is checked here because breseq will not check it.** Measured against the pinned
build: `-l notanumber` and `-l -5` both **pass `--dry-run`**, exactly as `-j notanumber` does.
So the preflight cannot be the guard, and `views._coverage_limit` is -- refusing before the
upload is claimed and naming the box in `field`. `float()` alone is not enough either: it
accepts `nan` and `inf`, which would reach the command line as words.

**The stored value is a float and does not reach breseq as one.**
`runner.format_coverage_limit` is what keeps `80` from being written `80.0` in every log, and
it trims `%f` rather than using `%g`, which switches to exponent notation past six digits and
would hand breseq `1e+06`.

**The page checks it too, and has to.** The form is submitted through JS, so the browser's own
constraint validation never runs -- and with the box ticked, an unparseable entry reporting
itself empty would post as "every read", the opposite of what the tick said. `checkValidity()`
before the upload is what stops that.

### breseq is asked whether it would accept the command line, twice

`breseq --dry-run` validates every option, checks that bowtie2, samtools and gnuplot are
installed, checks every input file exists and every output path can be written, then exits
without running and **without creating anything** -- 0 if it is happy, 255 if not. It is asked
in two places, for two different reasons:

| | reads it names | what it establishes |
|---|---|---|
| `views._preflight`, at launch | a throwaway FASTQ in a temp dir | the box is a command line breseq accepts |
| `tasks.run_breseq`, before trimming | the real untrimmed reads | that, **and** that this machine has the toolchain |

**The launch one runs before the upload is claimed**, which is the whole reason it is where it
is. Once `_take_reads` has moved the reads out of staging, the only way to try again is to
upload them again -- so a typo in the arguments box has to be caught while the session is still
open. It creates no `BreseqRun` and closes no session: fix the box, press the button again.

**It writes its own FASTQ because breseq checks that inputs exist.** A genuinely absent path
fails the check rather than passing it, so one four-line record in a `TemporaryDirectory` is
what makes the check about the *options*. Measured: a minimal record satisfies it and the
output directory is never created.

**The worker's check is not the same check.** Options cannot change in between, but the machine
can: `db_worker` may run on another host, and one started outside `./mutint` has no
`MUTINT_TOOLS_DIR` and finds none of breseq's toolchain. It is also the cheapest place the
missing-bowtie2 trap is caught -- breseq stops for that **exiting 0**, which `check_output`
otherwise only discovers after a run that did nothing.

**One argv builder.** `runner.build_argv(dry_run=True)` is the real command line plus the flag,
because a preflight assembled separately would validate something other than what runs. The one
difference it cannot avoid is the read files, which is why the launch check is about options
alone and the worker's is about options as they will actually be given.

**What it does not catch**: option *values*. Measured, `-j notanumber` passes the dry run. It
knows what options exist and whether paths are usable, and does not pretend to more.

`runner.refusal_from` is what reaches a person: breseq answers an unknown option by printing its
whole help and the reason on the last line, and a bad path with `---> ERROR` lines and a summary
-- so it keeps every ERROR line plus the last one. The full output is in the job log either way.

### Cancellation is cooperative, and reaches the whole process group

The first `cancellable=True` task in the suite. The queue cannot interrupt a running task -- it
has no cancel API, and its worker calls the function and looks at nothing again until it
returns -- so `mutint_jobs` records a flag and the run loop polls it while the tool runs. That
is why `subprocess.run` is gone: it blocks until exit, so there is no moment at which anything
could ask.

**The loop is core's now.** It was `runner.run_breseq_process` and is
`mutint_jobs.processes.run_tool`: polling a flag and signalling a process group is what any
task shelling out from a worker needs, and none of it was breseq's. What stayed is what is --
the argv, the PATH those binaries are found on, and what counts as output the importer can
read. The process-group assertions moved with it, to `mutint_jobs/tests/test_processes.py`.

**`start_new_session=True` and `os.killpg`, never `process.kill()`.** breseq spawns bowtie2 and
samtools; killing only the parent leaves them running with no parent at all, and the job would
report itself stopped while the machine stayed saturated -- worse than not offering the button.
`test_cancelling_kills_the_process_and_its_children` asserts a spawned child dies too, because
`process.kill()` would pass a test that checked only the parent.

**Cancellation is not failure and is not re-raised.** Letting it out would record the job FAILED
on the queue and put a traceback in front of somebody who got what they asked for. It is its own
status, and it is the one path that **deletes** the reads and the partial output: a failure is
something to diagnose, a cancellation is not.

Four places poll, and the last two are easy to forget: at task entry (a job cancelled while
queued is still handed to a worker, because `request_cancel` deliberately never touches the
queue row), inside the run loop -- which fastp runs through as well as breseq -- in the gap
between trimming and breseq (a cancel that landed during fastp's last file would otherwise start
an hours-long breseq), and while waiting for the import lock -- that wait can be half an hour,
and a wait nobody can give up on is the same dead end as a job nobody can stop.

### The log is the job's, and `BreseqRun.log` is its tail

fastp and breseq both write into one file, opened once for the run:
`<store>/components/mutint_jobs/<queue id>/job.log`, read at `/jobs/<pk>/log` **while the run
is still going**. That is the point -- a twelve-hour run used to print nothing anybody could
see until it ended, because the output sat in a pipe nobody drained until `communicate()`
returned. The run list links there per run.

`BreseqRun.log` stays, and stays the tail: it is what the failure messages embed, what the run
list folds open, and what survives `./mutint reap_jobs` removing the `Job` row. It is filled
from `logs.read_tail` when the tools are done rather than from a return value, so `_log_text`
-- which joined fastp's account to breseq's by hand -- is gone: they are in the order they ran
because they appended to one file in that order.

**`_queue_id(context, run)` is why the task takes a context.** `BreseqRun.task_result_id` is
written *after* `jobs.enqueue` returns, so a backend that runs the task inside `enqueue` --
the suite's -- executes everything before that column is set, and both the log and the
cancellation poll would silently do nothing. The `TaskContext` knows either way. A caller
invoking the task directly passes `None` and gets neither, which is the honest answer for a
run that is on no queue.

### Trimming is breseq's pairing rule, or it is wrong

fastp trims a pair in paired-end mode, so the plugin has to decide which files are mates
*before* breseq does -- and if the two decided differently, breseq would build read groups from
trimmed files whose mates it never saw together. `pairing.read_file_sets` is therefore
`cReadFileSets::Init` from breseq's `settings.cpp` transcribed, not a regex on `_R1`: same-length
base names differing at exactly one `1`/`2`, duplicates renamed first, ambiguity meaning
unpaired. `test_pairing.py` holds the cases. The trimmed files keep their names in `trimmed/`,
which is what makes breseq pair them identically and what keeps a `.gz` a `.gz` (fastp decides
compression from the output name).

The options are brefito's and only brefito's -- `--disable_quality_filtering`, plus
`--detect_adapter_for_pe` for a pair. Long-read files are sniffed (a read of 1000 bp or more in
the first 200 records, breseq's own trigger length) and passed through untrimmed, per *set* so a
pair is never half trimmed; so is anything not FASTQ by name. fastp failing fails the run.

**`run_delete` refuses an unfinished run.** The `post_delete` receiver rmtrees the run
directory, so deleting a running one pulls the reads out from under the live subprocess and
breseq then fails minutes later naming neither cause nor culprit. Cancel, then delete.

### The import lock is waited on, not raced

`import_lock.acquire()` refuses immediately, because the web path would rather answer 409 than
hold a request open for somebody else's drop. Out here the calculus is the opposite: nobody is
waiting on a response, and what is at stake is **hours of finished work** that would be thrown
away over a few seconds of overlap with a web upload. `_wait_for_import_lock` polls for up to
half an hour. It is taken **only around the ingest**, never around the breseq run.

### The task re-raises after recording

`_fail` writes the row, then the exception goes on. Both halves matter and the second is easy
to delete: the row is what a person reads, and the queue's own record is what says a worker
tried and could not. A task that returned quietly would leave `db_worker` reporting a clean run
of a job that did nothing. Same posture as `mutint_import.tasks.build_coverage` calling the
raising variant. `test_the_task_re_raises_after_recording_a_failure` pins it, and has to call
the task directly — `.enqueue()` hands the exception to the backend.

### `status` and `task_result_id` disagree usefully

A row saying `queued` is either a job a worker has not reached yet or a job **no worker will
ever reach**, and those are indistinguishable from the row alone. `_queue_status` asks the
queue, and the page says *waiting for a worker* when the answer is that nothing has picked it
up. Running no worker is this feature's easiest failure by a distance and the one a person is
least equipped to guess at; it is worth a column.

### The report is the sample's, not the run's

This plugin used to keep breseq's `output/` in a `report/` of its own and serve it at
`/breseq/run/<pk>/report/<path>`. Both are gone. **mutint-core keeps the report under the
sample** -- `breseq_folder._store_report` into `store.sample_report_dir` -- and serves it
sandboxed at `/mutations/report/<sample_id>/`, so the run list simply links there.

Two reasons, and the second is the real one. Two homes for the same bytes is one; and a report
describes *the sample that was produced*, which outlives this row -- deleting a run must not
take away the evidence behind mutations that are still in the database. It also means a sample
uploaded as a breseq folder on the Add page has a report on exactly the same terms, which it
could never have had while this plugin owned the serving.

`cleanup_after_import` therefore deletes everything now and returns nothing; the importer has
already kept what matters by the time it runs.

### The run directory is this plugin's to delete

`store.component_dir("mutint_breseq", pk)` is core's path builder and **core reaps nothing
under it** — it cannot know what a component keeps. The `post_delete` receiver in `models.py`
is the whole lifecycle, and because `BreseqRun.experiment` cascades, that receiver is also what
makes deleting an *experiment* reach the reads and the report.

A success deletes `reads/`, `trimmed/` and the output directory. A **failure keeps everything**,
which is exactly when the reads matter.

---

## What it deliberately does not do

- **It registers no import handler.** A FASTQ drop needs a sample name and a command line, and
  `handle(experiment, staged_root, paths, user)` can carry neither — registering would put an
  entry in the Add page's dropdown that cannot carry what the entry needs. The upload machinery
  is still core's: `mutint_import.staging` was added for this, and `docs/plugin/staging.md` in
  mutint-core is the guide.
- **It registers no rebuilder.** It derives nothing from the mutations, it *makes* them once.
  Core's importer asks for every registered rebuild itself.
- **No export handler, no example dataset.** It adds no mutation type, and an example would
  have to ship reads and run breseq to demonstrate anything.
- **No multi-sample launch.** One launch is one sample. Several launches queue.
- **No anonymous use.** `breseq`, `launch`, `runs` and `run_delete` all say
  `if not request.user.is_authenticated` outright, as `project_create` does, even though
  `can_edit_experiment` already refuses anonymous. The rule is stated rather than left to be
  inferred from three files — the shape that has bitten this suite before is an endpoint whose
  author had no object to run a predicate against.

*(This list used to end **No cancel button**, on the grounds that the process belongs to the
worker and a status the product cannot enforce is a button that lies. The first half is still
true of the queue and the second is still the rule; what changed is that the task now stops
itself. See **Cancellation is cooperative** above.)*

---

## Tests

```bash
cd mutint && ./mutint test mutint_breseq
```

There is no way to run them from mutint-core: the plugin is not installed there.

**130 tests**, and the end-to-end ones are affordable because of two things. The test runner
forces `django.tasks` to its immediate backend, so `.enqueue()` runs inline and one POST
exercises launch, the subprocess, the ingest and the cleanup. And `tests/fake_breseq.py` is a
**real executable on disk** rather than a `subprocess.run` patch — the two things most likely
to be wrong here are the argv and the PATH, and a patch would assert against the call rather
than against a process that actually has to start. It records its own argv and `PATH` to a
file, which is how those two are checked.

The template it copies into `-o` is built by **mutint-core's own** `breseq_fixture.write_sample`,
so the shape the fake produces cannot drift from the shape the importer requires.

One gotcha, found the hard way: `tools.tool_path` falls back to `PATH` by design, so a test
asserting "breseq is missing" must clear `PATH` as well as move `MUTINT_TOOLS_DIR` aside — on a
machine with a real breseq installed it otherwise tests the developer's copy.
