"""End-to-end tests: compile each tests/programs/*.sd (and tests/programs/seadash/*.sd) to a
binary, run it, compare output. Those in tests/programs/ are valid Python whose output python3
gives too (test_python_parity.py checks it); those in seadash/ use seadash's own syntax, types
or behaviour.

For NAME.sd:
  NAME.out   expected stdout (required)
  NAME.err   expected stderr (optional; if present the program must exit with 1)
  NAME.exit  expected exit code (optional; overrides the above)

Programs are compiled in parallel once per test session (g++ is the slow part).
"""

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from seadash.driver import BuildError, BuildOptions, compile_cpp, translate

PROGRAMS = Path(__file__).parent / "programs"
PATHS = {p.stem: p for p in [*PROGRAMS.glob("*.sd"), *(PROGRAMS / "seadash").glob("*.sd")]}
CASES = sorted(PATHS)


@pytest.fixture(scope="session")
def binaries(tmp_path_factory) -> dict[str, Path | str]:
    """Build every program; maps name -> binary path, or an error message."""
    out_dir = tmp_path_factory.mktemp("programs")

    def build(name: str) -> Path | str:
        try:
            path = PATHS[name]
            result = translate(path.read_text(), path)
        except Exception as e:  # compile errors are test failures, with the message shown
            return f"seadash failed: {e}"
        cpp_path = out_dir / f"{name}.cpp"
        cpp_path.write_text(result.cpp)
        binary = out_dir / name
        try:
            # (debug builds, for speed, but no tracebacks: they'd show where the checkout is)
            compile_cpp(cpp_path, binary, BuildOptions(optimize=False, traceback=False), result.libs)
        except BuildError as e:
            return str(e)
        return binary

    with ThreadPoolExecutor(max_workers=os.cpu_count()) as pool:
        return dict(zip(CASES, pool.map(build, CASES)))


@pytest.mark.parametrize("name", CASES)
def test_program(name: str, binaries, tmp_path):
    binary = binaries[name]
    if isinstance(binary, str):
        pytest.fail(binary, pytrace=False)

    # Each program runs in its own empty directory, so file tests can write freely (with
    # certs/, the test certificates, for the HTTPS programs).
    shutil.copytree(PROGRAMS.parent / "certs", tmp_path / "certs")
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10, cwd=tmp_path)

    path = PATHS[name]
    expected_out = path.with_suffix(".out").read_text()
    err_file = path.with_suffix(".err")
    exit_file = path.with_suffix(".exit")
    expected_err = err_file.read_text() if err_file.exists() else ""
    if exit_file.exists():
        expected_code = int(exit_file.read_text())
    else:
        expected_code = 1 if err_file.exists() else 0

    assert result.stdout == expected_out
    assert result.stderr == expected_err
    assert result.returncode == expected_code
