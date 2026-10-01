"""The `sd` command: reading programs from stdin, running with arguments."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).parent.parent


def sd(args: list[str], stdin: str, cwd, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "seadash.cli", *args], input=stdin, capture_output=True, text=True, cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})},
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


def test_package_data_covers_the_runtime():
    # A wheel ships only what pyproject.toml lists; without the runtime headers nothing builds.
    import tomllib
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["seadash"]
    package = ROOT / "seadash"
    shipped = {path for pattern in patterns for path in package.glob(pattern)}
    assert {path for path in (package / "runtime").rglob("*") if path.is_file()} <= shipped


def test_formatdate_localtime_follows_tz(tmp_path):
    """formatdate(localtime=True) uses the local zone (kept out of tests/programs, which can't set TZ).
    The expected lines are python3's, with the same TZ."""
    program = "from email.utils import formatdate\nprint(formatdate(1700000000, localtime=True), formatdate(1720000000, True, True))\n"
    expected = {
        "IST-5:30": "Wed, 15 Nov 2023 03:43:20 +0530 Wed, 03 Jul 2024 15:16:40 +0530\n",
        "EST5EDT,M3.2.0,M11.1.0": "Tue, 14 Nov 2023 17:13:20 -0500 Wed, 03 Jul 2024 05:46:40 -0400\n",
        "UTC0": "Tue, 14 Nov 2023 22:13:20 +0000 Wed, 03 Jul 2024 09:46:40 +0000\n",
    }
    for tz, out in expected.items():
        r = sd(["run"], program, tmp_path, env={"TZ": tz})
        assert (r.returncode, r.stdout, r.stderr) == (0, out, "")
