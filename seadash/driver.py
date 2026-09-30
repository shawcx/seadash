"""The full pipeline: seadash source -> C++ -> native binary."""

import os
import shutil
import subprocess
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
        self.units.append(codegen.ModuleUnit(module, info, name, namespace))
        return user


def check_program(source: str, path: Path | None = None) -> list[codegen.ModuleUnit]:
    """Parse and check a program and everything it imports, including thread safety.
    Returns the modules in dependency order (the main program last). Raises CompileError."""
    program = Program(path.parent if path is not None else Path.cwd())
    module = parse(source)
    info = check(module, "__main__", program.load)
    units = [*program.units, codegen.ModuleUnit(module, info)]
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
