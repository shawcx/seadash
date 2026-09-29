"""The `sd` command.

    sd tokens FILE     print the token stream (for debugging the lexer)
    sd ast FILE        print the syntax tree (for debugging the parser)
"""

import argparse
import sys

from .astdump import dump
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
    args = parser.parse_args(argv)

    if args.command == "tokens":
        return cmd_tokens(args.file)
    if args.command == "ast":
        return cmd_ast(args.file)
    return 2


if __name__ == "__main__":
    sys.exit(main())
