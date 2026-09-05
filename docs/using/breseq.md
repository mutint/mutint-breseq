# Running breseq

**Run breseq** in an experiment's sidebar takes FASTQ read files, runs breseq on them against
that experiment's reference genome, and imports the result as a sample. It is the same
analysis you would run at a terminal and then upload as a folder — done here, with the result
landing directly in the experiment.

## Before you can use it

**The experiment needs a reference genome.** breseq calls mutations against one, so without it
there is nothing to run and the page says so instead of offering the form. Establish one by
dropping a GenBank, GFF3 or FASTA on the experiment's **Add data** page.

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

1. Open **Run breseq** with an experiment selected.
2. Type a **sample name**. This is what the sample is called everywhere in ALEdb.
3. Optionally type **breseq arguments**.
4. Drop the sample's read files, and press **Run breseq**.

Everything you drop is one sample's reads — both mates of a pair, or several lanes. To analyse
a second sample, launch again.

### The sample name is worth thinking about

Two shapes place the sample on its ALE and time point automatically:

| name | ALE | time point | isolate |
|---|---|---|---|
| `3-30000-1-1` | 3 | 30000 | 1 |
| `Ara-2_500gen_763A` | Ara-2 | 500 | 763A |

Anything else is auto-numbered onto ALE 1, time point 1 — which is fine for a one-off, and is
worth avoiding for a series, because analyses that read along an ALE need a time axis to read
along. **Fixed Mutations in particular can find nothing** in an experiment whose samples all
sit at one time point: a mutation is fixed if it is in the last two, and there is no "last
two" of one. You can correct it afterwards on the sample's edit page.

Letters, digits, dot, underscore, plus and hyphen; it must start with a letter or digit.

### The arguments box

Passed to breseq as you type it. `-o` and `-r` are supplied for you — the output directory and
the experiment's reference — and `-j` defaults to every processor on the machine unless you
name one yourself.

Common ones:

- `-p` — call polymorphisms as well as consensus mutations, for a population sample rather
  than a clone.
- `-j 4` — use four processors rather than all of them, to leave the machine usable.
- `-l 80` — trim coverage to 80-fold before calling.

breseq's own documentation lists the rest. Nothing is passed through a shell, so quoting works
the way it does in a terminal and nothing else in the box can have any other effect.

## Watching it

The run list under the form updates itself while anything is in flight:

- **Queued** — waiting for a worker. If it says a worker is not running, that is the problem.
- **Running** — breseq has started. Hours is normal for a bacterial genome.
- **Imported** — done, with links to the sample's **Mutations** and to breseq's own report.
- **Failed** — with the reason, and breseq's output behind a fold.

## What is kept

After a successful import, **breseq's HTML report is kept and everything else is deleted** —
the reads and breseq's working data. That is not a loss: the mutations, the alignment and the
reference are all in the store under the sample by then, which is what the mutation tables and
the genome browser read.

A **failed** run keeps everything, which is exactly when you want it: the reads are still
there, so you can look at what breseq was given.

Deleting a run from the list removes its report and its record. **It does not delete the
sample** — a run is a record of how a sample was made, and removing that record should not
remove the data. Delete the sample itself from the mutation editor if that is what you meant.

## What it does not do

- **It does not analyse several samples at once.** One launch is one sample; launch again for
  the next. Several launches queue and run in order.
- **It cannot be cancelled.** The process belongs to the worker; stopping the worker is what
  stops a run, and it will be re-attempted.
- **It does not choose the reference.** Every run uses the experiment's own, which is what
  makes the resulting samples comparable with everything else in it.
