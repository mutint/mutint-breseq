# Running breseq

The **Run breseq** tab of an experiment's Import data page takes FASTQ read files, runs breseq on them against
that experiment's reference genome, and imports the result as a sample. It is the same
analysis you would run at a terminal and then upload as a folder — done here, with the result
landing directly in the experiment.

## Before you can use it

**You must be signed in.** Running breseq creates data, so the rule is the same one that
governs creating a project or an experiment. Signed out, the page explains itself and offers
no form.

**The experiment needs a reference genome.** breseq calls mutations against one, so without it
there is nothing to run and the page says so instead of offering the form. Establish one by
dropping a GenBank, GFF3 or FASTA on the **Reference Sequence** tab of the experiment's Import data page.

**A worker has to be running.** The analysis takes hours, so it does not happen inside the web
request — it is queued, and `./mutint db_worker` is what picks it up. Nothing starts one for
you. If runs sit at *Queued*, that is why, and the page says so in as many words.

Start it in its own terminal, and **start it through `./mutint`**:

```bash
cd mutint
./mutint db_worker
```

Run as a bare `manage.py` command it has neither the database connection nor the path to
breseq, and every run fails identically.

## Launching a run

1. Open the experiment's **Import data** page and choose the **Run breseq** tab.
2. Choose an **Input type** and name the sample &mdash; see *Three ways to name a sample* below.
3. Select **Population sample** if these reads are a whole population rather than a clone.
4. Optionally select **Limit read-depth coverage** and name a fold coverage. Left deselected,
   every read is used; 60&ndash;80 is recommended for clonal samples.
5. Optionally type **breseq arguments**, and deselect **Trim reads with fastp first** if you do
   not want the reads trimmed.
6. Drop the sample's read files, and press **Run breseq**.

Everything you drop is one sample's reads — both mates of a pair, or several lanes. To analyze
a second sample, launch again.

## Three ways to name a sample

The **Input type** menu at the top of the form decides how the samples in a drop are named,
and shows only the boxes that way needs.

### Single sample with metadata from a name

Type the name in **Full Name**. The three boxes below it show how it will be read — which
population, which time point, which sample — and cannot be typed into; switch to the next
input type to set them yourself.

**The name is stored exactly as you type it.** It is also the name of the directory breseq
writes into, so it may use letters, digits, spaces, dot, underscore, plus and hyphen, and must
start with a letter or a digit.

### Single sample with specified population, time point

Fill in **Population**, **Time point** and **Sample**, and the name is built from them —
joined with underscores, and shown in the Full Name box, which is read-only in this mode.
Population and Time point offer the values this experiment already uses and take a new one
just as readily.

Leave Population and Time point empty and the sample is filed under **Unspecified** with no
time point. A name carries a population and a time point together or neither: there is no way
to write one without the other.

### One or more samples with metadata from read names

No name boxes at all: **each read file, or each pair of mates, is one sample**, named after the
file. Drop twenty files and launch ten samples in one go — each becomes a run of its own, with
its own entry on the Jobs page and its own Cancel.

What is removed from a filename to leave the sample's name is the extension, the read number,
the lane (`_L001`) and bcl2fastq's trailing chunk index (`_001`). Everything else is the name,
and it never ends in the `.`, `-` or `_` that introduced whatever was taken out.

**A period, a hyphen or an underscore separates them all the same**, so `SRR37077254.R1`,
`SRR37077254_R1` and `SRR37077254-R1` are each the sample `SRR37077254` — with or without a
mate in the drop, which matters because single-end reads are common and there is then nothing
to compare against.

**Only decorations at the end of the name are removed.** The scan stops at the first thing that
is not one, so a population genuinely called `R1` survives: `R1_500gen_x` is read as population
R1, time point 500, sample x. When both mates *are* present, the read number is also found by
comparing the two names, which is what catches a bare `lane_1.fq`/`lane_2.fq` with no `R` to
announce itself.

| dropped | sample | placed as |
|---|---|---|
| `Ara-2_500gen_763A_R1.fastq.gz`, `..._R2...` | `Ara-2_500gen_763A` | Ara-2, time point 500, sample 763A |
| `pop3_day7_clone2_R1.fq.gz`, `..._R2...` | `pop3_day7_clone2` | pop3, time point 7, sample clone2 |
| `3-30000-1-1_R1.fastq.gz`, `..._R2...` | `3-30000-1-1` | 3, time point 30000, sample 1-1 |
| `lane_1.fq`, `lane_2.fq` | `lane` | Unspecified, no time point |
| `SRR37077254.R1.fastq.gz`, `...R2...` | `SRR37077254` | Unspecified, no time point |
| `S12_L001_R1_001.fastq.gz` and three more | `S12` | Unspecified, no time point |

That last row is worth reading twice. Illumina's own filenames carry a sample-sheet index and
nothing else, so there is no population and no time point to find — the sample lands under
**Unspecified**, which is fine for a one-off and worth correcting on the sample's edit page for
a series. All four of its files are one sample: two lanes of one library are one sample, not
two.

