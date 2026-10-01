"""The checks to run before committing (CLAUDE.md), in one command:

    .venv/bin/python tools/check.py               # everything
    .venv/bin/python tools/check.py asan tsan     # some of them
    .venv/bin/python tools/check.py -k http       # only the programs whose names contain "http"

  tests     the test suite (pytest)
  warnings  the C++ for every test program, benchmark and example compiles with no
            warnings under -Wall -Wextra, with and without -DSD_TRACEBACK
  asan      every test program, built with AddressSanitizer and UndefinedBehaviorSanitizer
            (and -DSD_TRACEBACK, whose frames on the stack give them more to check), runs clean and gives its expected output, errors and exit code
  tsan      the same with ThreadSanitizer, for the programs that use threads

Exits with 1 if anything failed. Linux and macOS (ThreadSanitizer needs ASLR off on Linux:
setarch -R).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from seadash.driver import RUNTIME_DIR, find_cxx, library_paths, translate  # noqa: E402
from seadash.errors import CompileError  # noqa: E402

PROGRAMS = ROOT / "tests" / "programs"
FLAGS = ["-std=c++23", "-fwrapv", "-ffp-contract=off"]
SANITIZER_REPORT = re.compile(r"(ERROR: \w+Sanitizer[^\n]*|WARNING: ThreadSanitizer[^\n]*|runtime error:[^\n]*)")
TRACEBACK_LINE = re.compile(r"^(Traceback \(most recent call last\):|  .*)\n", re.M)  # (-DSD_TRACEBACK's)
THREAD_USE = re.compile(r"\b(threading|queue|concurrent\.futures|ThreadPoolExecutor|ThreadingHTTPServer|Mutex|Atomic|Synchronized|signal)\b")


def programs(pattern: str) -> list[Path]:
    return [p for p in sorted([*PROGRAMS.glob("*.sd"), *(PROGRAMS / "seadash").glob("*.sd")]) if pattern in p.stem]


def emit(path: Path, out: Path):
    """The program's C++ (written to out/NAME.cpp) and its libraries, or an error message."""
    try:
        t = translate(path.read_text(), path)
    except CompileError as e:
        return None, f"seadash failed: {e.message}"
    cpp = out / f"{path.parent.name}_{path.stem}.cpp"
    cpp.write_text(t.cpp)
    return (cpp, t.libs), None


