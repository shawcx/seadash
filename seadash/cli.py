"""The `sd` command.

    sd tokens FILE     print the token stream (for debugging the lexer)
    sd ast FILE        print the syntax tree (for debugging the parser)
    sd check FILE      type-check a file and list each function's variables
    sd emit FILE       print the generated C++
    sd build FILE      compile to a native binary (named after FILE, or -o NAME)
    sd run FILE ARGS   build to a temporary binary and run it
    sd clean           empty the build cache (built binaries, precompiled headers)

FILE may be `-` to read the program from stdin; `sd run` with no FILE does too.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .astdump import dump
from .checker import check
from .driver import BuildError, BuildOptions, Translation, cache_dir, check_program, compile_cpp, translate
from .errors import CompileError
from .lexer import TokenKind, tokenize
from .parser import parse


def cmd_tokens(path: str) -> int:
    source = read_source(path)
    try:
        tokens = tokenize(source)
    except CompileError as e:
        print(e.render(source, display_name(path)), file=sys.stderr)
        return 1
    for tok in tokens:
        if tok.kind in (TokenKind.NEWLINE, TokenKind.INDENT, TokenKind.DEDENT, TokenKind.EOF):
            print(f"{str(tok.loc):>7}  {tok.kind.value}")
        else:
            print(f"{str(tok.loc):>7}  {tok.kind.value:<8} {tok.value!r}")
    return 0


def cmd_ast(path: str) -> int:
    source = read_source(path)
    try:
        module = parse(source)
    except CompileError as e:
        print(e.render(source, display_name(path)), file=sys.stderr)
        return 1
    print(dump(module))
    return 0


def cmd_check(path: str) -> int:
    source = read_source(path)
    try:
        info = check_program(source, source_path(path))[-1].info
    except CompileError as e:
        print(e.render(source, display_name(path)), file=sys.stderr)
        return 1

    def show_vars(variables) -> None:
        for v in variables:
            renamed = f"  (source name '{v.name}')" if v.cpp_name != v.name else ""
            print(f"    {v.cpp_name}: {v.type}{renamed}")

    for st in info.structs:
        print(f"{'enum' if st.enum is not None else st.kind} {st.name}")
        for f in st.fields.values():
            print(f"    {f.name}: {f.type}")
    if info.globals:
        print("globals")
        show_vars(info.globals)
    for fn in [*info.functions, *(m for st in info.structs for m in st.methods.values())]:
        owner = f"{fn.owner.name}." if fn.owner else ""
        print(str(fn).replace("def ", f"def {owner}", 1))
        show_vars(fn.locals)
    if info.main_locals:
        print("module code")
        show_vars(info.main_locals)
    print(f"{display_name(path)}: ok", file=sys.stderr)
    return 0


def translate_file(path: str) -> Translation | None:
    """seadash source -> C++, printing any compile error. None on failure."""
    source = read_source(path)
    try:
        return translate(source, source_path(path))
    except CompileError as e:
        print(e.render(source, display_name(path)), file=sys.stderr)
        return None


def cmd_emit(path: str) -> int:
    result = translate_file(path)
    if result is None:
        return 1
    print(result.cpp, end="")
    return 0


def build(path: str, output: Path, options: BuildOptions) -> bool:
    result = translate_file(path)
    if result is None:
        return False
    with tempfile.TemporaryDirectory(prefix="seadash-") as tmp:
        cpp_path = Path(tmp) / (Path(path).stem + ".cpp")
        cpp_path.write_text(result.cpp)
        try:
            compile_cpp(cpp_path, output, options, result.libs)
        except BuildError as e:
            print(f"sd: {e}", file=sys.stderr)
            return False
    return True


def cmd_build(path: str, output: str | None, options: BuildOptions) -> int:
    out = Path(output) if output else Path("stdin" if path == STDIN else Path(path).stem)
    return 0 if build(path, out, options) else 1


def cmd_run(path: str, args: list[str], options: BuildOptions) -> int:
    with tempfile.TemporaryDirectory(prefix="seadash-") as tmp:
        binary = Path(tmp) / ("stdin" if path == STDIN else Path(path).stem)
        if not build(path, binary, options):
            return 1
        return subprocess.run([str(binary), *args]).returncode


STDIN = "-"


def source_path(path: str) -> Path | None:
    """Where the program lives (imports are found next to it); stdin programs import from the cwd."""
    return None if path == STDIN else Path(path)


def display_name(path: str) -> str:
    return "<stdin>" if path == STDIN else path


def read_source(path: str) -> str:
    if path == STDIN:
        return sys.stdin.read()
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        sys.exit(f"sd: cannot read {path}: {e.strerror}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sd", description="The seadash compiler")
    sub = parser.add_subparsers(dest="command", required=True)
    p_tokens = sub.add_parser("tokens", help="print the token stream of a file")
    p_tokens.add_argument("file")
    p_ast = sub.add_parser("ast", help="print the syntax tree of a file")
    p_ast.add_argument("file")
    p_check = sub.add_parser("check", help="type-check a file")
    p_check.add_argument("file")
    p_emit = sub.add_parser("emit", help="print the generated C++")
    p_emit.add_argument("file")
    p_build = sub.add_parser("build", help="compile to a native binary")
    p_build.add_argument("file")
    p_build.add_argument("-o", "--output", help="binary name (default: the file's name without .sd)")
    p_run = sub.add_parser("run", help="build and run")
    p_run.add_argument("file", nargs="?", default=STDIN, help="the program (default: read it from stdin)")
    p_run.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the program")
    for p in (p_build, p_run):
        p.add_argument("--debug", action="store_true", help="compile without optimization (faster build)")
        p.add_argument("--no-cache", action="store_true", help="always compile (don't reuse cached builds)")
    sub.add_parser("clean", help="empty the build cache")
    args = parser.parse_args(argv)

    if args.command == "tokens":
        return cmd_tokens(args.file)
    if args.command == "ast":
        return cmd_ast(args.file)
    if args.command == "check":
        return cmd_check(args.file)
    if args.command == "emit":
        return cmd_emit(args.file)
    if args.command == "clean":
        shutil.rmtree(cache_dir(), ignore_errors=True)
        print(f"removed {cache_dir()}")
        return 0
    options = BuildOptions(optimize=not getattr(args, "debug", False), cxx=os.environ.get("SEADASH_CXX"),
                           cache=not getattr(args, "no_cache", False))
    if args.command == "build":
        return cmd_build(args.file, args.output, options)
    if args.command == "run":
        return cmd_run(args.file, args.args, options)
    return 2


if __name__ == "__main__":
    sys.exit(main())
