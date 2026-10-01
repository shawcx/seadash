import textwrap

import pytest

from seadash import ast as A
from seadash.astdump import dump, expr
from seadash.errors import Loc, ParseError
from seadash.parser import parse


def e(source: str) -> str:
    """Parse a single expression statement and render it as an S-expression."""
    [stmt] = parse(source).body
    assert isinstance(stmt, A.ExprStmt)
    return expr(stmt.value)


def s(source: str) -> str:
    """Parse statements and render them with `dump`."""
    return dump(parse(textwrap.dedent(source)))


def parse_error(source: str) -> ParseError:
    with pytest.raises(ParseError) as info:
        parse(textwrap.dedent(source))
    return info.value


# ---- precedence and associativity ------------------------------------------


@pytest.mark.parametrize("src,tree", [
    ("1 + 2 * 3", "(+ 1 (* 2 3))"),
    ("(1 + 2) * 3", "(* (+ 1 2) 3)"),
    ("a - b - c", "(- (- a b) c)"),
    ("a / b // c % d", "(% (// (/ a b) c) d)"),
    ("2 ** 3 ** 2", "(** 2 (** 3 2))"),
    ("-2 ** 2", "(- (** 2 2))"),
    ("2 ** -1", "(** 2 (- 1))"),
    ("-x.y", "(- (. x y))"),
    ("a | b ^ c & d", "(| a (^ b (& c d)))"),
    ("a << 1 + 2", "(<< a (+ 1 2))"),
    ("~a + b", "(+ (~ a) b)"),
    ("not a == b", "(not (== a b))"),
    ("a or b and c", "(or a (and b c))"),
    ("not a and not b", "(and (not a) (not b))"),
    ("a + b < c * d", "(< (+ a b) (* c d))"),
    ("x if c else y", "(if-exp c x y)"),
    ("a if b else c if d else e", "(if-exp b a (if-exp d c e))"),
    ("a or b if c else d", "(if-exp c (or a b) d)"),
])
def test_precedence(src, tree):
    assert e(src) == tree


@pytest.mark.parametrize("src,tree", [
    ("a < b", "(< a b)"),
    ("a < b <= c", "(chain a < b <= c)"),
    ("x in xs", "(in x xs)"),
    ("x not in xs", "(not in x xs)"),
    ("x is None", "(is x None)"),
    ("x is not None", "(is not x None)"),
    ("0 <= i < n != m", "(chain 0 <= i < n != m)"),
])
def test_comparisons(src, tree):
    assert e(src) == tree


# ---- atoms ------------------------------------------------------------------


@pytest.mark.parametrize("src,tree", [
    ("42", "42"),
    ("3.5", "3.5"),
    ("True", "True"),
    ("None", "None"),
    ("'hi'", "'hi'"),
    ("'a' \"b\" 'c'", "'abc'"),
    ("[]", "(list)"),
    ("[1, 2, 3,]", "(list 1 2 3)"),
    ("()", "(tuple)"),
    ("(1,)", "(tuple 1)"),
    ("(1, 2)", "(tuple 1 2)"),
    ("(1)", "1"),
    ("{}", "(dict)"),
    ("{'a': 1, 'b': 2}", "(dict 'a': 1 'b': 2)"),
    ("{1, 2}", "(set 1 2)"),
])
def test_literals(src, tree):
    assert e(src) == tree


def test_fstrings():
    assert e('f"hi {name}!"') == "(fstr 'hi ' {name} '!')"
    assert e('f"{x + 1:>5}"') == "(fstr {(+ x 1):>5})"
    assert e('f"{a}" "b" f"{c}"') == "(fstr {a} 'b' {c})"
    assert e('f"no holes"') == "'no holes'"
    assert e("f\"{d['k']}\"") == "(fstr {(index d 'k')})"
    assert e('f"{x:{w}.{p + 1}f}"') == "(fstr {x:{w}.{(+ p 1)}f})"
    assert e('f"{x:>{w!r}}!"') == "(fstr {x:>{w!r}} '!')"


def test_fstring_expression_locations_point_into_the_string():
    err = parse_error('x = f"value: {a +}"\n')
    assert err.loc == Loc(1, 18)
    assert err.message == "expected an expression, found ')'"


# ---- postfix: calls, indexing, slicing, attributes -------------------------


@pytest.mark.parametrize("src,tree", [
    ("f()", "(call f)"),
    ("f(a, b)", "(call f a b)"),
    ("f(a, k=1, j=2)", "(call f a k=1 j=2)"),
    ("obj.method(x).attr", "(. (call (. obj method) x) attr)"),
    ("xs[0]", "(index xs 0)"),
    ("xs[-1]", "(index xs (- 1))"),
    ("m[i, j]", "(index m (tuple i j))"),
    ("f(x)[0](y)", "(call (index (call f x) 0) y)"),
    ("sum(x * x for x in xs)", "(call sum (genexp (* x x) (for x xs)))"),
])
def test_postfix(src, tree):
    assert e(src) == tree


