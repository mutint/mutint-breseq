# CLAUDE.md — mutint-breseq

Guidance for Claude Code working in this repository.

It is a **submodule of `mutint`**. Edit it **here**, in the suite-root checkout, never in
`mutint/mutint-breseq` — that copy is on a detached HEAD and a commit made there is reachable
only by SHA inside that one clone. See the suite `CLAUDE.md`.

---

## What this is

A page that runs breseq. Drop an experiment's FASTQ reads on `/breseq/` -- or name them by SRA
accession, or both -- name the sample, and breseq runs in the background against that
experiment's stored reference; its output folder is then handed to **mutint-core's own
`mutint_import.breseq_folder`**, so the sample that lands is indistinguishable from one
analyzed elsewhere and dropped on the Add Data page.

That last clause is the design. This plugin does not import anything itself — it *produces the
input* to core's importer and gets out of the way. Nothing here parses a `.gd`, writes a
`Mutation`, or knows what a sample's coordinate is.

**The name no longer sets it apart, and used to.** Every other plugin was `aledb-*` and this
one was named for the deployment that asked for it; the platform is MutInt now, so the name
says nothing about where it is installed. It is deliberately absent from `aledb/.gitmodules`,
and that is the whole of what keeps it out of ALEdb — omitting it from
a project's `.gitmodules` is how a component is not installed.

---

## The pieces

| file | what |
|---|---|
| `runner.py` | pure: both argvs (fastp's and breseq's), the PATH, what counts as usable output, what to keep. It runs nothing -- `mutint_jobs.processes.run_tool` does |
| `pairing.py` | pure: breseq's rule for which read files are mates, and which files fastp must not touch |
| `read_names.py` | pure: what a read file's name says the sample is called, and which files are one sample |
| `mate_check.py` | pure: whether two files that pair by name really are mates, and how to unpair them if not |
| `tasks.py` | the `@task` — trim, run breseq, check, import, clean up |
| `views.py` | the page, the launch endpoint, the preview, the run list; resolves accessions through core |
| `models.py` | `BreseqRun`, and the receiver that owns its directory |
| `static/mutint_breseq/launch.js` | the page's behaviour: the input-type menu, the boxes, the drop zone, the run list |

The first four are pure on purpose, in the shape `mutint_sample/locus.py` is: the rules most
likely to be changed by accident are what reaches breseq's argv, what has to be on disk before
the importer is called, how twenty files become ten samples, and whether two of them are really
mates -- all testable without breseq, a worker or a request.

**The page's script is a file, not an inline `<script>`**, and moved when the input-type menu
made it four hundred lines inside a template that also holds the markup. It lives under
`static/`, which is `AppDirectoriesFinder`'s directory and the one a plugin gets for free --
`mutint_common/staticfiles/` is core's own and is named explicitly in `STATICFILES_DIRS`. The
two values it used to interpolate reach it through a `json_script` element instead.

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

### The Input type menu, and the three contracts behind it

There were four boxes for one thing -- **Full Name** plus **Population**, **Time point** and
**Sample**, kept in step both ways -- and that reads as two questions rather than one answered
two ways. The menu says which half is being filled in, and the other half is **disabled**,
showing what the first one means. The two-way sync is unchanged; what is new is that one side
is always authoritative.

`disabled` rather than `readonly`: a disabled input posts nothing and cannot be focused, which
is exactly "not editable unless you switch to that version of the form". `readonly` looks
identical and still submits.

**The three modes are three server contracts**, not one with optional fields:

| mode | posts | server |
|---|---|---|
| `parts` | population, time point, sample | `compose_sample_name` -- one rule for how a coordinate becomes a string |
| `name` | the name | stored **verbatim** |
| `read_names` | nothing about names | `read_names.derive_samples` over the staged files |

`parts` is the default, because it is the contract every existing caller posts and because a
mode nobody named should be the one that composes rather than the one that trusts a string. An
unknown mode is a 400: guessing `parts` would launch one sample for a drop somebody meant as
ten.

**`name` stores the name verbatim, and that is a change.** Every mode used to post the three
parts and the server recomposed -- which is not a round trip. `3-30000-1-1` parses to population
3, time point 30000, label `1-1`, and composes back to `3_30000_1-1`, so somebody who typed a
perfectly good A-F-I-R name got a different one. A mode called *metadata from a name* has to
leave the name alone; the coordinate is still read out of it by the importer, exactly as it
would be from a dropped folder of that name.

**The split shown under the name is still `mutint_sample_names.js`**, core's transcription of
`sample_names.py`, and it still decides nothing -- the server parses or composes and the
importer parses, all in Python. That is the whole argument for having a second copy of that
rule at all, and the JS header records the ways it is known to under-read.

