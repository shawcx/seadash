"""The `sd` command: reading programs from stdin, running with arguments."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).parent.parent


def sd(args: list[str], stdin: str, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "seadash.cli", *args], input=stdin, capture_output=True, text=True, cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def test_run_reads_stdin(tmp_path):
    (tmp_path / "helpers.sd").write_text(textwrap.dedent("""
        def shout(f: Callable[[str], str]) -> Callable[[str], str]:
            return lambda s: f(s).upper()

        @shout
        def greet(name: str) -> str:
            return "hi " + name
    """))
    program = "import sys\nimport helpers\nprint(helpers.greet('stdin'), sys.argv[1:])\n"
    r = sd(["run"], program, tmp_path)  # imports are found in the current directory
    assert (r.returncode, r.stdout, r.stderr) == (0, "HI STDIN []\n", "")
    r = sd(["run", "-", "a", "b"], program, tmp_path)
    assert r.stdout == "HI STDIN ['a', 'b']\n"


def test_stdin_errors_name_stdin(tmp_path):
    r = sd(["run"], "x = 1\nx.foo()\n", tmp_path)
    assert r.returncode == 1
    assert r.stderr.startswith("<stdin>:2:3: error: int has no method 'foo'")
    r = sd(["check", "-"], "print(1)\n", tmp_path)
    assert r.returncode == 0 and r.stderr == "<stdin>: ok\n"