**The table under the drop zone says what will happen before anything is uploaded** — one row
per sample, the files that make it, where it will be placed, and whether it replaces a sample
the experiment already holds. Read it. If a name is not being split the way you expected,
renaming the files is quicker than correcting the samples afterwards.

**The Population sample checkbox applies to every sample in the drop.** Read names say nothing
about clonality — a drop that mixes clones and populations is two launches.

### What a name places, and what it does not

Whichever input type you use, the same two shapes place a sample on its population and time
point automatically:

| name | population | time point | sample |
|---|---|---|---|
| `3-30000-1-1` | 3 | 30000 | 1-1 |
| `Ara-2_500gen_763A` | Ara-2 | 500 | 763A |

The first is four whole numbers separated by hyphens. The second is three fields separated by
underscores, where the middle one is a number — and it may wear its unit on either side, so
`500gen`, `day7`, `t12` and `h24` are all read as numbers, with the unit discarded. A time
point is one unit-less number, so an experiment that mixes days and generations has two time
axes and no way to say so.

Anything else is filed under **Unspecified** with no time point. That is fine for a one-off and
worth avoiding for a series, because analyses that read along a population need a time axis to
read along. **Fixed Mutations in particular can find nothing** in an experiment whose samples
all sit at one time point: a mutation is fixed if it is in the last two, and there is no "last
two" of one. You can correct it afterwards on the sample's edit page.

Letters, digits, spaces, dot, underscore, plus and hyphen; it must start with a letter or
digit. **Spaces are allowed** in a population or sample name — `Ara 2` is a name the rest of
MutInt has always accepted, and the launcher no longer refuses it.

### Clone or population

**Population sample** is the one question about the analysis that the page asks outright,
because it is the one that changes what the numbers mean. A clone is one genotype; a
population is a whole evolving culture sequenced together, so its mutations carry frequencies
rather than being simply present.

Selecting it does two things. breseq is run with `-p`, its polymorphism mode, so it predicts
mixed mutations as well as consensus ones. And the sample that lands is **recorded as a
population**, which is what puts a frequency column on its mutation table and what the
Clonal / Mixed filter on Compare selects by. You can change it afterwards on the sample's edit
page, where it is called **Mixed**.

Leave it deselected for a clone, which is the default.

You can also just type `-p` yourself, and that goes on working: breseq runs the same way, and
the sample is recorded as a population as long as it is new. Two things the checkbox does that
typing the flag does not: it also covers **re-running over a sample that already exists** --
correcting a clone's analysis to a population's -- and it works with either spelling of the
flag. If you select the box and type the flag as well, what you typed is left exactly as it is
and nothing is repeated.

### Limiting coverage

A deep run wastes time: past a point, more reads make the analysis slower without finding
anything more. **Limit read-depth coverage** tells breseq to use only enough reads to reach the
fold coverage you name, and to ignore the rest.

**The check box is what decides whether coverage is limited at all.** Left deselected — which
is how the page starts — every read is used, which is breseq's own default and is always safe.
Selecting it enables the number beside it and fills in 80, which you can change.

**60&ndash;80 is the recommendation for clonal samples**, and it is usually a large speed-up
with no loss of sensitivity. The actual coverage achieved comes out somewhat lower, because
not every read maps. For a population sample, where a mutation at 5% has to be told from
noise, think before you cut anything: depth is what that distinction is made of.

The box takes a number and nothing else. It is passed as `-l`; typing `-l` or
`--limit-fold-coverage` into the arguments box yourself does the same job, and what you typed
wins over the box.

### The arguments box

Passed to breseq as you type it. `-o` and `-r` are supplied for you — the output directory and
the experiment's reference — and `-j` defaults to every processor on the machine unless you
name one yourself.

Common ones:

- `-j 4` — use four processors rather than all of them, to leave the machine usable.

breseq's own documentation lists the rest. Nothing is passed through a shell, so quoting works
the way it does in a terminal and nothing else in the box can have any other effect.

### When two files are not really a pair

Files are paired by their names — two names of the same length differing in one place, where
one has a `1` and the other a `2`. That is breseq's own rule, and names are all it has to go on.

Before anything is trimmed, each apparent pair is checked: the two files must hold the **same
number of reads**, and must start with the **same read**. If they do not, they are analysed as
**two single-end files** instead of as a pair, and the run says so in its row of the run list.

Nothing is discarded, and that is the point. Handed two files that pair by name but hold
different numbers of reads, **both breseq and fastp quietly use only as many reads as the
shorter file holds and exit successfully** — so without this check a run reports success, the
sample imports, the mutation table looks perfectly ordinary, and a chunk of the data is simply
missing. Each tool does print a warning, in the middle of several hundred lines nobody reads.