@pytest.mark.parametrize("src,tree", [
    ("xs[1:]", "(index xs (slice 1 _ _))"),
    ("xs[:-1]", "(index xs (slice _ (- 1) _))"),
    ("xs[:]", "(index xs (slice _ _ _))"),
    ("xs[::2]", "(index xs (slice _ _ 2))"),
    ("xs[1:10:2]", "(index xs (slice 1 10 2))"),
    ("xs[::-1]", "(index xs (slice _ _ (- 1)))"),
    ("m[1:, 0]", "(index m (tuple (slice 1 _ _) 0))"),
])
def test_slices(src, tree):
    assert e(src) == tree


# ---- comprehensions and walrus ---------------------------------------------


@pytest.mark.parametrize("src,tree", [
    ("[x * 2 for x in xs]", "(listcomp (* x 2) (for x xs))"),
    ("[x for x in xs if x > 0 if x < 9]", "(listcomp x (for x xs (if (> x 0)) (if (< x 9))))"),
    ("[(i, x) for i, x in enumerate(xs)]", "(listcomp (tuple i x) (for (tuple i x) (call enumerate xs)))"),
    ("[a for row in m for a in row]", "(listcomp a (for row m) (for a row))"),
    ("{k: v for k, v in pairs}", "(dictcomp k: v (for (tuple k v) pairs))"),
    ("{x for x in xs}", "(setcomp x (for x xs))"),
    ("(x for x in xs)", "(genexp x (for x xs))"),
    ("[y for x in xs if (y := f(x))]", "(listcomp y (for x xs (if (:= y (call f x)))))"),
])
def test_comprehensions(src, tree):
    assert e(src) == tree


# ---- simple statements -----------------------------------------------------


def test_assignments():
    assert s("""
        a = 2
        a = str(a)
        x = y = 0
        a, b = b, a
        p.x = 1
        xs[0] += 1
        n: int = 5
        field: list[str]
    """) == textwrap.dedent("""\
        (= a 2)
        (= a (call str a))
        (= x y 0)
        (= (tuple a b) (tuple b a))
        (= (. p x) 1)
        (+= (index xs 0) 1)
        (: n int 5)
        (: field list[str])""")


def test_simple_statements_on_one_line():
    assert s("a = 1; b = 2;\n") == "(= a 1)\n(= b 2)"


def test_return_forms():
    assert s("""
        def f():
            return
        def g():
            return 1, 2
    """) == textwrap.dedent("""\
        def f():
          (return)
        def g():
          (return (tuple 1 2))""")


def test_imports():
    assert s("""
        import base64
        import os.path as p, sys
        from math import sqrt, pi as PI
        from collections import (
            deque,
            Counter,
        )
    """) == textwrap.dedent("""\
        (import base64)
        (import os.path as p sys)
        (from math import sqrt pi as PI)
        (from collections import deque Counter)""")


def test_assert():
    assert s("assert x > 0, 'x must be positive'") == "(assert (> x 0) 'x must be positive')"


# ---- compound statements ---------------------------------------------------


def test_if_elif_else():
    assert s("""
        if n := len(xs):
            a = 1
        elif b:
            a = 2
        else:
            a = 3
    """) == textwrap.dedent("""\
        if (:= n (call len xs)):
          (= a 1)
        else:
          if b:
            (= a 2)
          else:
            (= a 3)""")


def test_loops():
    assert s("""
        while i < n:
            i += 1
            if done: break
        else:
            pass
        for i, x in enumerate(xs):
            continue
    """) == textwrap.dedent("""\
        while (< i n):
          (+= i 1)
          if done:
            (break)
        else:
          (pass)
        for (tuple i x) in (call enumerate xs):
          (continue)""")


def test_function_definitions():
    assert s("""
        def add(a: int, b: int = 1) -> int:
            return a + b

        def biggest[T](xs: list[T]) -> T?:
            pass

        def f(self, x, y: int | str = 'a',) -> dict[str, list[int]]: pass
    """) == textwrap.dedent("""\
        def add(a: int, b: int = 1) -> int:
          (return (+ a b))
        def biggest[T](xs: list[T]) -> T?:
          (pass)
        def f(self, x, y: int | str = 'a') -> dict[str, list[int]]:
          (pass)""")


