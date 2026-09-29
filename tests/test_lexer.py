import textwrap

import pytest

from seadash.errors import LexError, Loc
from seadash.lexer import FStringExpr, TokenKind as K, tokenize


def toks(source: str) -> list[tuple]:
    """Tokens as compact tuples: (KIND, value) or just KIND for layout tokens."""
    out = []
    for t in tokenize(textwrap.dedent(source)):
        if t.kind in (K.NEWLINE, K.INDENT, K.DEDENT, K.EOF):
            out.append(t.kind.value)
        else:
            out.append((t.kind.value, t.value))
    return out


def lex_error(source: str) -> LexError:
    with pytest.raises(LexError) as info:
        tokenize(textwrap.dedent(source))
    return info.value


# ---- basics -----------------------------------------------------------------


def test_empty_source():
    assert toks("") == ["EOF"]
    assert toks("\n\n  \n# just a comment\n") == ["EOF"]


def test_simple_assignment():
    assert toks("a = 2\n") == [("NAME", "a"), ("OP", "="), ("INT", 2), "NEWLINE", "EOF"]


def test_missing_final_newline_is_added():
    assert toks("a = 2") == [("NAME", "a"), ("OP", "="), ("INT", 2), "NEWLINE", "EOF"]


def test_keywords_vs_names():
    assert toks("def struct print None") == [
        ("KEYWORD", "def"), ("KEYWORD", "struct"), ("NAME", "print"), ("KEYWORD", "None"),
        "NEWLINE", "EOF",
    ]


def test_locations():
    tokens = tokenize("x = 1\n  \nfoo(y)\n")
    assert [(t.kind, t.loc) for t in tokens] == [
        (K.NAME, Loc(1, 1)), (K.OP, Loc(1, 3)), (K.INT, Loc(1, 5)), (K.NEWLINE, Loc(1, 6)),
        (K.NAME, Loc(3, 1)), (K.OP, Loc(3, 4)), (K.NAME, Loc(3, 5)), (K.OP, Loc(3, 6)),
        (K.NEWLINE, Loc(3, 7)), (K.EOF, Loc(4, 1)),
    ]


def test_comments_are_skipped():
    assert toks("x = 1  # set x\n") == [("NAME", "x"), ("OP", "="), ("INT", 1), "NEWLINE", "EOF"]


# ---- operators --------------------------------------------------------------


def test_longest_operator_wins():
    assert [v for _, v in toks("** **= // //= -> := == != <= >= < >")[:-2]] == [
        "**", "**=", "//", "//=", "->", ":=", "==", "!=", "<=", ">=", "<", ">",
    ]


def test_optional_type_marker():
    assert toks("x: int? = None")[:4] == [("NAME", "x"), ("OP", ":"), ("NAME", "int"), ("OP", "?")]


@pytest.mark.parametrize("src,msg", [
    ("a && b", "use 'and' instead of '&&'"),
    ("a || b", "use 'or' instead of '||'"),
    ("!a", "use 'not' instead of '!'"),
    ("a $ b", "unexpected character '$'"),
])
def test_c_style_operators_get_hints(src, msg):
    assert lex_error(src).message == msg


# ---- numbers ----------------------------------------------------------------


@pytest.mark.parametrize("src,kind,value", [
    ("0", "INT", 0),
    ("42", "INT", 42),
    ("1_000_000", "INT", 1_000_000),
    ("0xff", "INT", 255),
    ("0o17", "INT", 15),
    ("0b1010", "INT", 10),
    ("3.14", "FLOAT", 3.14),
    ("1.", "FLOAT", 1.0),
    (".5", "FLOAT", 0.5),
    ("1e3", "FLOAT", 1000.0),
    ("2.5E-2", "FLOAT", 0.025),
])
def test_numbers(src, kind, value):
    assert toks(src)[0] == (kind, value)


@pytest.mark.parametrize("src", ["12abc", "0xZZ", "007", "1e", "1__0x"])
def test_bad_numbers(src):
    assert lex_error(src).message.startswith("invalid number literal")


# ---- strings ----------------------------------------------------------------


def test_strings_and_escapes():
    assert toks(r"'a\tb' " + r'"q\"q" ' + r"'\x41\u00e9'")[:3] == [
        ("STRING", "a\tb"), ("STRING", 'q"q'), ("STRING", "Aé"),
    ]


def test_raw_string_keeps_backslashes():
    assert toks(r'r"\d+\.\"x"')[0] == ("STRING", r"\d+\.\"x")


def test_triple_quoted_string_spans_lines():
    assert toks('s = """one\ntwo"""\n')[2] == ("STRING", "one\ntwo")


def test_unterminated_string_points_at_start():
    err = lex_error('x = "hello\ny = 1\n')
    assert err.message == "unterminated string literal"
    assert err.loc == Loc(1, 5)


