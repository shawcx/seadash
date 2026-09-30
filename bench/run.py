"""Benchmark seadash against CPython.

Every bench/*.sd must also be a valid Python program. Each one is compiled with
`sd build` (-O3), run with both, checked for identical output, and timed (best
of N runs, wall clock).

    .venv/bin/python bench/run.py            # all benchmarks
    .venv/bin/python bench/run.py lists      # names containing "lists"
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BENCH = Path(__file__).parent
SD = Path(sys.executable).parent / "sd"
RUNS = 3


def run(cmd: list[str]) -> tuple[float, str]:
    """(wall seconds, stdout)."""
    start = time.perf_counter()
    env = {**os.environ, "PYTHONPATH": str(BENCH / "python")}  # (`from seadash import value` under python3)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=env)
    return time.perf_counter() - start, result.stdout


def best(cmd: list[str]) -> tuple[float, str]:
    runs = [run(cmd) for _ in range(RUNS)]
    return min(r[0] for r in runs), runs[0][1]


def main() -> int:
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    programs = sorted(p for p in BENCH.glob("*.sd") if pattern in p.stem)
    if not programs:
        print(f"no benchmarks match {pattern!r}")
        return 1
    print(f"{'benchmark':<16} {'python3':>9} {'seadash':>9} {'speedup':>8}   {'compile':>7}")
    ok = True
    with tempfile.TemporaryDirectory(prefix="seadash-bench-") as tmp:
        for prog in programs:
            binary = Path(tmp) / prog.stem
            start = time.perf_counter()
            subprocess.run([str(SD), "build", "--no-cache", str(prog), "-o", str(binary)], check=True)
            compile_time = time.perf_counter() - start
            py_time, py_out = best(["python3", str(prog)])
            sd_time, sd_out = best([str(binary)])
            same = py_out == sd_out
            ok = ok and same
            note = "" if same else "   OUTPUT DIFFERS"
            print(f"{prog.stem:<16} {py_time:>8.2f}s {sd_time:>8.2f}s {py_time / sd_time:>7.1f}x   {compile_time:>6.1f}s{note}")
    print(f"(best of {RUNS} runs; seadash built with -O3)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
