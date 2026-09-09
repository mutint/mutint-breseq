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

**93 tests**, and the end-to-end ones are affordable because of two things. The test runner
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