def test_value_class_and_class():
    assert s("""
        @value
        class Point:
            x: float
            y: float = 0.0

            def length(self) -> float:
                return sqrt(self.x ** 2 + self.y ** 2)

        class Dog(Animal):
            pass

        class Box[T]:
            item: T
    """) == textwrap.dedent("""\
        @value
        class Point:
          (: x float)
          (: y float 0.0)
          def length(self) -> float:
            (return (call sqrt (+ (** (. self x) 2) (** (. self y) 2))))
        class Dog(Animal):
          (pass)
        class Box[T]:
          (: item T)""")


def test_lambdas():
    assert e("lambda: 0") == "(lambda () 0)"
    assert e("lambda x, y: x + y") == "(lambda (x y) (+ x y))"
    assert e("lambda x: lambda y: x * y") == "(lambda (x) (lambda (y) (* x y)))"
    assert e("f(key=lambda s: s.lower(), reverse=True)") == "(call f key=(lambda (s) (call (. s lower))) reverse=True)"
    assert e("lambda x: a if x else b") == "(lambda (x) (if-exp x a b))"


def test_function_types():
    assert s("f: (int, str) -> bool = g") == "(: f (int, str) -> bool g)"
    assert s("f: () -> None = g") == "(: f () -> None g)"
    assert s("f: ((int) -> int)? = None") == "(: f ((int) -> int)? None)"
    assert s("f: (int) -> (int) -> int = g") == "(: f (int) -> (int) -> int g)"
    assert s("f: Callable[[int, int], int] = g") == "(: f (int, int) -> int g)"
    assert s("f: Callable[[], None] = g") == "(: f () -> None g)"
    assert s("def f(g: (int) -> int) -> (str) -> str:\n    pass") == "def f(g: (int) -> int) -> (str) -> str:\n  (pass)"


def test_with():
    assert s("""
        with open("a") as f, lock:
            pass
        with (open("a") as f, open("b") as g,):
            pass
        with (a):
            pass
        with (yield_value(1), 2) as (x, y):
            pass
    """) == textwrap.dedent("""\
        with (call open 'a') as f, lock:
          (pass)
        with (call open 'a') as f, (call open 'b') as g:
          (pass)
        with a:
          (pass)
        with (tuple (call yield_value 1) 2) as (tuple x y):
          (pass)""")


def test_decorators():
    assert s("""
        @dataclass(order=True)
        class P:
            x: int
            @property
            def double(self) -> int:
                return self.x * 2
            @double.setter
            def double(self, v: int):
                pass
        @functools.cache
        @retry(3)
        def f(n: int) -> int:
            return n
    """) == textwrap.dedent("""\
        @(call dataclass order=True)
        class P:
          (: x int)
          @property
          def double(self) -> int:
            (return (* (. self x) 2))
          @(. double setter)
          def double(self, v: int):
            (pass)
        @(. functools cache)
        @(call retry 3)
        def f(n: int) -> int:
          (return n)""")


def test_types():
    assert s("x: (int | str)? = None") == "(: x (int | str)? None)"
    assert s("x: mod.Thing") == "(: x mod.Thing)"


def test_the_demo_program():
    assert s("""
        import base64

        def greet(name: str) -> str:
            n = len(name)
            n = str(n)
            return f"hi {name} ({n} chars)"

        if __name__ == "__main__":
            print(greet("claude"))
    """) == textwrap.dedent("""\
        (import base64)
        def greet(name: str) -> str:
          (= n (call len name))
          (= n (call str n))
          (return (fstr 'hi ' {name} ' (' {n} ' chars)'))
        if (== __name__ '__main__'):
          (expr (call print (call greet 'claude')))""")


def test_locations_are_recorded():
    [fn] = parse("def f(x):\n    return x + 1\n").body
    assert fn.loc == Loc(1, 1)
    [ret] = fn.body
    assert ret.loc == Loc(2, 5)
    assert ret.value.loc == Loc(2, 14)  # binary ops point at the operator


# ---- errors -----------------------------------------------------------------


