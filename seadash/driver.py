"""The full pipeline: seadash source -> C++ -> native binary."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import codegen
from .checker import check
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


def translate(source: str) -> Translation:
    """Compile seadash source to C++ source. Raises CompileError on bad input."""
    module = parse(source)
    info = check(module)
    libs = list(dict.fromkeys(lib for m in info.imports for lib in m.libs))
    return Translation(codegen.generate(module, info), libs)


def to_cpp(source: str) -> str:
    return translate(source).cpp


def find_cxx() -> str:
    if env := os.environ.get("SEADASH_CXX"):
        return env
    for name in CXX_CANDIDATES:
        if shutil.which(name):
            return name
    raise BuildError("no C++ compiler found; install g++-14 or set SEADASH_CXX")


@dataclass
class BuildOptions:
    optimize: bool = True
    cxx: str | None = None


def compile_cpp(cpp_path: Path, output: Path, options: BuildOptions, libs: list[str] = ()) -> None:
    cxx = options.cxx or find_cxx()
    cmd = [
        cxx,
        "-std=c++23",
        "-fwrapv",  # int overflow wraps instead of being undefined behaviour
        "-O2" if options.optimize else "-O0",
        f"-I{RUNTIME_DIR}",
        str(cpp_path),
        "-o",
        str(output),
        *(f"-l{lib}" for lib in libs),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise BuildError(
            "the C++ compiler rejected the generated code. This is a bug in seadash, "
            f"not in your program.\ncommand: {' '.join(cmd)}\n{result.stderr}"
        )
