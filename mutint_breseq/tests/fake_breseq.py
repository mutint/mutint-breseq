"""A stand-in for breseq, so the end-to-end path can be tested in milliseconds.

Real breseq on the smallest genome worth calling is minutes of CPU and needs bowtie2, samtools
and gnuplot beside it. What this plugin's own code is responsible for is everything *around*
the call -- the argv, the environment, the output check, the ingest, the cleanup -- so the
executable in the middle is the one part worth faking.

It is a real executable on disk rather than a `subprocess.run` patch on purpose: the two things
most likely to be wrong here are the argv and the PATH, and a patch would assert against the
call rather than against a process that actually has to start.

It records **one JSON line per call**, because a run invokes breseq twice: `--dry-run` first,
then the real thing.
"""

import os
import stat
import sys
import textwrap

SCRIPT = textwrap.dedent('''\
    #!{python}
    """Stand-in breseq. Copies a prepared sample folder to -o and records its argv."""
    import json
    import os
    import shutil
    import subprocess
    import sys
    import time

    argv = sys.argv[1:]
    template = os.environ["FAKE_BRESEQ_TEMPLATE"]
    record = os.environ["FAKE_BRESEQ_ARGV"]

    # **Appended, one JSON line per call**, the way fake_fastp already does: a run invokes
    # breseq twice now -- once for --dry-run and once for real -- and overwriting would leave
    # the tests unable to see the first or to prove the order of the two.
    apply_check = argv[argv.index("--apply-check") + 1] if "--apply-check" in argv else None
    with open(record, "a") as handle:
        handle.write(json.dumps(
            {"argv": argv, "path": os.environ.get("PATH", ""),
             "dry_run": "--dry-run" in argv, "apply_check": apply_check}) + "\\n")

    # The preflight: exit without doing anything, and without creating -o. Real breseq
    # validates the options and every path here; what matters to the tests is that it runs
    # first, creates nothing, and can be made to refuse.
    if "--dry-run" in argv:
        if os.environ.get("FAKE_BRESEQ_DRY_RUN_FAIL"):
            sys.stdout.write("Unknown command argument option: no-such-flag\\n")
            sys.exit(255)
        # A breseq from before g25617bb8, which is what a check's preflight has to name.
        if apply_check and os.environ.get("FAKE_BRESEQ_NO_APPLY_CHECK"):
            sys.stdout.write("Unknown command argument option: apply-check\\n")
            sys.exit(255)
        if apply_check and not os.path.isfile(apply_check):
            sys.stdout.write("---> ERROR Input file does not exist: " + apply_check + "\\n")
            sys.exit(255)
        sys.stdout.write("+++   DRY RUN PASSED -- options and paths are valid, "
                         "no work performed\\n")
        sys.exit(0)

    # A run that does not return, for the cancellation tests. It spawns a child first and
    # writes both pids out, because killing only the parent is the mistake this is here to
    # catch: breseq spawns bowtie2 and samtools, and `process.kill()` would leave them
    # running while the job reported itself stopped.
    if os.environ.get("FAKE_BRESEQ_SLEEP"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        with open(os.environ["FAKE_BRESEQ_PIDS"], "w") as handle:
            json.dump({"parent": os.getpid(), "child": child.pid}, handle)
        time.sleep(600)
        sys.exit(0)

    if os.environ.get("FAKE_BRESEQ_FAIL"):
        sys.stderr.write("breseq: something went wrong\\n")
        sys.stdout.write("a line of ordinary output\\n")
        sys.exit(int(os.environ["FAKE_BRESEQ_FAIL"]))

    output = argv[argv.index("-o") + 1]
    if os.path.exists(output):
        shutil.rmtree(output)
    shutil.copytree(template, output)

    # breseq's own HTML report, which is the one thing kept after a successful import.
    if not os.environ.get("FAKE_BRESEQ_NO_REPORT"):
        os.makedirs(os.path.join(output, "output", "evidence"), exist_ok=True)
        with open(os.path.join(output, "output", "index.html"), "w") as handle:
            handle.write("<html><body>fake breseq report</body></html>")
        with open(os.path.join(output, "output", "evidence", "e.html"), "w") as handle:
            handle.write("<html><body>evidence</body></html>")

    if os.environ.get("FAKE_BRESEQ_TRUNCATE"):
        os.remove(os.path.join(output, "data", "output.gd"))

    # A curation check. Real breseq applies the gd's mutations (skipping polymorphic ones
    # with a warning), aligns against the result, and writes every entry of output.gd with
    # `original_*` coordinates beside its own. The fake applies nothing but *shifts*: every
    # coordinate it writes is moved by the net length of the applied indels before it, and
    # the original_* field carries the unshifted value -- so a test can prove the ingest reads
    # positions from original_* rather than from the mutated frame. The residual mutations
    # come from FAKE_BRESEQ_CHECK_RESIDUAL (gd lines, in original coordinates); the
    # template's own mutations are *not* reported, since the check applied them.
    if apply_check:
        applied = []
        with open(apply_check) as handle:
            for line in handle:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\\n").split("\\t")
                if len(parts[0]) != 3:
                    continue
                fields = dict(p.split("=", 1) for p in parts[3:] if "=" in p)
                if fields.get("frequency") not in (None, "1", "1.0"):
                    sys.stderr.write("WARNING: skipping polymorphic mutation " + parts[1] + "\\n")
                    continue
                applied.append(parts)
        sys.stderr.write("APPLYING MUTATIONS::" + apply_check + "\\n")
        sys.stderr.write("Applied " + str(len(applied)) + " mutations.\\n")

        def shift(position):
            delta = 0
            for parts in applied:
                if int(parts[4]) >= position:
                    continue
                if parts[0] == "DEL":
                    delta -= int(parts[5])
                elif parts[0] == "INS":
                    delta += len(parts[5])
            return position + delta

        gd_path = os.path.join(output, "data", "output.gd")
        with open(gd_path) as handle:
            original_lines = handle.read().splitlines()
        out = [line for line in original_lines if line.startswith("#")]
        # The template's mutations are gone -- the check applied them -- and so is the
        # evidence they were called from: breseq would not see a junction for a deletion
        # that is now part of the reference.
        explained = set()
        for line in original_lines:
            parts = line.split("\\t")
            if not line.startswith("#") and len(parts[0]) == 3 and parts[2] != ".":
                explained.update(parts[2].split(","))
        next_id = 1
        for text in os.environ.get("FAKE_BRESEQ_CHECK_RESIDUAL", "").split("\\n"):
            if not text.strip():
                continue
            parts = text.split("\\t")
            position = int(parts[4])
            parts[1] = str(next_id); next_id += 1
            parts[4] = str(shift(position))
            parts.append("original_position=" + str(position))
            out.append("\\t".join(parts))
        for line in original_lines:
            if line.startswith("#"):
                continue
            parts = line.split("\\t")
            if len(parts[0]) != 2 or parts[1] in explained:
                continue
            parts[1] = str(next_id); next_id += 1
            if parts[0] in ("MC", "CN", "UN"):
                start, end = int(parts[4]), int(parts[5])
                parts[4], parts[5] = str(shift(start)), str(shift(end))
                parts += ["original_start=" + str(start), "original_end=" + str(end)]
            elif parts[0] in ("JC", "DP", "PD"):
                side_1, side_2 = int(parts[4]), int(parts[7])
                parts[4], parts[7] = str(shift(side_1)), str(shift(side_2))
                parts += ["original_side_1_position=" + str(side_1),
                          "original_side_2_position=" + str(side_2)]
                if os.environ.get("FAKE_BRESEQ_CHECK_OFFSET"):
                    parts.append("original_side_1_position_offset=3")
            else:
                position = int(parts[4])
                parts[4] = str(shift(position))
                parts.append("original_position=" + str(position))
            out.append("\\t".join(parts))
        with open(gd_path, "w") as handle:
            handle.write("\\n".join(out) + "\\n")
        with open(os.path.join(output, "data", "original_coordinates.tsv"), "w") as handle:
            handle.write("seq_id\\tapplied_start\\tapplied_end\\toriginal_start\\toriginal_end\\tstrand\\n")
        os.makedirs(os.path.join(output, "output"), exist_ok=True)
        with open(os.path.join(output, "output", "index.html"), "w") as handle:
            handle.write("<html><body>fake breseq check report</body></html>")

    sys.stdout.write("+++   SUCCESSFULLY COMPLETED\\n")
''')


def install(tools_dir):
    """Write the fake into ``<tools_dir>/bin/breseq`` and return its path.

    ``tools_dir`` is what `MUTINT_TOOLS_DIR` will be overridden to, so `tools.tool_path` finds
    this in preference to any real breseq the developer has on PATH.
    """
    bin_dir = os.path.join(tools_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    path = os.path.join(bin_dir, "breseq")
    with open(path, "w") as handle:
        # This interpreter, by absolute path, rather than `#!/usr/bin/env python3`: the kernel
        # resolves a shebang through PATH, and two tests empty PATH deliberately to prove that
        # `tool_path` falls back to it. Without this they fail on the fake refusing to start
        # rather than on the thing they are about.
        handle.write(SCRIPT.replace("{python}", sys.executable))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path