The usual cause is a download that was interrupted or a file that was copied while it was still
being written. The reads are worth analysing as far as they go, which is why the run continues —
but if you were expecting a paired analysis, this is the note that tells you why you did not get
one, and re-downloading the files and running again is the fix.

### Trimming

By default the reads are run through [fastp](https://github.com/OpenGene/fastp) before breseq
sees them. It trims adapters and nothing else: quality filtering is off, because it has been
seen to gut older data sets, and no length filter or deduplication is applied. What breseq
receives is the same files under the same names, adapter-free.

**Pairs are trimmed as pairs.** Two files are mates when breseq would read them as mates, and
breseq's rule is simple: their names are the same length and differ in exactly one place, where
one has a `1` and the other a `2`. `sample_R1.fastq.gz` and `sample_R2.fastq.gz` qualify, so do
`lane_1.fq` and `lane_2.fq`, and a file that could be paired two ways is treated as unpaired --
exactly as breseq treats it. A pair is trimmed with fastp's paired-end adapter detection.

**Long reads are left alone.** A file whose first reads include one of 1000 bases or more --
breseq's own threshold for a long-read file -- is passed to breseq untrimmed, and the run log
says so. Files that are not FASTQ by name, such as aligned SAM under `--aligned-sam`, pass
through as well. Compression is untouched: a gzipped file comes out gzipped.

If fastp fails the run fails, with fastp's output in the log, rather than quietly running breseq
on the untrimmed reads. Deselect the box to skip trimming altogether.

## Re-running a sample you already have

If the coordinate matches a sample already in the experiment, the page says so before you
launch. That is a warning and not a refusal: importing a sample that already exists **replaces
its mutations** — the old calls are cleared and the new ones written — which is exactly what
you want when you are re-running breseq on the same reads with better options. Nothing else
about the sample changes, and no second sample appears.

Launching again for a sample whose run is still queued or running **stops the earlier run**.
Its output was about to be overwritten by yours, so finishing it would be hours of computer
time spent on something thrown away.

After a launch the files clear and everything else stays, so the next sample in a batch is
usually one box to edit.

## If breseq will not accept your options

The box is handed to breseq before anything else happens, and breseq is asked whether it would
accept it. A flag it does not recognise, or a path it cannot use, comes back as an error beside
the form straight away -- with breseq's own words -- and **nothing is consumed**: no run is
created and your upload is still there, so fix the box and press Launch again.

It checks that the options exist and that the files do. It does not check that a value makes
sense: `-j notanumber` gets past it and fails later.

## Watching it

The run list under the form updates itself while anything is in flight:

- **Queued** — waiting for a worker. If it says a worker is not running, that is the problem.
- **Running** — breseq has started. Hours is normal for a bacterial genome.
- **Imported** — done, with links to the sample's **Mutations** and to breseq's own report.
- **Failed** — with the reason, and breseq's output behind a fold.

Every row also has a **log** link, which is what fastp and breseq have printed so far — the
command line that was run, and their output as it arrives. It works while the run is going, so
it is the way to see how far a long run has got; the page has a Refresh button rather than
updating itself, and a Download for the whole log. How much has appeared is up to breseq,
which writes in blocks rather than a line at a time.

## Stopping a run

Every run appears on the **Jobs** page, reached from your username in the sidebar. While it is
queued or running there is a **Cancel** button.

Cancelling a run that has already started stops breseq and everything it launched — bowtie2
and samtools included — and it is not instant: the run checks between slices of work, so there
is a second or two before it notices. The page says *stopping…* meanwhile.

breseq's own HTML report is kept for the sample the run produced, and is reached from that
sample's mutations page rather than from here — see *Reading breseq's report*. Deleting a run
does not take it away, because it belongs to the sample.

**A cancelled run keeps nothing.** The reads and the partial breseq output are deleted, which
is the one way cancelling differs from failing: a failed run is something to look at, and a
cancelled one is something you decided you did not want. Relaunching means uploading the reads
again.

A run that is still queued or running **cannot be deleted** — cancel it first. Deleting it
would pull the reads out from under breseq while it was reading them, and the failure that
followed would name neither the cause nor the person who caused it.

## What is kept

After a successful import, **the run directory is emptied** — the reads, the trimmed copies and
breseq's working data go, and breseq's HTML report has already been stored under the sample. That is not a loss: the mutations, the alignment and the
reference are all in the store under the sample by then, which is what the mutation tables and
the genome browser read.

A **failed** run keeps everything, which is exactly when you want it: the reads are still
there, so you can look at what breseq was given.

Deleting a run from the list removes its report and its record. **It does not delete the
sample** — a run is a record of how a sample was made, and removing that record should not
remove the data. Delete the sample itself from the mutation editor if that is what you meant.

## What it does not do

- **It does not name several samples for you by hand.** Two of the three input types are one
  sample per launch; the third takes as many as the drop holds, but only because the read
  names carry the names. There is no table of boxes to fill in.
- **It does not choose the reference.** Every run uses the experiment's own, which is what
  makes the resulting samples comparable with everything else in it.