@pytest.mark.parametrize("src,msg,loc", [
    ("if x\n    y\n", "expected ':' after 'if' statement, found end of line", Loc(1, 5)),
    ("if x:\ny\n", "expected an indented block after 'if' statement", Loc(2, 1)),
    ("x = 1\n  y = 2\n", "unexpected indent", Loc(2, 1)),
    ("x = \n", "expected an expression, found end of line", Loc(1, 5)),
    ("1 = x\n", "cannot assign to a literal", Loc(1, 1)),
    ("f() = x\n", "cannot assign to a function call", Loc(1, 1)),
    ("a, 1 = x\n", "cannot assign to a literal", Loc(1, 4)),
    ("print 'hi'\n", "expected end of line, found string", Loc(1, 7)),
    ("f(a=1, b)\n", "positional argument follows keyword argument", Loc(1, 8)),
    ("f(a=1, a=2)\n", "keyword argument repeated: 'a'", Loc(1, 8)),
    ("def f(a=1, b): pass\n", "parameter without a default follows parameter with a default", Loc(1, 12)),
    ("def f(a, a): pass\n", "duplicate parameter 'a'", Loc(1, 10)),
    ("else:\n    pass\n", "'else' without a matching 'if'", Loc(1, 1)),
    ("def h(a, *):\n    pass\n", "named arguments must follow bare *", Loc(1, 10)),
    ("def h(a, *, b, *c):\n    pass\n", "* argument may appear only once", Loc(1, 17)),
    ("def h(a, *, b, /):\n    pass\n", "/ must be ahead of *", Loc(1, 16)),
    ("def h(/, a):\n    pass\n", "at least one argument must precede /", Loc(1, 7)),
    ("*a = [1]\n", "starred assignment target must be in a list or tuple", Loc(1, 1)),
    ("a, *b, *c = [1, 2]\n", "multiple starred expressions in assignment", Loc(1, 8)),
    ("print((*a))\n", "cannot use starred expression here", Loc(1, 8)),
    ("f(**d)\n", "'**' arguments aren't supported yet; pass the keywords by name", Loc(1, 3)),
    ("del f()\n", "'del' takes names, items and slices: del x, xs[i], d[k], xs[1:3]", Loc(1, 5)),
    ("with f() x:\n    pass\n", "expected ':' after 'with', found name 'x'", Loc(1, 10)),
    ("try:\n    pass\nx = 1\n", "expected 'except' or 'finally' after the 'try' block, found name 'x'", Loc(3, 1)),
    ("try:\n    pass\nexcept:\n    pass\nexcept E:\n    pass\n", "a bare 'except:' must be the last except clause", Loc(3, 1)),
    ("try:\n    pass\nfinally:\n    pass\nelse:\n    pass\n", "'else' without a matching 'if'", Loc(5, 1)),
    ("except E:\n    pass\n", "'except' without a matching 'try'", Loc(1, 1)),
    ("f = lambda x=1: x\n", "lambda parameters can't have default values", Loc(1, 13)),
    ("x: (int, str) = 1\n", "expected '->' after a parameter list in a function type", Loc(1, 15)),
    ("x: 5 = 1\n", "expected a type, found number 5", Loc(1, 4)),
    ("f(x for x in xs, 1)\n", "generator expression must be parenthesized", Loc(1, 3)),
    ("case 1:\n    pass\n", "'case' outside a 'match' statement", Loc(1, 1)),
    ("match x:\n    pass\n", "expected 'case' inside 'match', found 'pass'", Loc(2, 5)),
    ("match x:\n    case *a:\n        pass\n", "a starred pattern needs to be inside a sequence pattern", Loc(2, 10)),
    ("match x:\n    case {**_}:\n        pass\n", "'**_' isn't allowed; leave it out to ignore the other keys", Loc(2, 13)),
    ("match x:\n    case f\"a\":\n        pass\n",
     "an f-string can't be a pattern; match a plain string, or use a guard (`case s if ...`)", Loc(2, 10)),
    ("match x:\n    case P(x=1, 2):\n        pass\n", "positional patterns follow keyword patterns", Loc(2, 17)),
])
def test_errors(src, msg, loc):
    err = parse_error(src)
    assert (err.message, err.loc) == (msg, loc)


def test_match_is_a_soft_keyword():
    tree = parse("match = 1\nmatch.x = 2\nmatch(3)\nmatch -x:\n    case -1:\n        pass\n")
    assert [type(s).__name__ for s in tree.body] == ["Assign", "Assign", "ExprStmt", "Match"]
    case = tree.body[3].cases[0]
    assert case.pattern == A.MatchValue(A.UnaryOp("-", A.IntLit(1)))


def test_match_patterns():
    tree = parse("match p:\n    case Point(0, y=[a, *rest]) | {'k': _, **others} as q if q:\n        pass\n")
    [case] = tree.body[0].cases
    assert case.pattern == A.MatchAs(
        A.MatchOr([
            A.MatchClass(A.Name("Point"), [A.MatchValue(A.IntLit(0))], ["y"],
                         [A.MatchSequence([A.MatchAs(None, A.Name("a")), A.MatchStar(A.Name("rest"))])]),
            A.MatchMapping([A.StrLit("k")], [A.MatchAs(None, None)], A.Name("others")),
        ]),
        A.Name("q"),
    )
    assert case.guard == A.Name("q")


def test_struct_is_an_ordinary_name():
    assert e("struct.pack('<i', 1)") == "(call (. struct pack) '<i' 1)"
    s("import struct\nstruct = 3\n")
    err = parse_error("struct Point:\n    x: int\n")
    assert err.message == (
        "seadash's value types are written `@value class Point:` now (with `from seadash import value`), "
        "so that `import struct` works"
    )