def test_unknown_escape():
    assert lex_error(r'"\q"').message == "unknown escape sequence '\\q'"


def test_prefix_letters_alone_are_names():
    assert toks("f r fr")[:3] == [("NAME", "f"), ("NAME", "r"), ("NAME", "fr")]


# ---- f-strings --------------------------------------------------------------


def test_fstring_parts():
    [(kind, parts)] = toks('f"hi {name}, {x + 1:>5}!"')[:1]
    assert kind == "FSTRING"
    assert parts == (
        "hi ",
        FStringExpr("name", Loc(1, 7)),
        ", ",
        FStringExpr("x + 1", Loc(1, 15), ">5"),
        "!",
    )


def test_fstring_escaped_braces_and_nesting():
    [(_, parts)] = toks("""f"{{literal}} {d['k']} {f(a, [1, 2])}" """)[:1]
    assert parts == (
        "{literal} ",
        FStringExpr("d['k']", Loc(1, 16)),
        " ",
        FStringExpr("f(a, [1, 2])", Loc(1, 25)),
    )


@pytest.mark.parametrize("src,msg", [
    ('f"{}"', "f-string: empty expression not allowed"),
    ('f"a } b"', "single '}' is not allowed in an f-string (use '}}')"),
    ('f"{x"', "f-string: expected '}'"),
])
def test_fstring_errors(src, msg):
    assert lex_error(src).message == msg


# ---- indentation ------------------------------------------------------------


def test_indent_and_dedent():
    src = """
        if x:
            y = 1
        z = 2
    """
    assert toks(src) == [
        ("KEYWORD", "if"), ("NAME", "x"), ("OP", ":"), "NEWLINE",
        "INDENT", ("NAME", "y"), ("OP", "="), ("INT", 1), "NEWLINE",
        "DEDENT", ("NAME", "z"), ("OP", "="), ("INT", 2), "NEWLINE",
        "EOF",
    ]


def test_multiple_dedents_at_once_and_at_eof():
    src = """
        def f():
            while a:
                if b:
                    pass
        x
        def g():
            if c:
                pass
    """
    layout = [t for t in toks(src) if isinstance(t, str)]
    assert layout == [
        "NEWLINE", "INDENT", "NEWLINE", "INDENT", "NEWLINE", "INDENT", "NEWLINE",
        "DEDENT", "DEDENT", "DEDENT", "NEWLINE",
        "NEWLINE", "INDENT", "NEWLINE", "INDENT", "NEWLINE",
        "DEDENT", "DEDENT", "EOF",
    ]


def test_blank_and_comment_lines_do_not_affect_indentation():
    src = """
        if x:

            # a comment at a different indent
          # and another
            y = 1

        z = 2
    """
    assert "INDENT" in toks(src)
    assert toks(src).count("DEDENT") == 1


def test_newlines_inside_brackets_are_ignored():
    src = """
        xs = [
            1,
          2,
        ]
        y = (a +
             b)
    """
    assert toks(src) == [
        ("NAME", "xs"), ("OP", "="), ("OP", "["), ("INT", 1), ("OP", ","), ("INT", 2),
        ("OP", ","), ("OP", "]"), "NEWLINE",
        ("NAME", "y"), ("OP", "="), ("OP", "("), ("NAME", "a"), ("OP", "+"), ("NAME", "b"),
        ("OP", ")"), "NEWLINE",
        "EOF",
    ]


def test_backslash_continuation():
    assert toks("x = 1 + \\\n        2\n") == [
        ("NAME", "x"), ("OP", "="), ("INT", 1), ("OP", "+"), ("INT", 2), "NEWLINE", "EOF",
    ]


def test_bad_dedent():
    err = lex_error("if x:\n    y\n  z\n")
    assert err.message == "unindent does not match any outer indentation level"
    assert err.loc == Loc(3, 1)


def test_inconsistent_tabs_and_spaces():
    err = lex_error("if x:\n\ty\n    z\n")
    assert err.message == "inconsistent use of tabs and spaces in indentation"


def test_tabs_are_fine_when_consistent():
    assert toks("if x:\n\ty\n\tz\nw\n").count("INDENT") == 1


# ---- brackets ---------------------------------------------------------------


@pytest.mark.parametrize("src,msg,loc", [
    ("f(x", "'(' was never closed", Loc(1, 2)),
    ("x)", "unmatched ')'", Loc(1, 2)),
    ("[1, 2)", "closing ')' does not match '['", Loc(1, 6)),
])
def test_bracket_errors(src, msg, loc):
    err = lex_error(src)
    assert (err.message, err.loc) == (msg, loc)


def test_error_rendering():
    err = lex_error('x = 1\ny = "oops\n')
    assert err.render('x = 1\ny = "oops\n', "demo.sd") == (
        'demo.sd:2:5: error: unterminated string literal\n'
        '    y = "oops\n'
        '        ^'
    )