**A blank Population and Time point is the unplaced case**, and the page says so: the sample
lands on `Unspecified` with no time point rather than on a population called `1`.

**The menu is remembered per person** through `mutint_common.preferences`
(`breseq.input_mode`), the way the Import data page remembers its tab. One key rather than one
per experiment: which way in somebody uses is a habit rather than a property of the data.

### One drop can be several samples, and that is N rows

`read_names` mode derives one sample per read file set and creates **one `BreseqRun` per
sample** -- each with its own `component_dir`, its own breseq invocation, its own job on
`/jobs/` and its own Cancel. Every invariant a single run has is kept; nothing about the model,
the task or the importer changed, and there was no migration.

**N rows rather than one row with N samples, and the receiver is why.** `models.py`'s
`post_delete` rmtrees `component_dir(COMPONENT, pk)` -- the run's *whole* directory -- so two
rows sharing one would mean deleting either destroys the other's reads mid-run. A single row
holding several samples fails differently and worse: `tasks.py` reads `summary["files"][0]`,
looks one sample up by `run.sample_name`, and cleans up one `output_dir`, so samples two
onwards would import unchecked, link to no run, and leave their output in the store for ever.

Three things in `launch` follow from the loop, and the first is the one that bites:

- **Every name is superseded before any row is created.** `_supersede_in_flight` matches on the
  sample name and knows nothing about which launch a row came from, so called inside the loop,
  run two's supersede would cancel run one from this same launch.
- **`_take_reads` takes one run's share**, named rather than "everything under the root". The
  shares are disjoint, so files are moved and nothing is stored twice.
- **A partial failure unwinds every row**, not the one that failed. A half-launched drop is
  worse than none: the samples that did start would have to be found and cancelled by hand.

There is deliberately **no batch column**. The run list is ordered `-created_at`, so a launch's
rows are already adjacent, and a grouping key would be a mechanism with one producer.

### The preview is a round trip, not a second copy of the rule

`POST /breseq/preview` takes filenames -- no bytes, no session, no state -- and answers one row
per sample the drop would produce: its name, its files, the coordinate the importer will read
out of that name, and the sample it would replace. The page draws it under the drop zone
whenever the selection changes.

**A JavaScript copy of the derivation is the alternative and is refused here.** The suite
already carries one such copy, `mutint_sample_names.js`, and its own agreement test states the
cost: it can check that the two *specifications* match and never that the JS implementation
matches its own table. That copy earns its place because a name box needs an answer per
keystroke. A file drop is a discrete event, so it can afford a round trip -- and one derivation
with two callers cannot drift from itself.

`_flat_name` is shared by the preview and by `_take_reads` for the same reason: the derivation
runs over the flattened names, so a second spelling would make the preview promise names the
run would not produce.

### Deriving a sample from a read filename

`read_names.py` strips what a file's name carries *beyond* the sample's -- the extension, the
lane, the read number, the chunk index -- and hands the remainder to core's
`parse_sample_identity`. **It parses no coordinates itself**, so a sample analyzed from reads
and the same sample uploaded as a breseq folder cannot disagree about where they land.

**It says nothing about clonality either.** The Population sample checkbox governs every sample
in the drop. A vocabulary of words meaning *clone* would be a second thing to keep up to date
and a silent way for one sample in twenty to differ from what the form said.

**Strip first, pair second, and that order was found by measuring.** The obvious design reads
the read number off the one character that differs between mates -- breseq's own rule, needing
no vocabulary. It is wrong here: with two lanes in the drop, `S12_L001_R1_001` can be paired
with its own R2 *or* with `S12_L002_R1_001`, breseq resolves that by input order, and the
differing character is then the **lane** digit. The derived name came out as `S12_L00`. So the
lane goes first, by name; mates are found afterwards, over the stripped names, where there is
no lane left to mistake.

**Sets deriving the same name merge into one sample**, which is what a lane split is: four
files, one library, one sample.

**A bare trailing `1`/`2` is only a read number when a mate was actually found.** `Ara-2_500gen_2`
is a sample whose isolate is called 2, and a rule that ate it would file the sample at a time
point with no isolate. Likewise a trailing `001` is only a chunk index in the company of a lane
or a read number.

**A derived name never ends in a separator**, and it did. Taking a decoration out of the middle
leaves the punctuation that introduced it: `SRR37077254.R1` and `.R2` are mates, removing the
read number leaves `SRR37077254.`, and that became the sample's name and its directory's.
Nothing downstream would have caught it -- `SAMPLE_NAME_RE` anchors the *first* character only,
so a trailing period is a perfectly acceptable name. It was found in use, on real SRA
downloads, which is the general lesson: the derivation is only as good as the filename shapes
it has actually met.

