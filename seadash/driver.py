"""The full pipeline: seadash source -> C++ -> native binary."""

import fcntl
import functools
import hashlib
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from . import builtins, codegen, threads
from .checker import ImportCycle, check
from .errors import CompileError
from .parser import parse

RUNTIME_DIR = Path(__file__).parent / "runtime"

# Compilers to try, newest first. Override with the SEADASH_CXX environment variable.
CXX_CANDIDATES = ["g++-15", "g++-14", "clang++-20", "clang++-19", "g++", "clang++"]


class BuildError(Exception):
    pass


@dataclass
class Translation:
    cpp: str
    libs: list[str]  # libraries the program must link with, e.g. ["z"]


class Program:
    """Finds, parses and checks the .sd modules a program imports.

    `import geometry.shapes` means geometry/shapes.sd, next to the main file. Each module
    is checked once; `units` ends up in dependency order (a module after everything it imports).
    """

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.loaded: dict[str, builtins.UserModule | None] = {}
        self.loading: list[str] = []
        self.units: list[codegen.ModuleUnit] = []
        self.paths: dict[str, str] = {}
        self.sources: dict[str, str] = {}

    def load(self, name: str) -> builtins.UserModule | None:
        if name in self.loading:
            raise ImportCycle([*self.loading[self.loading.index(name):], name])
        if name in self.loaded:
            return self.loaded[name]
        path = self.base_dir.joinpath(*name.split(".")).with_suffix(".sd")
        if not path.is_file():
            self.loaded[name] = None
            return None
        source = path.read_text(encoding="utf-8")
        self.paths[name], self.sources[name] = str(path), source
        self.loading.append(name)
        try:
            module = parse(source)
            info = check(module, name, self.load)
        except CompileError as e:
            if e.file is None:  # the error is in this module (not one it imports)
                e.file, e.source = str(path), source
            raise
        finally:
            self.loading.pop()
        namespace = "sdm::" + "::".join(codegen.ident(part) for part in name.split("."))
        members = {**{f.name: f for f in info.functions if f.cpp_name is None},
                   **{st.name: st for st in info.structs if st.origin is None},
                   **{v.name: v for v in info.globals}, **info.generics}
        user = builtins.UserModule(name, members, namespace=namespace, info=info, path=str(path.resolve()))
        self.loaded[name] = user
        self.units.append(codegen.ModuleUnit(module, info, name, namespace, str(path), source))
        return user


def check_program(source: str, path: Path | None = None) -> list[codegen.ModuleUnit]:
    """Parse and check a program and everything it imports, including thread safety.
    Returns the modules in dependency order (the main program last). Raises CompileError."""
    program = Program(path.parent if path is not None else Path.cwd())
    module = parse(source)
    info = check(module, "__main__", program.load)
    units = [*program.units, codegen.ModuleUnit(module, info, path=str(path) if path is not None else "<stdin>",
                                                source=source)]
    try:
        threads.verify([(u.module, u.info, u.name) for u in units])
    except threads.ThreadSafetyError as e:
        if e.module != "__main__":
            e.file, e.source = program.paths[e.module], program.sources[e.module]
        raise
    return units


def translate(source: str, path: Path | None = None) -> Translation:
    """Compile a seadash program (and the modules it imports) to one C++ file.
    `path` is where the source lives; imports are found next to it. Raises CompileError."""
    units = check_program(source, path)
    libs = list(dict.fromkeys(lib for u in units for m in u.info.imports for lib in m.libs))
    return Translation(codegen.generate_program(units), libs)


def to_cpp(source: str) -> str:
    return translate(source).cpp


def find_cxx() -> str:
    if env := os.environ.get("SEADASH_CXX"):
        return env
    for name in CXX_CANDIDATES:
        if shutil.which(name):
            return name
    raise BuildError("no C++ compiler found; install g++-14 (on macOS: xcode-select --install) or set SEADASH_CXX")


@dataclass
class BuildOptions:
    optimize: bool = True
    cxx: str | None = None
    cache: bool = True  # reuse binaries built from the same C++, and a precompiled runtime header
    traceback: bool | None = None  # tracebacks for uncaught exceptions (default: in debug builds)


# ---- build cache ---------------------------------------------------------------------
#
# Compiling the C++ is nearly all of a build's time, so two things are cached:
#  - the runtime header (seadash.hpp) precompiled, per compiler and flags (GCC only);
#  - finished binaries, keyed by everything that goes into them: the C++ source, flags,
#    libraries, the compiler's version and the runtime headers. An unchanged program
#    runs again without compiling at all.
# Files appear atomically (written aside, then renamed), so parallel builds are safe.

