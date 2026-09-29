"""Compiler diagnostics.

Every error the compiler reports carries a source location so it can be
rendered with the offending line and a caret, e.g.

    hello.sd:3:9: error: unterminated string literal
        x = "hello
            ^
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Loc:
    line: int  # 1-based
    col: int   # 1-based

    def __str__(self) -> str:
        return f"{self.line}:{self.col}"


class CompileError(Exception):
    def __init__(self, message: str, loc: Loc):
        super().__init__(message)
        self.message = message
        self.loc = loc

    def render(self, source: str, filename: str = "<input>") -> str:
        header = f"{filename}:{self.loc}: error: {self.message}"
        lines = source.splitlines()
        if not 1 <= self.loc.line <= len(lines):
            return header
        text = lines[self.loc.line - 1]
        # Keep tabs in the caret line so it lines up under the source.
        pad = "".join(c if c == "\t" else " " for c in text[: self.loc.col - 1])
        return f"{header}\n    {text}\n    {pad}^"


class LexError(CompileError):
    pass


class ParseError(CompileError):
    pass