**Any of `.`, `-` and `_` separates a token.** Underscores alone were not enough: single-end
reads are ordinary, SRA downloads arrive as `SRR….R1.fastq.gz`, and with no mate beside them
the `.R1` has to be recognised by name or not at all.

Two things make that widening safe, and both are the reason it is not simply a bigger character
class:

- **Only a trailing run of decorations is removed.** A read number sits at the end of a
  filename, or beside the chunk index which is also at the end, so the scan works right to left
  and stops at the first token that is not a decoration. A rule that removed one from anywhere
  would eat the *population* out of `R1_500gen_x`. At least one token always survives.
- **The separators are kept and the survivors rejoin as they arrived.** Splitting on `[._-]`
  and rejoining with one of them would rewrite `Ara-2_500gen_763A`.

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

### Two files that pair by name are not necessarily mates

`pairing.read_file_sets` is breseq's own rule and decides mates from the **names** alone, which
is all breseq itself knows. Nothing checked the contents, and the failure that hides behind that
is the worst kind this plugin has:

**Handed an unequal pair, both tools truncate to the shorter and exit 0.** Measured against the
pinned versions:

| | given | wrote | exit | said |
|---|---|---|---|---|
| fastp 1.3.6 | 5 and 3 | 3 and 3 | 0 | `WARNING: different read numbers of the 0 pack` |
| breseq 0.50 | 400 and 250 | `num_reads=250` for **both** | 0 | `Warning: R2 file ended before R1 …` |

So the run succeeds, the sample imports, the mutation table looks ordinary, and the reads are
gone. Each tool warns, into hundreds of lines nobody reads, with a zero exit code -- the same
shape as the missing-bowtie2 trap above, and the same answer: *what the run produced decides,
not what it exited with.*

**Because both tools do it, the check belongs to the data**, and runs whether or not trimming
is on. `mate_check.mismatch_reason` asks, in order, whether either file can be read, whether
either has a line count not divisible by four, whether the counts differ, and whether the two
start with the same read -- that last because equal counts are not proof, two different samples'
R1 being able to pair by name and match in size. Counting is a block-at-a-time newline count,
measured at 2 000 000 records in 0.21s against a breseq run of hours.

**What it does about it is rename one file**, and that is the load-bearing part. fastp is *told*
which files are mates by `build_fastp_argv`; breseq works it out for itself from the names. So
unpairing for fastp alone would leave breseq pairing them anyway and truncating exactly as
before, and `--no-paired-mapping` would be far too blunt -- it is per *run*, so a sample
assembled from several lanes would lose the pairing on its good pairs too. Inserting `.unpaired`
before the suffix makes the name a different *length*, which is what breseq's rule keys on, so
both tools apply the one rule they already share and cannot reach different answers. Verified
end to end against the real breseq: `READ FILE SET::UNPAIRED` twice, no warning, `num_reads=400`
and `250` -- every read analysed.

Exactly **one** mate is renamed; renaming both would leave two names differing at one `1`/`2`
again and pair them straight back together.

**The run then succeeds, so `error` is the wrong place to say so** and the run list only folds
the log open for a failure. `BreseqRun.notes` is a list of sentences about a run that worked,
rendered amber in the run list's detail row. Replacing a silent truncation with a silent
unpairing would have been no better than the bug.

**What it does not catch**: truncation *inside* a line. A file cut mid-quality-line still has
four lines in that record. The counts of the two mates then disagree, which is the check that
fires -- but a single-end file cut that way passes unremarked.

### The sample records what it was made from, and this is the only place the pairing is known

Core keeps `Sample.inputs` -- what produced a sample -- and its own folder importer fills it
from breseq's `#=READSEQ` lines. `tasks._record_read_sources` **replaces** that after the
import, and the reason is worth stating: for a folder this plugin built, `READSEQ` names the
*trimmed copies* under a directory about to be deleted. The reads the run was handed are what a
person uploaded and what they would recognise.

**It is also the only place that knows which files were mates.** `pairing.read_file_sets` is
breseq's rule and lives here, so core records read files ungrouped; a run launched through this
page records the grouping the tools actually used.

**The names are the uploaded ones and the grouping is the real one**, which is why it takes the
final read list rather than `run.read_files`. They differ in exactly one case and it is the case
worth getting right: a pair `_split_mismatched_pairs` took apart was renamed on disk, so
grouping the *uploaded* names would re-pair them and record a pairing that did not happen.
`mate_check.uploaded_name` takes the marker back out, so the file shown is the one dropped and
the grouping says it stood alone.