MAX_CACHED_BINARIES = 256


def cache_dir() -> Path:
    if env := os.environ.get("SEADASH_CACHE_DIR"):
        return Path(env)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "seadash"


@functools.cache
def compiler_id(cxx: str) -> str:
    """The compiler's identity for cache keys: where it is and its version."""
    try:
        version = subprocess.run([cxx, "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    except (OSError, IndexError):
        version = "?"
    return f"{shutil.which(cxx) or cxx} {version}"


@functools.cache
def runtime_hash() -> str:
    h = hashlib.sha256()
    for path in sorted(RUNTIME_DIR.rglob("*.hpp")):
        h.update(str(path.relative_to(RUNTIME_DIR)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode())
        h.update(b"\0")
    return h.hexdigest()[:32]


def atomic_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{threading.get_ident()}")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def precompiled_header(cxx: str, flags: list[str]) -> Path | None:
    """A directory holding seadash.hpp.gch for these flags, built on first use; None if
    the compiler isn't GCC or it can't be built (then the header is simply parsed)."""
    if "clang" in compiler_id(cxx).lower():
        return None
    where = cache_dir() / "pch" / digest(compiler_id(cxx), *flags, runtime_hash())
    gch = where / "seadash.hpp.gch"
    if gch.exists():
        return where
    try:
        where.mkdir(parents=True, exist_ok=True)
        with open(where / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # one build; others wait for it
            if not gch.exists():
                tmp = where / f".seadash.hpp.gch.{os.getpid()}"
                cmd = [cxx, *flags, "-x", "c++-header", str(RUNTIME_DIR / "seadash.hpp"), "-o", str(tmp)]
                if subprocess.run(cmd, capture_output=True).returncode != 0:
                    tmp.unlink(missing_ok=True)
                    return None
                os.replace(tmp, gch)
    except OSError:
        return None
    return where


@functools.cache
def library_paths() -> tuple[list[str], list[str]]:
    """Where to find the libraries that modules link with, as (compile flags, link flags).
    Only macOS needs telling: Homebrew on Apple silicon isn't on the compiler's search path."""
    if sys.platform != "darwin":
        return [], []
    prefix = Path(os.environ.get("HOMEBREW_PREFIX") or "/opt/homebrew")
    if not (prefix / "include").is_dir():
        return [], []
    return [f"-I{prefix / 'include'}"], [f"-L{prefix / 'lib'}"]


def prune(directory: Path, keep: int) -> None:
    entries = sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    for old in entries[: max(0, len(entries) - keep)]:
        old.unlink(missing_ok=True)


def compile_cpp(cpp_path: Path, output: Path, options: BuildOptions, libs: list[str] = ()) -> None:
    cxx = options.cxx or find_cxx()
    flags = [
        "-std=c++23",
        "-fwrapv",  # int overflow wraps instead of being undefined behaviour
        "-ffp-contract=off",  # a * b + c rounds twice, as in Python (arm64 would fuse it)
        "-O3" if options.optimize else "-O0",  # (-O3 measured faster than -O2 on the benchmarks; bench/)
        *(["-DSD_TRACEBACK"] if (options.traceback if options.traceback is not None else not options.optimize) else []),
    ]
    link = [f"-l{lib}" for lib in libs]
    if libs:
        include_dirs, lib_dirs = library_paths()
        flags += include_dirs
        link = lib_dirs + link
    cached = None
    if options.cache:
        key = digest(cpp_path.read_text(), *flags, *link, compiler_id(cxx), runtime_hash())
        cached = cache_dir() / "bin" / key
        if cached.exists():
            try:
                os.utime(cached)  # recently used: kept when the cache is pruned
                atomic_copy(cached, output)
                return
            except OSError:
                pass
    pch = precompiled_header(cxx, flags) if options.cache else None
    cmd = [cxx, *flags, *([f"-I{pch}"] if pch else []), f"-I{RUNTIME_DIR}", str(cpp_path), "-o", str(output), *link]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise BuildError(
            "the C++ compiler rejected the generated code. This is a bug in seadash, "
            f"not in your program.\ncommand: {' '.join(cmd)}\n{result.stderr}"
        )
    if cached is not None:
        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            atomic_copy(output, cached)
            if len(os.listdir(cached.parent)) > MAX_CACHED_BINARIES + 32:
                prune(cached.parent, MAX_CACHED_BINARIES)
        except OSError:
            pass  # caching is only an optimization
