# mutint-breseq

Run [breseq](https://github.com/barricklab/breseq) on uploaded reads, from inside
[MutInt](https://github.com/mutint/mutint-core).

It is the one component that is **upstream of core's importer rather than downstream of it**: it
produces a breseq output folder and hands it to `mutint_import.breseq_folder`, so the sample that
lands is the same sample a manual upload would produce. It parses nothing and writes no
`Mutation` rows itself.

Its launcher is a tab on the experiment's **Import data** page — running breseq is one more way
of getting a sample in — because it needs a sample name and a command line that no import handler
can carry.

**It needs a worker.** A run takes hours, so it is enqueued on `django.tasks`; unlike coverage
derivation, a run nobody picks up simply never happens, so the run list asks the queue and says
*waiting for a worker* rather than leaving `queued` to mean two different things. `./mutint start`
runs one for you; anywhere else, run `./mutint db_worker`.

External tools come from `tools.txt` (breseq, fastp) and are installed by the entry script.

## Installing

```bash
git submodule add ../mutint-breseq mutint-breseq
```

MIT licensed. See [mutint-core](https://github.com/mutint/mutint-core) for the platform.
