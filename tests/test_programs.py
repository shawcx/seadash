"""End-to-end tests: compile each tests/programs/*.sd to a binary, run it, compare output.

For NAME.sd:
  NAME.out   expected stdout (required)
  NAME.err   expected stderr (optional; if present the program must exit with 1)
  NAME.exit  expected exit code (optional; overrides the above)

Programs are compiled in parallel once per test session (g++ is the slow part).
"""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from seadash.driver import BuildError, BuildOptions, compile_cpp, to_cpp

PROGRAMS = Path(__file__).parent / "programs"
CASES = sorted(p.stem for p in PROGRAMS.glob("*.sd"))


@pytest.fixture(scope="session")
def binaries(tmp_path_factory) -> dict[str, Path | str]:
    """Build every program; maps name -> binary path, or an error message."""
    out_dir = tmp_path_factory.mktemp("programs")

    def build(name: str) -> Path | str:
        try:
            cpp = to_cpp((PROGRAMS / f"{name}.sd").read_text())
        except Exception as e:  # compile errors are test failures, with the message shown
            return f"seadash failed: {e}"
        cpp_path = out_dir / f"{name}.cpp"
        cpp_path.write_text(cpp)
        binary = out_dir / name
        try:
            compile_cpp(cpp_path, binary, BuildOptions(optimize=False))
        except BuildError as e:
            return str(e)
        return binary

    with ThreadPoolExecutor(max_workers=os.cpu_count()) as pool:
        return dict(zip(CASES, pool.map(build, CASES)))


@pytest.mark.parametrize("name", CASES)
def test_program(name: str, binaries):
    binary = binaries[name]
    if isinstance(binary, str):
        pytest.fail(binary, pytrace=False)

    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)

    expected_out = (PROGRAMS / f"{name}.out").read_text()
    err_file = PROGRAMS / f"{name}.err"
    exit_file = PROGRAMS / f"{name}.exit"
    expected_err = err_file.read_text() if err_file.exists() else ""
    if exit_file.exists():
        expected_code = int(exit_file.read_text())
    else:
        expected_code = 1 if err_file.exists() else 0

    assert result.stdout == expected_out
    assert result.stderr == expected_err
    assert result.returncode == expected_code
