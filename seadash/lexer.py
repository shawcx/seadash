"""Lexer: source text -> tokens.

Most of this is ordinary tokenizing (names, numbers, strings, operators).
The Python-specific part is indentation. Like CPython's tokenizer, we turn
leading whitespace into explicit INDENT / DEDENT tokens, so the parser can
treat an indented block exactly like a `{ ... }` block in C:

    if x:            KEYWORD(if) NAME(x) OP(:) NEWLINE
        y = 1        INDENT NAME(y) OP(=) INT(1) NEWLINE
    z = 2            DEDENT NAME(z) OP(=) INT(2) NEWLINE

Rules (matching Python):
  * NEWLINE ends a logical line. Blank and comment-only lines produce nothing.
  * Inside (), [] or {} newlines and indentation are ignored.
  * A backslash at the end of a line joins it with the next line.
  * Indentation is compared as exact whitespace strings: a deeper block must
    start with its parent's indentation, so mixing tabs and spaces
    inconsistently is an error rather than a guess.
"""

from dataclasses import dataclass
from enum import Enum

from .errors import LexError, Loc


class TokenKind(Enum):
    NAME = "NAME"
    KEYWORD = "KEYWORD"
    INT = "INT"
    FLOAT = "FLOAT"
    STRING = "STRING"
    FSTRING = "FSTRING"
    OP = "OP"
    NEWLINE = "NEWLINE"
    INDENT = "INDENT"
    DEDENT = "DEDENT"
    EOF = "EOF"


@dataclass(frozen=True)
class FStringExpr:
    """A `{expr:spec}` hole in an f-string. The parser re-lexes `source`."""

    source: str
    loc: Loc  # where `source` starts, so errors inside it point at the right place
    spec: str | None = None


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    # NAME/KEYWORD/OP: the text. INT: int. FLOAT: float. STRING: the decoded str.
    # FSTRING: tuple of str (literal text) and FStringExpr parts.
    value: object
    loc: Loc

    def __repr__(self) -> str:
        if self.kind in (TokenKind.NEWLINE, TokenKind.INDENT, TokenKind.DEDENT, TokenKind.EOF):
            return f"{self.kind.value}@{self.loc}"
        return f"{self.kind.value}({self.value!r})@{self.loc}"


KEYWORDS = frozenset(
    """
    False None True and as assert break class continue def del elif else
    except finally for from global if import in is lambda nonlocal not or
    pass raise return struct try while with yield
    """.split()
)

# Longest first, so `**=` wins over `**` wins over `*`.
OPERATORS = sorted(
    """
    **= //= >>= <<= ...
    -> := == != <= >= ** // << >> += -= *= /= %= &= |= ^= @=
    + - * / % @ & | ^ ~ < > ( ) [ ] { } , : . ; = ?
    """.split(),
    key=len,
    reverse=True,
)

OPEN_BRACKETS = {"(": ")", "[": "]", "{": "}"}
CLOSE_BRACKETS = {v: k for k, v in OPEN_BRACKETS.items()}

# Things C/C++ programmers will reach for, with a friendlier error than "unexpected character".
HINTS = {
    "&&": "use 'and' instead of '&&'",
    "||": "use 'or' instead of '||'",
    "!": "use 'not' instead of '!'",
}

SIMPLE_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", "'": "'", '"': '"',
    "a": "\a", "b": "\b", "f": "\f", "v": "\v",
}

STRING_PREFIXES = {"f": (True, False), "r": (False, True), "fr": (True, True), "rf": (True, True)}


def tokenize(source: str, start: Loc = Loc(1, 1)) -> list[Token]:
    """`start` is where `source` begins in its file (used for f-string expressions)."""
    return Lexer(source, start).tokenize()