def build(cxx: str, cpp: Path, binary: Path, flags: list[str], libs: list[str]) -> str | None:
    """Compile and link; the compiler's errors if it failed."""
    include_dirs, lib_dirs = library_paths() if libs else ([], [])
    cmd = [cxx, *FLAGS, *flags, *include_dirs, f"-I{RUNTIME_DIR}", str(cpp), "-o", str(binary), *lib_dirs,
           *(f"-l{lib}" for lib in libs)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return (r.stderr or "failed") if r.returncode else None


def check_warnings(cxx: str, pattern: str, out: Path, jobs: int) -> list[str]:
    files = programs(pattern) + [p for d in ("bench", "examples") for p in sorted((ROOT / d).glob("*.sd")) if pattern in p.stem]

    def one(path: Path) -> str | None:
        built, error = emit(path, out)
        if error:
            return f"{path.relative_to(ROOT)}: {error}"
        cpp, _ = built
        for extra in ([], ["-DSD_TRACEBACK"]):
            r = subprocess.run([cxx, *FLAGS, *extra, "-O1", "-Wall", "-Wextra", "-fsyntax-only", f"-I{RUNTIME_DIR}",
                                *library_paths()[0], str(cpp)], capture_output=True, text=True)
            if r.returncode or "warning:" in r.stderr:
                first = next((line for line in r.stderr.splitlines() if "warning:" in line or "error:" in line), r.stderr[:300])
                return f"{path.relative_to(ROOT)}{' (-DSD_TRACEBACK)' if extra else ''}: {first}"
        return None

    with ThreadPoolExecutor(jobs) as pool:
        problems = [p for p in pool.map(one, files) if p]
    print(f"warnings: {len(files)} programs, {len(problems)} with warnings")
    return problems


def check_sanitizer(cxx: str, kind: str, pattern: str, out: Path, jobs: int) -> list[str]:
    flags = ["-DSD_TRACEBACK", "-O1", "-g", "-fno-omit-frame-pointer",
             "-fsanitize=address,undefined" if kind == "asan" else "-fsanitize=thread"]
    files = programs(pattern)
    if kind == "tsan":
        files = [p for p in files if THREAD_USE.search(p.read_text())]
    env = dict(os.environ)
    if kind == "asan":
        env["UBSAN_OPTIONS"] = "print_stacktrace=1"
        if sys.platform == "linux":
            env["ASAN_OPTIONS"] = "detect_leaks=1"  # (LeakSanitizer isn't on macOS)
    else:
        env["TSAN_OPTIONS"] = "halt_on_error=1"

    def one(path: Path) -> str | None:
        built, error = emit(path, out)
        if error:
            return f"{path.stem}: {error}"
        cpp, libs = built
        binary = out / f"{path.stem}.{kind}"
        if failure := build(cxx, cpp, binary, flags, libs):
            return f"{path.stem}: build failed\n{failure[-600:]}"
        cmd = [str(binary)]
        if kind == "tsan" and sys.platform == "linux":
            cmd = ["setarch", os.uname().machine, "-R", *cmd]
        with tempfile.TemporaryDirectory() as cwd:
            shutil.copytree(PROGRAMS.parent / "certs", Path(cwd) / "certs")  # (the HTTPS programs' certificate)
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=180, cwd=cwd, env=env)
            except subprocess.TimeoutExpired:
                return f"{path.stem}: timed out"
        if m := SANITIZER_REPORT.search(r.stderr):
            return f"{path.stem}: {m.group(1)}\n" + "\n".join(r.stderr.splitlines()[:16])
        err_file, exit_file = path.with_suffix(".err"), path.with_suffix(".exit")
        expected_err = err_file.read_text() if err_file.exists() else ""
        expected_code = int(exit_file.read_text()) if exit_file.exists() else (1 if err_file.exists() else 0)
        if r.stdout != path.with_suffix(".out").read_text():
            return f"{path.stem}: output differs from {path.stem}.out"
        if TRACEBACK_LINE.sub("", r.stderr) != expected_err or r.returncode != expected_code:
            return f"{path.stem}: stderr or exit code differs (exit {r.returncode})\n{r.stderr[:400]}"
        return None

    with ThreadPoolExecutor(jobs) as pool:
        problems = [p for p in pool.map(one, files) if p]
    print(f"{kind}: {len(files)} programs, {len(problems)} failed")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the checks to do before committing.")
    parser.add_argument("checks", nargs="*", metavar="CHECK", help="tests, warnings, asan, tsan (default: all of them)")
    parser.add_argument("-k", dest="pattern", default="", help="only the programs whose names contain this")
    parser.add_argument("-j", dest="jobs", type=int, default=os.cpu_count() or 4, help="programs built at once")
    parser.add_argument("--keep", metavar="DIR", help="keep the generated C++ and binaries in DIR")
    args = parser.parse_args()
    known = ["tests", "warnings", "asan", "tsan"]
    if unknown := [c for c in args.checks if c not in known]:
        parser.error(f"unknown check {unknown[0]!r} (choose from {', '.join(known)})")
    checks = args.checks or known
    problems: list[str] = []
    if "tests" in checks:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", str(ROOT / "tests"), *(["-k", args.pattern] if args.pattern else [])],
                           capture_output=True, text=True, cwd=ROOT)
        summary = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()[-300:]
        print(f"tests: {summary}")
        if r.returncode not in (0, 5):  # (5: nothing matched -k)
            problems.append(f"tests: {summary}")
    cxx = find_cxx()
    with tempfile.TemporaryDirectory(prefix="seadash-check-") as tmp:
        out = Path(args.keep) if args.keep else Path(tmp)
        out.mkdir(parents=True, exist_ok=True)
        if "warnings" in checks:
            problems += check_warnings(cxx, args.pattern, out, args.jobs)
        for kind in ("asan", "tsan"):
            if kind in checks:
                problems += check_sanitizer(cxx, kind, args.pattern, out, max(1, args.jobs // 2))
    for p in problems:
        print(f"\n--- {p}")
    print("\nall clean" if not problems else f"\n{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
