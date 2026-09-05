"""A stand-in for breseq, so the end-to-end path can be tested in milliseconds.

Real breseq on the smallest genome worth calling is minutes of CPU and needs bowtie2, samtools
and gnuplot beside it. What this plugin's own code is responsible for is everything *around*
the call -- the argv, the environment, the output check, the ingest, the cleanup -- so the
executable in the middle is the one part worth faking.

It is a real executable on disk rather than a `subprocess.run` patch on purpose: the two things
most likely to be wrong here are the argv and the PATH, and a patch would assert against the
call rather than against a process that actually has to start.
"""

import os
import stat
import textwrap

SCRIPT = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Stand-in breseq. Copies a prepared sample folder to -o and records its argv."""
    import json
    import os
    import shutil
    import sys

    argv = sys.argv[1:]
    template = os.environ["FAKE_BRESEQ_TEMPLATE"]
    record = os.environ["FAKE_BRESEQ_ARGV"]

    with open(record, "w") as handle:
        json.dump({"argv": argv, "path": os.environ.get("PATH", "")}, handle)

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

    sys.stdout.write("+++   SUCCESSFULLY COMPLETED\\n")
''')


def install(tools_dir):
    """Write the fake into ``<tools_dir>/bin/breseq`` and return its path.

    ``tools_dir`` is what `ALEDB_TOOLS_DIR` will be overridden to, so `tools.tool_path` finds
    this in preference to any real breseq the developer has on PATH.
    """
    bin_dir = os.path.join(tools_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    path = os.path.join(bin_dir, "breseq")
    with open(path, "w") as handle:
        handle.write(SCRIPT)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path