class Lexer:
    def __init__(self, source: str, start: Loc = Loc(1, 1)):
        self.src = source.replace("\r\n", "\n").replace("\r", "\n")
        self.pos = 0
        self.line = start.line
        self.col = start.col
        self.tokens: list[Token] = []
        self.indents = [""]  # stack of indentation strings; bottom is column 0
        self.brackets: list[tuple[str, Loc]] = []
        self.at_line_start = True

    # ---- character helpers -------------------------------------------------

    def peek(self, offset: int = 0) -> str:
        i = self.pos + offset
        return self.src[i] if i < len(self.src) else ""

    def advance(self) -> str:
        c = self.src[self.pos]
        self.pos += 1
        if c == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return c

    def loc(self) -> Loc:
        return Loc(self.line, self.col)

    def emit(self, kind: TokenKind, value: object, loc: Loc) -> None:
        self.tokens.append(Token(kind, value, loc))

    # ---- main loop ---------------------------------------------------------

    def tokenize(self) -> list[Token]:
        while True:
            if self.at_line_start and not self.brackets:
                self.at_line_start = False
                self.read_indentation()
            c = self.peek()
            if not c:
                break
            if c in " \t\f":
                self.advance()
            elif c == "#":
                while self.peek() not in ("\n", ""):
                    self.advance()
            elif c == "\\" and self.peek(1) == "\n":
                self.advance()
                self.advance()
            elif c == "\n":
                loc = self.loc()
                self.advance()
                if not self.brackets:
                    self.emit(TokenKind.NEWLINE, "\n", loc)
                    self.at_line_start = True
            elif c.isdigit() or (c == "." and self.peek(1).isdigit()):
                self.read_number()
            elif c.isalpha() or c == "_":
                self.read_name_or_string()
            elif c in "\"'":
                self.read_string(fmt=False, raw=False, start=self.loc())
            else:
                self.read_operator()
        return self.finish()

    def finish(self) -> list[Token]:
        if self.brackets:
            open_char, loc = self.brackets[-1]
            raise LexError(f"'{open_char}' was never closed", loc)
        loc = self.loc()
        if self.tokens and self.tokens[-1].kind not in (TokenKind.NEWLINE, TokenKind.DEDENT):
            self.emit(TokenKind.NEWLINE, "\n", loc)
        while len(self.indents) > 1:
            self.indents.pop()
            self.emit(TokenKind.DEDENT, "", loc)
        self.emit(TokenKind.EOF, "", loc)
        return self.tokens

    # ---- indentation -------------------------------------------------------

    def read_indentation(self) -> None:
        """At the start of a line: skip blank/comment lines, then emit INDENT/DEDENTs."""
        while True:
            start = self.pos
            while self.peek() in (" ", "\t", "\f") and self.peek():
                self.advance()
            ws = self.src[start : self.pos].replace("\f", "")
            c = self.peek()
            if c == "#":
                while self.peek() not in ("\n", ""):
                    self.advance()
                c = self.peek()
            if c == "\n":
                self.advance()
                continue  # blank line: indentation doesn't count
            if c == "":
                return  # trailing blank lines at EOF
            break

        loc = Loc(self.line, 1)
        top = self.indents[-1]
        if ws == top:
            return
        if ws.startswith(top):
            self.indents.append(ws)
            self.emit(TokenKind.INDENT, ws, loc)
            return
        while len(self.indents) > 1 and self.indents[-1] != ws and self.indents[-1].startswith(ws):
            self.indents.pop()
            self.emit(TokenKind.DEDENT, "", loc)
        if self.indents[-1] != ws:
            if "\t" in ws or "\t" in top:
                raise LexError("inconsistent use of tabs and spaces in indentation", loc)
            raise LexError("unindent does not match any outer indentation level", loc)

    # ---- names, keywords, numbers ------------------------------------------

    def read_name_or_string(self) -> None:
        loc = self.loc()
        start = self.pos
        while self.peek().isalnum() or self.peek() == "_":
            self.advance()
        text = self.src[start : self.pos]
        prefix = text.lower()
        if prefix in STRING_PREFIXES and self.peek() in "\"'" and self.peek():
            fmt, raw = STRING_PREFIXES[prefix]
            self.read_string(fmt=fmt, raw=raw, start=loc)
        elif text in KEYWORDS:
            self.emit(TokenKind.KEYWORD, text, loc)
        else:
            self.emit(TokenKind.NAME, text, loc)

    def read_number(self) -> None:
        loc = self.loc()
        start = self.pos
        is_float = False

        if self.peek() == "0" and self.peek(1) in ("x", "X", "o", "O", "b", "B") and self.peek(1):
            self.advance()
            self.advance()
            while self.peek().isalnum() or self.peek() == "_":
                self.advance()
        else:
            self.read_digits()
            if self.peek() == ".":
                is_float = True
                self.advance()
                self.read_digits()
            if self.peek() in ("e", "E") and self.peek():
                is_float = True
                self.advance()
                if self.peek() in ("+", "-") and self.peek():
                    self.advance()
                self.read_digits()
            if self.peek().isalpha() or self.peek() == "_":
                while self.peek().isalnum() or self.peek() == "_":
                    self.advance()

        text = self.src[start : self.pos]
        try:
            if is_float:
                self.emit(TokenKind.FLOAT, float(text), loc)
            else:
                self.emit(TokenKind.INT, int(text, 0), loc)
        except ValueError:
            raise LexError(f"invalid number literal '{text}'", loc) from None

    def read_digits(self) -> None:
        while self.peek().isdigit() or self.peek() == "_":
            self.advance()

    # ---- operators ---------------------------------------------------------

    def read_operator(self) -> None:
        loc = self.loc()
        for hint_text, message in HINTS.items():
            if self.src.startswith(hint_text, self.pos) and not self.src.startswith("!=", self.pos):
                raise LexError(message, loc)
        for op in OPERATORS:
            if self.src.startswith(op, self.pos):
                for _ in op:
                    self.advance()
                self.track_bracket(op, loc)
                self.emit(TokenKind.OP, op, loc)
                return
        raise LexError(f"unexpected character {self.peek()!r}", loc)

    def track_bracket(self, op: str, loc: Loc) -> None:
        if op in OPEN_BRACKETS:
            self.brackets.append((op, loc))
        elif op in CLOSE_BRACKETS:
            if not self.brackets:
                raise LexError(f"unmatched '{op}'", loc)
            open_char, _ = self.brackets.pop()
            if OPEN_BRACKETS[open_char] != op:
                raise LexError(f"closing '{op}' does not match '{open_char}'", loc)

    # ---- strings -----------------------------------------------------------

    def read_string(self, fmt: bool, raw: bool, start: Loc) -> None:
        quote = self.advance()
        triple = self.peek() == quote and self.peek(1) == quote
        if triple:
            self.advance()
            self.advance()
        closer = quote * 3 if triple else quote

        parts: list[str | FStringExpr] = []
        text: list[str] = []

        while True:
            c = self.peek()
            if c == "" or (c == "\n" and not triple):
                raise LexError("unterminated string literal", start)
            if self.src.startswith(closer, self.pos):
                for _ in closer:
                    self.advance()
                break
            if c == "\\":
                text.append(self.read_escape(raw))
            elif fmt and c == "{":
                self.advance()
                if self.peek() == "{":
                    self.advance()
                    text.append("{")
                else:
                    if text:
                        parts.append("".join(text))
                        text = []
                    parts.append(self.read_fstring_expr(quote, triple, start))
            elif fmt and c == "}":
                loc = self.loc()
                self.advance()
                if self.peek() != "}":
                    raise LexError("single '}' is not allowed in an f-string (use '}}')", loc)
                self.advance()
                text.append("}")
            else:
                text.append(self.advance())

        if fmt:
            if text:
                parts.append("".join(text))
            self.emit(TokenKind.FSTRING, tuple(parts), start)
        else:
            self.emit(TokenKind.STRING, "".join(text), start)

    def read_escape(self, raw: bool) -> str:
        loc = self.loc()
        self.advance()  # the backslash
        c = self.peek()
        if c == "":
            raise LexError("unterminated string literal", loc)
        if raw:
            # Like Python: in raw strings a backslash still stops the next char
            # from closing the string, but both characters are kept.
            return "\\" + self.advance()
        self.advance()
        if c == "\n":
            return ""  # backslash-newline continues the string
        if c in SIMPLE_ESCAPES:
            return SIMPLE_ESCAPES[c]
        width = {"x": 2, "u": 4, "U": 8}.get(c)
        if width:
            digits = self.src[self.pos : self.pos + width]
            if len(digits) != width or any(d not in "0123456789abcdefABCDEF" for d in digits):
                raise LexError(f"'\\{c}' escape needs {width} hex digits", loc)
            for _ in range(width):
                self.advance()
            code = int(digits, 16)
            if code > 0x10FFFF:
                raise LexError(f"'\\{c}{digits}' is not a valid Unicode code point", loc)
            return chr(code)
        raise LexError(f"unknown escape sequence '\\{c}'", loc)

    def read_fstring_expr(self, quote: str, triple: bool, string_start: Loc) -> FStringExpr:
        """Read `expr` or `expr:spec` after an f-string's `{`, through the closing `}`."""
        loc = self.loc()
        start = self.pos
        depth = 0
        spec: str | None = None
        expr_end = None

        while True:
            c = self.peek()
            if c == "" or (c == "\n" and not triple):
                raise LexError("unterminated string literal", string_start)
            if c == quote and (not triple or self.src.startswith(quote * 3, self.pos)):
                raise LexError("f-string: expected '}'", self.loc())
            if c in "([{":
                depth += 1
            elif c in ")]" or (c == "}" and depth > 0):
                depth -= 1
            elif c == "}":
                expr_end = expr_end if expr_end is not None else self.pos
                break
            elif c == ":" and depth == 0 and expr_end is None:
                expr_end = self.pos
                self.advance()
                spec_start = self.pos
                while self.peek() not in ("}", "", "\n"):
                    self.advance()
                spec = self.src[spec_start : self.pos]
                continue
            elif c in "\"'" and c != quote:
                self.skip_nested_string(c, string_start)
                continue
            self.advance()

        self.advance()  # the closing }
        source = self.src[start:expr_end]
        if not source.strip():
            raise LexError("f-string: empty expression not allowed", loc)
        return FStringExpr(source, loc, spec)

    def skip_nested_string(self, quote: str, string_start: Loc) -> None:
        """Skip a string literal inside an f-string expression, e.g. f"{d['key']}"."""
        self.advance()
        while self.peek() != quote:
            if self.peek() in ("", "\n"):
                raise LexError("unterminated string literal", string_start)
            if self.peek() == "\\":
                self.advance()
            self.advance()
        self.advance()
