"""A stand-in for fastp, on the same terms as `fake_breseq`.

What this plugin is responsible for is the command line -- which files are mates, which flags a
pair gets, where the output goes -- and that fastp's output lands where breseq then looks. So
the fake records every argv it is called with and copies each input to its named output, and
that is all. Appends rather than overwrites, because one run calls fastp once per read set.
"""

import os
import stat
import sys
import textwrap

SCRIPT = textwrap.dedent('''\
    #!{python}
    """Stand-in fastp. Copies -i to -o (and -I to -O), records its argv."""
    import json
    import os
    import shutil
    import subprocess
    import sys
    import time

    argv = sys.argv[1:]
    record = os.environ.get("FAKE_FASTP_ARGV")
    if record:
        with open(record, "a") as handle:
            handle.write(json.dumps({"argv": argv, "path": os.environ.get("PATH", "")}) + "\\n")

    # A trim that never finishes, for the cancellation test. Same shape as fake breseq's.
    if os.environ.get("FAKE_FASTP_SLEEP"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        with open(os.environ["FAKE_FASTP_PIDS"], "w") as handle:
            json.dump({"parent": os.getpid(), "child": child.pid}, handle)
        time.sleep(600)
        sys.exit(0)

    if os.environ.get("FAKE_FASTP_FAIL"):
        sys.stderr.write("fastp: adapter detection failed\\n")
        sys.exit(int(os.environ["FAKE_FASTP_FAIL"]))

    def value(flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    for source, target in (("-i", "-o"), ("-I", "-O")):
        if value(source):
            shutil.copyfile(value(source), value(target))
    for flag in ("-j", "-h"):
        if value(flag):
            with open(value(flag), "w") as handle:
                handle.write("{}")

    sys.stderr.write("Read1 before filtering:\\ntotal reads: 4\\n")
''')


def install(tools_dir):
    """Write the fake into ``<tools_dir>/bin/fastp`` and return its path."""
    bin_dir = os.path.join(tools_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    path = os.path.join(bin_dir, "fastp")
    with open(path, "w") as handle:
        # This interpreter, by absolute path, rather than `#!/usr/bin/env python3`: the kernel
        # resolves a shebang through PATH, and two tests empty PATH deliberately to prove that
        # `tool_path` falls back to it. Without this they fail on the fake refusing to start
        # rather than on the thing they are about.
        handle.write(SCRIPT.replace("{python}", sys.executable))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path