### Reads by accession: the fetch is core's, and the worker does it

The accessions box beneath the drop zone takes a run, a sample, an experiment or a study, and
**this plugin holds no rule about what any of those look like and no knowledge of ENA**:
`mutint_import.accessions.parse` reads the box, `mutint_import.sra_fetch.resolve` asks ENA
what each token is, and `sra_fetch.download` fetches the files -- see **Reads by accession
come from ENA** in mutint-core's CLAUDE.md for the design and `docs/plugin/staging.md` for
the guide. What is this plugin's is *when* each happens and *where the files land*.

**Resolve at launch, before the claim, for `_preflight`'s reason.** An accession ENA does not
know, a run with no FASTQ, a study past the caps -- each is refused while the answer is still
"fix the box", with no run row, no reads moved and the session still open, and with
`field: "accessions"` so the page can point at it. After the permission check, deliberately:
a reader must not be able to make the installation ask ENA on their behalf, which is also why
`preview` demands `can_edit_experiment` when the body carries accessions and only
`can_view_project` otherwise. What the row stores is the *resolved* plan (`BreseqRun.accessions`,
`sra.Plan.as_dict()` entries restricted to that row's runs), so the worker downloads what the
launch resolved rather than forming a second opinion; `read_files` goes on listing every
filename, ENA's included, so the run list needs no second reader.

**Download in the task, inside the log, after `breseq_path()` and before the dry run.** A run
is gigabytes, so a request that downloaded it would be bounded by nothing; the worker has the
log (`/jobs/<pk>/log` shows each file as it arrives), the cancellation flag (asked between
chunks, raising the same `Cancelled` the tool loop does) and a failure path that keeps the
directory. After `breseq_path()` so a machine with no breseq fails in a second rather than
after twenty gigabytes; before the dry run because breseq checks that its inputs exist. The
files land in `reads/`, beside whatever was dropped, so everything downstream -- the mate
check, trimming, breseq, the import -- treats a fetched file and a dropped one alike. ENA's
names (`<run>_1.fastq.gz`, `_2`) already pair under breseq's rule, which is why nothing renames
them. The run's deadline starts *after* the download: the budget guards a wedged tool, and a
stalled mirror is already guarded by the download's own read timeout.

**No session for a launch with nothing dropped.** The page opens a staging session only when
files were selected and posts a blank `upload_id` otherwise; `launch` reads that as "no staged
files" and the accessions as the rest. A session exists to receive bytes, and opening one to
close it unused would be the zero-file staging session `build_manifest`'s docstring calls a
caller's mistake -- so core's session code is untouched. The cost is one branch in `launch`
and that every refusal past that point abandons a session only if there is one. "Neither files
nor accessions" is refused in one sentence.

**Which runs are one sample is core's rule; what it is called passes through this plugin's.**
`sra.samples_in` makes one sample of every run under a sample or experiment accession and one
per BioSample in a study, and `sra.sample_name_for` names it by ENA's `sample_alias` --
`REL768A` for the LTEE's clones -- falling back to the accession when the alias fails the
`usable` test, which is `SAMPLE_NAME_RE` passed in from here: the name becomes a directory and
a coordinate, exactly as a typed one does. In the two single-sample modes an accession's files
simply join the one sample; in `read_names` mode each accession is a row of its own beside the
rows the dropped names derive. Two rows with one name -- an alias equal to a derived name, or
two accessions sharing an alias -- are refused as a clash rather than launched to supersede
each other. A dropped file named like a file about to be downloaded is refused too, at 409,
because both go into one `reads/`.

**The sample records the run, not the files.** `_record_read_sources` takes `download`'s
`{filename: run_accession}` and writes one `KIND_SRA` entry per run per read group in place
of a `KIND_READS` entry per file -- the shape `mutint_sample.inputs` promised when the kind was
reserved: the accession is what was given, and what it expanded to is the downloader's
business. Nothing on disk says which files were fetched; that map is in memory for the length
of the task and nowhere else.

**The preview asks ENA too**, once per `change` of the box rather than per keystroke, and
draws what each accession resolved to -- alias, title, runs, bytes -- in every mode, because a
person is deciding whether this is the run they meant before hours are spent on it. It is the
same `_accession_samples` the launch uses, so the two cannot disagree about a name.

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
- **No table of sample names to fill in.** One launch is several samples only when the read
  files carry the names -- see **One drop can be several samples**. There is no form for
  naming twenty samples by hand, because a filename is a better place to put a name than a
  row of boxes somebody retypes.
- **No anonymous use.** `breseq`, `launch`, `preview`, `runs` and `run_delete` all say
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

**199 tests**, and the end-to-end ones are affordable because of two things. The test runner
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
