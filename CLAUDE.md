# CLAUDE.md — mutint-breseq

Guidance for Claude Code working in this repository.

It is a **submodule of `mutint`**. Edit it **here**, in the suite-root checkout, never in
`mutint/mutint-breseq` — that copy is on a detached HEAD and a commit made there is reachable
only by SHA inside that one clone. See the suite `CLAUDE.md`.

---

## What this is

A page that runs breseq. Drop an experiment's FASTQ reads on `/breseq/`, name the sample, and
breseq runs in the background against that experiment's stored reference; its output folder is
then handed to **aledb-core's own `aledb_import.breseq_folder`**, so the sample that lands is
indistinguishable from one analysed elsewhere and dropped on the Add Data page.

That last clause is the design. This plugin does not import anything itself — it *produces the
input* to core's importer and gets out of the way. Nothing here parses a `.gd`, writes a
`Mutation`, or knows what a sample's coordinate is.

**The name is MutInt's, not the platform's.** Every other plugin is `aledb-*`; this one is
named for the deployment that asked for it and is deliberately absent from
`aledb-deploy/.gitmodules`. That is the whole of what keeps it out of ALEdb — omitting it from
a project's `.gitmodules` is how a component is not installed.

---

## The four pieces

| file | what |
|---|---|
| `runner.py` | pure: the argv, the PATH, what counts as usable output, what to keep |
| `tasks.py` | the `@task` — run breseq, check, import, clean up |
| `views.py` | the page, the launch endpoint, the run list, serving the report |
| `models.py` | `BreseqRun`, and the receiver that owns its directory |

`runner.py` is pure on purpose, in the shape `aledb_sample/locus.py` is: the two rules most
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
of a job that did nothing. Same posture as `aledb_import.tasks.build_coverage` calling the
raising variant. `test_the_task_re_raises_after_recording_a_failure` pins it, and has to call
the task directly — `.enqueue()` hands the exception to the backend.

### `status` and `task_result_id` disagree usefully

A row saying `queued` is either a job a worker has not reached yet or a job **no worker will
ever reach**, and those are indistinguishable from the row alone. `_queue_status` asks the
queue, and the page says *waiting for a worker* when the answer is that nothing has picked it
up. Running no worker is this feature's easiest failure by a distance and the one a person is
least equipped to guess at; it is worth a column.

### The run directory is this plugin's to delete

`store.component_dir("mutint_breseq", pk)` is core's path builder and **core reaps nothing
under it** — it cannot know what a component keeps. The `post_delete` receiver in `models.py`
is the whole lifecycle, and because `BreseqRun.experiment` cascades, that receiver is also what
makes deleting an *experiment* reach the reads and the report.

After a success only `report/` survives. A **failure keeps everything**, which is exactly when
the reads matter.

---

## What it deliberately does not do

- **It registers no import handler.** A FASTQ drop needs a sample name and a command line, and
  `handle(experiment, staged_root, paths, user)` can carry neither — registering would put an
  entry in the Add page's dropdown that cannot carry what the entry needs. The upload machinery
  is still core's: `aledb_import.staging` was added for this, and `docs/plugin/staging.md` in
  aledb-core is the guide.
- **It registers no rebuilder.** It derives nothing from the mutations, it *makes* them once.
  Core's importer asks for every registered rebuild itself.
- **No export handler, no example dataset.** It adds no mutation type, and an example would
  have to ship reads and run breseq to demonstrate anything.
- **No cancel button.** The process belongs to the worker, and a status the product cannot
  enforce is a button that lies.
- **No multi-sample launch.** One launch is one sample. Several launches queue.

---

## Tests

```bash
cd mutint && ./mutint test mutint_breseq
```

There is no way to run them from aledb-core: the plugin is not installed there.

**52 tests**, and the end-to-end ones are affordable because of two things. The test runner
forces `django.tasks` to its immediate backend, so `.enqueue()` runs inline and one POST
exercises launch, the subprocess, the ingest and the cleanup. And `tests/fake_breseq.py` is a
**real executable on disk** rather than a `subprocess.run` patch — the two things most likely
to be wrong here are the argv and the PATH, and a patch would assert against the call rather
than against a process that actually has to start. It records its own argv and `PATH` to a
file, which is how those two are checked.

The template it copies into `-o` is built by **aledb-core's own** `breseq_fixture.write_sample`,
so the shape the fake produces cannot drift from the shape the importer requires.

One gotcha, found the hard way: `tools.tool_path` falls back to `PATH` by design, so a test
asserting "breseq is missing" must clear `PATH` as well as move `ALEDB_TOOLS_DIR` aside — on a
machine with a real breseq installed it otherwise tests the developer's copy.
