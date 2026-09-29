"""The `sd` command.

    sd tokens FILE     print the token stream (for debugging the lexer)
    sd ast FILE        print the syntax tree (for debugging the parser)
    sd check FILE      type-check a file and list each function's variables
"""

import argparse
import sys

from .astdump import dump
from .checker import check
from .errors import CompileError
from .lexer import TokenKind, tokenize
from .parser import parse


def cmd_tokens(path: str) -> int:
    source = read_source(path)
    try:
        tokens = tokenize(source)
    except CompileError as e:
        print(e.render(source, path), file=sys.stderr)
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
        print(e.render(source, path), file=sys.stderr)
        return 1
    print(dump(module))
    return 0


def cmd_check(path: str) -> int:
    source = read_source(path)
    try:
        info = check(parse(source))
    except CompileError as e:
        print(e.render(source, path), file=sys.stderr)
        return 1

    def show_vars(variables) -> None:
        for v in variables:
            renamed = f"  (source name '{v.name}')" if v.cpp_name != v.name else ""
            print(f"    {v.cpp_name}: {v.type}{renamed}")

    for st in info.structs:
        print(f"{st.kind} {st.name}")
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
    print(f"{path}: ok", file=sys.stderr)
    return 0


def read_source(path: str) -> str:
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
    args = parser.parse_args(argv)

    if args.command == "tokens":
        return cmd_tokens(args.file)
    if args.command == "ast":
        return cmd_ast(args.file)
    if args.command == "check":
        return cmd_check(args.file)
    return 2


if __name__ == "__main__":
    sys.exit(main())
