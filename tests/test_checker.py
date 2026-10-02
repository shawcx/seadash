import textwrap

import pytest

from seadash.checker import ModuleInfo, check
from seadash.errors import CompileError, CheckError, Loc
from seadash.parser import parse


def ok(source: str) -> ModuleInfo:
    source = textwrap.dedent(source)
    if "@value" in source and "import value" not in source:
        source += "\nfrom seadash import value\n"  # (at the end, so line numbers in messages stay put)
    return check(parse(source))


def err(source: str) -> CheckError:
    with pytest.raises(CheckError) as info:
        ok(source)
    return info.value


def variables(info: ModuleInfo, function: str | None = None) -> list[str]:
    """`cpp_name: type` for each variable of a function (None = module code)."""
    if function is None:
        vs = info.globals + info.main_locals
    else:
        [fn] = [f for f in info.functions if f.name == function]
        vs = fn.locals
    return [f"{v.cpp_name}: {v.type}" for v in vs]


def fn(body: str, params: str = "", ret: str = "") -> str:
    """Wrap statements in a function so they're checked as locals."""
    arrow = f" -> {ret}" if ret else ""
    return f"def f({params}){arrow}:\n" + textwrap.indent(textwrap.dedent(body), "    ")


# ---- rebinding --------------------------------------------------------------


def test_rebinding_to_a_new_type_makes_a_new_variable():
    info = ok(fn("""
        a = 2
        a = str(a)
        a = a + "!"
    """))
    assert variables(info, "f") == ["a: int", "a_1: str"]


def test_same_type_reuses_the_variable():
    info = ok(fn("""
        a = 2
        a = 3
        a += 1
    """))
    assert variables(info, "f") == ["a: int"]


def test_int_into_float_variable_is_widened_not_rebound():
    info = ok(fn("""
        x: float = 0
        x = 1
        x = x / 2
    """))
    assert variables(info, "f") == ["x: float"]


def test_aug_assign_can_rebind_like_python():
    info = ok(fn("""
        n = 1
        n += 0.5
    """))
    assert variables(info, "f") == ["n: int", "n_1: float"]


def test_switching_back_reuses_the_first_variable():
    info = ok(fn("""
        a = 1
        a = "x"
        a = 2
    """))
    assert variables(info, "f") == ["a: int", "a_1: str"]


def test_rebinding_at_module_level():
    info = ok("""
        a = 2
        a = str(a)
        print(a)
    """)
    assert variables(info) == ["a: int", "a_1: str"]


# ---- control flow merges ----------------------------------------------------


def test_branches_that_agree_are_fine():
    ok(fn("""
        if True:
            a = 1
        else:
            a = 2
        print(a)
    """))


def test_branches_that_disagree_are_an_error_when_read():
    e = err(fn("""
        if True:
            a = 1
        else:
            a = "one"
        print(a)
    """))
    assert "'a' has different types depending on the path" in e.message
    assert "int (line 4), str (line 6)" in e.message
    assert e.loc == Loc(7, 11)


def test_branches_that_disagree_are_fine_if_reassigned_before_use():
    ok(fn("""
        if True:
            a = 1
        else:
            a = "one"
        a = 2.5
        print(a)
    """))


def test_maybe_unassigned():
    e = err(fn("""
        if True:
            a = 1
        print(a)
    """))
    assert e.message == "'a' might not be assigned yet"


def test_used_before_assignment():
    e = err(fn("""
        print(a)
        a = 1
    """))
    assert e.message == "'a' is used before it's assigned"


def test_return_in_one_branch_means_the_other_decides():
    ok(fn("""
        if True:
            return 0
        else:
            a = 5
        return a
    """, ret="int"))


def test_loop_that_changes_a_type_is_caught_on_the_second_iteration():
    e = err(fn("""
        a = 0
        for x in range(3):
            print(a + 1)
            a = str(a)
    """))
    assert "'a' has different types" in e.message
    assert e.loc.line == 5


def test_loop_with_consistent_types_is_fine():
    info = ok(fn("""
        total = 0
        for x in [1, 2, 3]:
            total += x
        print(total)
    """))
    assert variables(info, "f") == ["total: int", "x: int"]


def test_variable_first_assigned_in_loop_may_be_unassigned_after():
    e = err(fn("""
        for x in range(3):
            last = x
        print(last)
    """))
    assert e.message == "'last' might not be assigned yet"


def test_while_true_only_exits_through_break():
    ok(fn("""
        while True:
            line = input()
            if line == "q":
                break
        print(line)
    """))


def test_break_continue_outside_loop():
    assert err(fn("break")).message == "'break' outside a loop"


# ---- optionals and narrowing ------------------------------------------------


def test_optional_needs_a_check_before_use():
    e = err("""
        @value
        class P:
            x: int
        def f(p: P?) -> int:
            return p.x
    """)
    assert e.message == "P? might be None; check it first, e.g. `if p is not None:`"


@pytest.mark.parametrize("condition", ["p is not None", "p", "p != None"])
def test_narrowing_in_if(condition):
    ok(f"""
        @value
        class P:
            x: int
        def f(p: P?) -> int:
            if {condition}:
                return p.x
            return 0
    """)


def test_narrowing_after_early_return():
    ok("""
        @value
        class P:
            x: int
        def f(p: P?) -> int:
            if p is None:
                return 0
            return p.x
    """)


def test_narrowing_with_and_and_not():
    ok("""
        def f(n: int?, m: int?) -> bool:
            if not n:
                return False
            return n > 3 and m is not None and m + 1 > 4
    """)


def test_redundant_none_check_after_narrowing_is_reported():
    e = err("""
        def f(n: int?) -> bool:
            if not n:
                return False
            return n is not None
    """)
    assert e.message == "int can never be None (only T? types can)"


def test_narrowing_with_walrus():
    ok("""
        def find(xs: list[int], x: int) -> int?:
            for i, y in enumerate(xs):
                if y == x:
                    return i
            return None

        def f() -> int:
            if (i := find([1, 2], 2)) is not None:
                return i + 1
            if j := find([3], 3):
                return j
            return -1
    """)


def test_narrowing_with_assert():
    ok(fn("""
        assert n is not None
        return n + 1
    """, params="n: int?", ret="int"))


def test_or_default_unwraps_optional():
    info = ok(fn("""
        d: dict[str, int] = {}
        count = d.get("a") or 0
    """))
    assert "count: int" in variables(info, "f")


def test_narrowing_is_undone_by_merge():
    e = err(fn("""
        if n is not None:
            print(n + 1)
        print(n + 1)
    """, params="n: int?"))
    assert e.message == "int? might be None; check it first, e.g. `if n is not None:`"


def test_assigning_none_needs_annotation():
    e = err(fn("x = None"))
    assert e.message == "can't tell what type 'x' should be from None alone; annotate it, e.g. `x: int? = None`"
    info = ok(fn("""
        x: int? = None
        x = 5
        print(x + 1)
    """))
    assert variables(info, "f") == ["x: int?"]


def test_is_none_on_non_optional():
    assert err(fn("print(n is None)", params="n: int")).message == "int can never be None (only T? types can)"


def test_is_between_an_optional_and_an_object():
    ok("""
    class Node:
        parent: Node?
    root = Node(None)
    child = Node(root)
    print(child.parent is root, root is child.parent, child.parent is not child, child.parent is root.parent)
    xs: list[int]? = None
    print(xs is [1])
    """)
    assert err(fn("print(a is b)", params="a: int?, b: int")).message == (
        "'is' is for None checks, class instances, lists, dicts and sets; use '==' to compare values")


# ---- functions --------------------------------------------------------------


def test_params_need_annotations():
    e = err("def f(x): pass")
    assert e.message == "parameter 'x' needs a type annotation, e.g. `x: int`"


def test_return_type_required_to_return_a_value():
    e = err(fn("return 5"))
    assert e.message == "'f' returns a value but has no return type; add `-> int` to its definition"


def test_missing_return():
    e = err(fn("""
        if n > 0:
            return 1
    """, params="n: int", ret="int"))
    assert e.message == "function 'f' can reach its end without returning a value (it's declared to return int)"


def test_optional_return_may_fall_off_the_end():
    ok(fn("pass", ret="int?"))


def test_wrong_return_type():
    assert err(fn("return 'x'", ret="int")).message == "'f' should return int, not str"


@pytest.mark.parametrize("call,msg", [
    ("add(1)", "add() is missing argument 'b'"),
    ("add(1, 2, 3)", "add() takes 2 arguments but 3 were given"),
    ("add(1, c=2)", "add() got an unexpected keyword argument 'c'"),
    ("add(1, a=2)", "add() got multiple values for argument 'a'"),
    ("add(1, 'x')", "argument 'b' of add() must be int, not str"),
    ("nope(1)", "name 'nope' is not defined"),
])
def test_call_errors(call, msg):
    e = err(f"""
        def add(a: int, b: int) -> int:
            return a + b
        {call}
    """)
    assert e.message == msg


def test_calls_with_defaults_and_keywords():
    info = ok("""
        def greet(name: str, punct: str = "!", times: int = 1) -> str:
            return (name + punct) * times
        x = greet("hi", times=3)
    """)
    [g] = [f for f in info.functions if f.name == "greet"]
    assert g.ret.name == "str"


def test_functions_can_call_each_other_in_any_order():
    ok("""
        def a() -> int:
            return b() + 1
        def b() -> int:
            return 1
    """)


# ---- globals ----------------------------------------------------------------


def test_functions_can_read_single_assignment_globals():
    info = ok("""
        LIMIT = 10
        names: list[str] = []
        def f() -> int:
            names.append("x")
            return LIMIT
    """)
    assert variables(info) == ["LIMIT: int", "names: list[str]"]


def test_functions_cannot_read_rebound_globals():
    e = err("""
        a = 1
        a = "x"
        def f() -> str:
            return a
    """)
    assert e.message.startswith("functions can only use module-level variables that are assigned exactly once")


def test_local_assignment_shadows_global_for_whole_function():
    e = err("""
        x = 1
        def f():
            print(x)
            x = 2
    """)
    assert e.message == "'x' is used before it's assigned"


# ---- structs and classes ----------------------------------------------------


def test_struct_constructor_and_methods():
    info = ok("""
        @value
        class Point:
            x: float
            y: float = 0.0

            def scaled(self, k: float) -> Point:
                return Point(self.x * k, self.y * k)

        p = Point(3)
        q = p.scaled(2).scaled(k=0.5)
        p.x = 5
    """)
    assert variables(info) == ["p: Point", "q: Point"]


@pytest.mark.parametrize("line,msg", [
    ("p = Point()", "Point() is missing argument 'x'"),
    ("p = Point(1, 2, 3)", "Point() takes 2 arguments but 3 were given"),
    ("print(Point(1).z)", "Point has no field 'z'"),
    ("Point(1).nope()", "Point has no method 'nope'"),
    ("Point(1).x = 'a'", "field 'x' is float, can't assign str"),
    ("Point(1).norm(1)", "norm() takes 0 arguments but 1 were given"),
])
def test_struct_errors(line, msg):
    e = err(f"""
        @value
        class Point:
            x: float
            y: float = 0.0
            def norm(self) -> float:
                return self.x
        {line}
    """)
    assert e.message == msg


def test_class_with_init():
    ok("""
        class Counter:
            count: int
            step: int

            def __init__(self, step: int = 1):
                self.count = 0
                self.step = step

            def tick(self):
                self.count += self.step

        c = Counter(step=2)
        c.tick()
    """)


def test_struct_cannot_contain_itself_but_class_can():
    e = err("""
        @value
        class Node:
            value: int
            next: Node?
    """)
    assert e.message == (
        "@value class 'Node' can't contain itself (field 'next'): it would be infinitely large. "
        "Remove @value to make it an ordinary class, or use a list"
    )
    ok("""
        class Node:
            value: int
            next: Node?
    """)


def test_struct_body_restrictions():
    e = err("""
        @value
        class P:
            print(1)
    """)
    assert e.message == ("a struct body can only contain fields (`x: int`), methods (`def ...`) and class attributes "
                         '(`version = "1.0"`)')


def test_method_needs_self():
    assert err("@value\nclass P:\n    def f(): pass\n").message == "method 'f' needs 'self' as its first parameter"


# ---- expressions and operators ----------------------------------------------


@pytest.mark.parametrize("expr,ty", [
    ("1 + 2", "int"),
    ("1 + 2.0", "float"),
    ("7 / 2", "float"),
    ("7 // 2", "int"),
    ("2 ** 10", "int"),
    ("'a' + 'b'", "str"),
    ("'ab' * 3", "str"),
    ("[1] + [2]", "list[int]"),
    ("[1, 2.5]", "list[float]"),
    ("[[1], [2, 3]]", "list[list[int]]"),
    ("{'a': 1}", "dict[str, int]"),
    ("{1, 2}", "set[int]"),
    ("(1, 'a')", "tuple[int, str]"),
    ("(1, 'a')[1]", "str"),
    ("'hello'[1:3]", "str"),
    ("[1, 2, 3][::-1]", "list[int]"),
    ("1 < 2 < 3", "bool"),
    ("3 in [1, 2]", "bool"),
    ("'ell' in 'hello'", "bool"),
    ("1 if True else 2.5", "float"),
    ("[x * 2 for x in range(3)]", "list[int]"),
    ("{w: len(w) for w in ['a', 'bb']}", "dict[str, int]"),
    ("[(i, c) for i, c in enumerate('abc')]", "list[tuple[int, str]]"),
    ("sum(x for x in [1.5, 2])", "float"),
    ("sorted({3, 1, 2})", "list[int]"),
    ("'a,b'.split(',')", "list[str]"),
    ("', '.join(['a', 'b'])", "str"),
    ("{'a': 1}.get('a')", "int?"),
    ("{'a': 1}.get('a', 0)", "int"),
    ("max(1, 2.5)", "float"),
    ("min([3, 1])", "int"),
    ("abs(-3)", "int"),
    ("round(2.5)", "int"),
    ("f'{1 + 1} is two'", "str"),
])
def test_expression_types(expr, ty):
    info = ok(f"x = {expr}\n")
    [v] = info.globals
    assert str(v.type) == ty


@pytest.mark.parametrize("expr,msg", [
    ("'n = ' + 5", "unsupported operand types for +: str and int (convert with str(...), or use an f-string)"),
    ("[1, 'a']", "list items have different types: int and str"),
    ("[]", "can't tell what type of list this is; annotate the variable, e.g. `xs: list[int] = []`"),
    ("{}", "can't tell what type of dict this is; annotate the variable, e.g. `d: dict[str, int] = {}`"),
    ("1 == 'a'", "comparing int with str using '==' is always False"),
    ("1 < 'a'", "'<' isn't supported between int and str"),
    ("'a' in [1]", "a str can never be in a list[int]"),
    ("len(5)", "len() argument must be a str, list, dict, set or tuple, not int"),
    ("len()", "len() takes exactly 1 argument (0 given)"),
    ("[1].append('x')", "list.append() argument must be int, not str"),
    ("'abc'.nope()", "str has no method 'nope'"),
    ("(1, 'a')[5]", "tuple index 5 is out of range for tuple[int, str]"),
    ("5[0]", "int can't be indexed"),
    ("'%d' % 5", "'%' formatting isn't supported; use an f-string: f\"{x}\""),
    ("1 if True else 'a'", "the two branches have different types: int and str"),
    ("print('x')", "this call doesn't return a value"),
    ("range(3)", "can't store range(...) in a variable; loop over it directly, or make a list with list(...)"),
    ("{[1]: 2}", "dict keys must be int, float, str, bool, or a tuple of those; not list[int]"),
    ("'s'.join([1, 2])", "str.join() needs strings, not int (convert with str(...) first)"),
    ("print", "'print' can only be used as a value where a function type is expected; otherwise wrap it in a lambda"),
])
def test_expression_errors(expr, msg):
    assert err(f"x = {expr}\n").message == msg


def test_empty_containers_take_their_type_from_the_annotation():
    info = ok("""
        xs: list[int] = []
        d: dict[str, list[int]] = {}
        s: set[str] = {}
        d["a"] = []
        xs = []
    """)
    # xs is assigned twice, so it's local to the module code rather than a global
    assert info.main_locals[0].name == "xs"
    assert variables(info) == ["d: dict[str, list[int]]", "s: set[str]", "xs: list[int]"]


def test_tuple_unpacking_and_swap():
    info = ok(fn("""
        a, b = 1, "x"
        b, a = a, b
    """))
    assert variables(info, "f") == ["a: int", "b: str", "b_1: int", "a_1: str"]


def test_immutable_values():
    assert err(fn("s = 'abc'\ns[0] = 'x'")).message == "str can't be changed in place (it's immutable)"


def test_cannot_loop_over_int():
    assert err(fn("for i in 10:\n    pass")).message == "can't loop over int"


# ---- modules ----------------------------------------------------------------


def test_math_module():
    info = ok("""
        import math
        from math import sqrt as root, pi
        x = math.sqrt(2) + root(3) * pi
        n = math.floor(2.5)
    """)
    assert variables(info) == ["x: float", "n: int"]


def test_unknown_module():
    assert err("import requests").message == "no module named 'requests'"
    assert err("import colections").message == "no module named 'colections'; did you mean 'collections'?"


def test_unknown_module_member():
    assert err("import math\nx = math.nope(1)").message == "module 'math' has no member 'nope'"


# ---- not yet supported -------------------------------------------------------


@pytest.mark.parametrize("src,msg", [
    ("x: int | str = 1", "union types are not supported yet (T? or `T | None` for 'T or None' is)"),
    ("@value\nclass A: pass\n@value\nclass B(A): pass", "a @value class can't inherit (it's a value, not a shared object): remove @value to make `class B(A):` an ordinary class"),
    ("@value\nclass A: pass\nclass B(A): pass", "can't inherit from 'A', a @value class; only ordinary classes can be inherited from"),
    ("def f():\n    class C: pass", "a class can only be defined at the top level of a module"),
])
def test_not_yet_supported(src, msg):
    assert err(src).message == msg


# ---- exceptions -------------------------------------------------------------


@pytest.mark.parametrize("src,msg", [
    ("raise", "a bare 'raise' can only re-raise inside an 'except' block"),
    ("raise 5", "can only raise exceptions, not int"),
    ("@value\nclass P:\n    x: int\nraise P", "can only raise exceptions, not P"),
    ("class E(Exception):\n    code: int\nraise E", "E needs arguments: `raise E(...)`"),
    ("try:\n    pass\nexcept int:\n    pass", "'except' needs an exception class, like `except ValueError:`"),
    ("@value\nclass S(Exception):\n    pass", "an exception can't be a @value class: remove @value to make `class S(Exception):` an ordinary class"),
    ("class ValueError(Exception):\n    pass", "can't define a class named 'ValueError'; that's a built-in type"),
    ("def f() -> int:\n    try:\n        return 1\n    finally:\n        return 2", "'return' can't be used inside a 'finally' block"),
    ("for i in range(3):\n    try:\n        pass\n    finally:\n        break", "'break' can't leave a 'finally' block"),
    ("try:\n    x = int('1')\nexcept ValueError:\n    print(x)", "'x' might not be assigned yet"),
    ("try:\n    pass\nexcept ValueError as e:\n    pass\nprint(e)", "'e' might not be assigned yet"),
])
def test_exception_errors(src, msg):
    assert err(src).message == msg


def test_exception_types_and_hierarchy():
    info = ok("""
        class AppError(Exception):
            code: int = 1
        class DbError(AppError):
            table: str = ""

        def f() -> int:
            try:
                raise DbError("down", 2, "users")
            except (KeyError, IndexError) as e:
                print(e.message)
                return 1
            except AppError as e:
                return e.code
            return 0
    """)
    [f] = info.functions
    assert variables(info, "f") == ["e: LookupError", "e_1: AppError"]


def test_break_inside_loop_inside_finally_is_fine():
    ok(fn("""
        try:
            pass
        finally:
            for i in range(3):
                break
    """))


def test_try_else_sees_body_assignments():
    ok(fn("""
        try:
            n = int("5")
        except ValueError:
            return 0
        else:
            n += 1
        return n
    """, ret="int"))


# ---- functions as values ------------------------------------------------------


@pytest.mark.parametrize("src,ty", [
    ("lambda: 42", "() -> int"),
    ("double", "(int) -> int"),
    ("P(2).scaled", "(float) -> P"),
])
def test_function_value_types(src, ty):
    info = ok(f"""
        @value
        class P:
            x: float
            def scaled(self, k: float) -> P:
                return P(self.x * k)
        def double(n: int) -> int:
            return n * 2
        f = {src}
    """)
    assert str(info.globals[-1].type) == ty


def test_lambda_parameter_types_come_from_context():
    info = ok("""
        def apply(f: (int) -> int, x: int) -> int:
            return f(x)
        def make_adder(n: int) -> (int) -> int:
            return lambda x: x + n
        a = apply(lambda v: v * 3, 2)
        add5 = make_adder(5)
        b = add5(1)
        ops: dict[str, (float, float) -> float] = {"+": lambda x, y: x + y, "max": lambda x, y: max(x, y)}
        halve: (int) -> float = lambda n: n / 2
        words = sorted(["bb", "a", "ccc"], key=lambda w: (len(w), w))
        longest = max(["bb", "a"], key=len)
        lower = sorted(["B", "a"], key=str.lower)
        roots = list(map(math.sqrt, [1, 4]))
        lengths = list(map(len, ["a", "bb"]))
        evens = list(filter(lambda n: n % 2 == 0, range(10)))
        callbacks: list[() -> None] = [lambda: print("hi")]
    """.replace("        a = ", "        import math\n        a = ", 1))
    types = {v.name: str(v.type) for v in info.globals}
    assert types["add5"] == "(int) -> int"
    assert types["b"] == "int"
    assert types["halve"] == "(int) -> float"
    assert types["longest"] == "str"
    assert types["roots"] == "list[float]"
    assert types["lengths"] == "list[int]"
    assert types["evens"] == "list[int]"


@pytest.mark.parametrize("src,msg", [
    ("f = lambda x: x", "can't tell the types of this lambda's parameters from here; give it a type, e.g. `f: (int) -> int = lambda ...`"),
    ("f: (int) -> int = lambda a, b: a", "this lambda takes 2 parameters, but (int) -> int is expected here"),
    ("f: (int) -> int = lambda a: 'x'", "'f' is declared as (int) -> int, but the value is (int) -> str"),
    ("def g(f: (int) -> int) -> None:\n    f('x')", "argument 1 must be int, not str"),
    ("def g(f: (int) -> int) -> None:\n    f(1, 2)", "this function takes 1 argument but 2 were given"),
    ("def g(f: (int) -> int) -> None:\n    f(a=1)",
     "keyword arguments can't be used when calling a function value whose parameters aren't known here"),
    ("f: (int) -> int = lambda a: a\nf('x')", "argument 'a' of <lambda>() must be int, not str"),
    ("f: (int) -> int = lambda a: a\nf(b=1)", "<lambda>() got an unexpected keyword argument 'b'"),
    ("f: ((int) -> int)? = None\nf(1)", "((int) -> int)? might be None; check it first"),
    ("x = 5\nx(1)", "int is not callable"),
    ("xs = sorted([{1}], key=lambda s: s)", "sorted() key must return something comparable, not set[int]"),
    ("xs = sorted([{1}])", "sorted() can't compare set[int] values"),
    ("xs = list(map(lambda n: print(n), [1]))", "map() function must return a value"),
    ("xs = max(1, 2, key=abs)", "max() only supports key= with a single iterable argument"),
    ("f: (int) -> int = lambda a: (b := a)", "':=' can't be used inside a lambda"),
    ("f = len", "'len' can only be used as a value where a function type is expected; otherwise wrap it in a lambda"),
])
def test_function_value_errors(src, msg):
    assert err(src).message == msg


def test_callback_results_can_be_ignored():
    ok("""
        def run(action: (int) -> None):
            action(1)
        def double(n: int) -> int:
            return n * 2
        run(double)
        run(lambda n: print(n))
    """)


# ---- nested functions and closures -----------------------------------------------


def captured(info: ModuleInfo, function: str) -> list[str]:
    [fn] = [f for f in info.functions if f.name == function]
    return sorted(v.cpp_name for v in fn.locals if v.captured)


def test_captured_variables_are_marked():
    info = ok("""
        def f() -> int:
            shared = 1
            private = 2
            def g() -> int:
                return shared + 1
            h: () -> int = lambda: shared * 2
            return g() + h() + private
    """)
    assert captured(info, "f") == ["shared"]


def test_self_recursion_does_not_capture_itself():
    info = ok("""
        def f() -> int:
            def fact(n: int) -> int:
                return 1 if n <= 1 else n * fact(n - 1)
            return fact(5)
    """)
    assert captured(info, "f") == []


@pytest.mark.parametrize("src,msg", [
    ("def f():\n    nonlocal x", "'nonlocal' is only allowed in nested functions"),
    ("nonlocal x", "'nonlocal' is only allowed in nested functions"),
    ("def f():\n    def g():\n        nonlocal y\n        y = 1", "no variable 'y' in an enclosing function for 'nonlocal' to use"),
    ("def f():\n    n = 0\n    def g():\n        nonlocal n\n        n = 'x'", "can't change the type of nonlocal 'n' from int to str"),
    ("def f():\n    global g\n    g = 1", "no module-level variable 'g' for 'global' to use"),
    ("x = 1\nx = 2\ndef f():\n    global x\n    x = 3", "'global x' needs a module-level variable assigned exactly once; 'x' is assigned 2 times"),
    ("def f():\n    def g(a: int = 1) -> int:\n        return a", "default values aren't supported in nested functions yet ('a')"),
    ("def f():\n    def g():\n        print(later)\n    later = 1", "'later' isn't assigned yet where this nested function is defined; assign it before the def"),
    ("def f(p: int?):\n    if p is not None:\n        def g() -> int:\n            return p + 1",
     "int? might be None; check it first, e.g. `if p is not None:`"),
    ("def f(p: int?):\n    if p is not None:\n        h: () -> int = lambda: p + 1",
     "int? might be None; check it first, e.g. `if p is not None:`"),
])
def test_closure_errors(src, msg):
    assert err(src).message == msg


def test_global_counter():
    info = ok("""
        count = 0
        def bump():
            global count
            count += 1
    """)
    assert variables(info) == ["count: int"]


def test_nested_def_is_a_function_value():
    info = ok("""
        def make() -> (int) -> int:
            def double(x: int) -> int:
                return x * 2
            return double
    """)
    assert variables(info, "make") == ["double: (int) -> int"]


# ---- bytes and runtime modules ------------------------------------------------


@pytest.mark.parametrize("expr,ty", [
    ('b"ab" + b"c"', "bytes"),
    ('b"ab"[0]', "int"),
    ('b"ab"[1:]', "bytes"),
    ('"x".encode()', "bytes"),
    ('b"x".decode("utf-8")', "str"),
    ('[b for b in b"ab"]', "list[int]"),
    ('bytes([1, 2])', "bytes"),
    ('b"x".hex()', "str"),
])
def test_bytes_types(expr, ty):
    [v] = ok(f"x = {expr}\n").globals
    assert str(v.type) == ty


@pytest.mark.parametrize("src,msg", [
    ('x = b"a" + "b"', "unsupported operand types for +: bytes and str (convert with s.encode() or b.decode())"),
    ('x = bytes("abc")', 'bytes(str) needs an encoding: bytes(s, "utf-8"), or s.encode()'),
    ('b = b"x"\nb[0] = 1', "bytes can't be changed in place (it's immutable)"),
    ('x = "a" b"b"', "can't combine bytes and str literals"),
    ("import zlib\nx = zlib.compress(5)", "zlib.compress() argument must be bytes or str, not int"),
    ("import zlib\nx = zlib.error", "'zlib.error' is a class; it can be called, raised or caught"),
])
def test_bytes_errors(src, msg):
    with pytest.raises(Exception) as info:
        ok(src)
    assert info.value.message == msg


def test_module_exception_classes():
    ok("""
        import zlib
        from zlib import error
        def f(data: bytes) -> bytes:
            try:
                return zlib.decompress(data)
            except zlib.error as e:
                print(e.message)
            except error:
                pass
            raise zlib.error("custom")
        def g(e: zlib.error) -> str:
            return str(e)
    """)


# ---- files and with ------------------------------------------------------------


@pytest.mark.parametrize("src,ty", [
    ('open("f")', "TextIO"),
    ('open("f", "rb")', "BinaryIO"),
    ('open("f", mode="w", encoding="utf-8")', "TextIO"),
    ('open("f").read()', "str"),
    ('open("f", "rb").read(10)', "bytes"),
    ('open("f").readlines()', "list[str]"),
    ('[line for line in open("f", "rb")]', "list[bytes]"),
    ('open("f", "w").write("x")', "int"),
])
def test_file_types(src, ty):
    [v] = ok(f"x = {src}\n").globals
    assert str(v.type) == ty


@pytest.mark.parametrize("src,msg", [
    ('m = "r"\nf = open("x", m)', "open() mode must be a string literal like 'r', 'w' or 'rb' (it decides whether you get str or bytes)"),
    ('import gzip\nm = "rt"\nf = gzip.open("x.gz", m)', "open() mode must be a string literal like 'r', 'w' or 'rb' (it decides whether you get str or bytes)"),
    ('import gzip\nf = gzip.open("x.gz", "rb")\ns: str = f.read()', "'s' is declared as str, but the value is bytes"),
    ('import sqlite3\nc = sqlite3.connect(":memory:")\nrow = c.execute("select 1").fetchone()',
     "sqlite3.Cursor.fetchone() needs to know the row type; annotate the variable, e.g. `row: tuple[int, str] | None = cur.fetchone()`"),
    ('import sqlite3\nc = sqlite3.connect(":memory:")\nrows: list[tuple[list[int]]] = c.execute("select 1").fetchall()',
     "a row column is int, float, str, bytes or bool (or one of those | None), not list[int]"),
    ('import struct\nfmt = "<i"\nstruct.pack(fmt, 1)', "struct.pack() needs its format as a string literal (it decides the types)"),
    ('import struct\nstruct.pack("<ih", 1)', "pack expected 2 items for packing (got 1)"),
    ('import struct\nstruct.pack("<i", "x")', "struct.pack(): this value must be int, not str"),
    ('import struct\nstruct.pack("<z", 1)', "bad char in struct format"),
    ('import struct\ns = struct.Struct("<i")\ns.pack(1.5)', "struct.Struct.pack(): this value must be int, not float"),
    ('import sqlite3\nc = sqlite3.connect(":memory:")\nc.execute("select ?", 5)',
     "sqlite3.Connection.execute() parameters must be a tuple, list or dict of int, float, str, bytes, bool or None values, not int"),
    ('f = open("x", "rw")', "invalid mode: 'rw'"),
    ('f = open("x", "rb", encoding="utf-8")', "binary mode doesn't take an encoding argument"),
    ('open("x", "wb").write("x")', "BinaryIO.write() argument must be bytes, not str"),
    ("with 5:\n    pass", "int can't be used in a 'with' statement (it needs __enter__ and __exit__ methods)"),
    ("class C:\n    def __enter__(self): pass\nwith C():\n    pass", "C can't be used in a 'with' statement: it has no __exit__ method"),
    ("class C:\n    def __enter__(self): pass\n    def __exit__(self, t: int, v: int, tb: int): pass\nwith C():\n    pass",
     "__exit__ takes either no parameters, or one `exc: Exception?` (None when the block finished normally)"),
    ("class C:\n    def __enter__(self): pass\n    def __exit__(self): pass\nwith C() as c:\n    pass",
     "__enter__ doesn't return anything, so there's nothing to bind with 'as'"),
    ("import os\nos.makedirs('a', exists_ok=True)", "os.makedirs() got an unexpected keyword argument 'exists_ok'"),
    ("from typing import Callable\nx = Callable", "'Callable' is a type; it can only be used in annotations"),
])
def test_file_and_with_errors(src, msg):
    assert err(src).message == msg


def test_typing_names_are_accepted():
    info = ok("""
        from typing import Callable, Optional, List, Dict, TextIO
        def f(g: Callable[[int], int], xs: List[int], d: Dict[str, int], o: Optional[int], out: TextIO) -> int:
            return g(xs[0])
        x: Optional[int] = None
    """)
    assert variables(info) == ["x: int?"]


def test_variables_after_suppressing_with_may_be_unassigned():
    e = err("""
        class Quiet:
            def __enter__(self): pass
            def __exit__(self, exc: Exception?) -> bool:
                return True
        with Quiet():
            value = int("x")
        print(value)
    """)
    assert e.message == "'value' might not be assigned yet"


# ---- json and literal inference -------------------------------------------------


@pytest.mark.parametrize("src,msg", [
    ("import json\nx = json.loads('1')", "json.loads() needs to know what type to produce; annotate the variable, e.g. `data: dict[str, int] = ...`, or use `json.Value` for any JSON"),
    ("import json\nx: dict[int, str] = json.loads('{}')", "JSON object keys are strings, so dict[int, str] can't be decoded"),
    ("import json\nx: bytes = json.loads('1')", "bytes can't be converted to or from JSON"),
    ("import json\nf: (int) -> int = lambda n: n\ns = json.dumps(f)", "(int) -> int can't be converted to or from JSON"),
    ("import json\ns = json.dumps([1], indent='  ')", "json.dumps() argument 'indent' must be int?, not str"),
    ("import json\nv: json.Value = json.loads('1')\nx = v[1.5]", "a json.Value is indexed by int (arrays) or str (objects), not float"),
    ("import json\nx = json.Value", "'Value' is a type; it can only be used in annotations"),
])
def test_json_errors(src, msg):
    assert err(src).message == msg


def test_json_accepts_structs_and_nested_types():
    ok("""
        import json
        from json import Value
        @value
        class P:
            x: float
            tags: set[str]
        class Tree:
            name: str
            kids: list[Tree]
        a: list[P] = json.loads("[]")
        b: Tree = json.loads("{}")
        c: dict[str, tuple[int, str?]] = json.loads("{}")
        d: Value = json.loads("[]")
        s = json.dumps({1: [P(1.0, {"a"})]}, indent=2, sort_keys=True)
    """)


@pytest.mark.parametrize("expr,ty", [
    ('{"a": [1.5], "b": []}', "dict[str, list[float]]"),
    ('{"a": [1.5], "b": [None]}', "dict[str, list[float?]]"),
    ('[[1], [], [None]]', "list[list[int?]]"),
    ('[{"k": 1}, {}]', "list[dict[str, int]]"),
    ('[[1.0, None], [3.0]]', "list[list[float?]]"),
])
def test_literal_items_take_types_from_siblings(expr, ty):
    [v] = ok(f"x = {expr}\n").globals
    assert str(v.type) == ty


def test_literal_items_that_really_differ():
    assert err('x = {"a": [1], "b": {}}').message.startswith("dict values have different types")


# ---- attribute narrowing ----------------------------------------------------------

NARROW_PRELUDE = """
@value
class Address:
    city: str
class User:
    name: str
    address: Address?
    nick: str?
"""


@pytest.mark.parametrize("body", [
    "if u.address is not None:\n    print(u.address.city)",
    "if u.address:\n    print(u.address.city)",
    "if u.address is None:\n    return\nprint(u.address.city)",
    "assert u.address is not None\nprint(u.address.city)",
    "print(u.address.city if u.address is not None else '-')",
    "if u.address is not None and u.address.city == 'x':\n    pass",
    "if u.address is None or u.address.city == 'x':\n    pass",
    "u.address = Address('x')\nprint(u.address.city)",
    "if u.nick is not None:\n    n = len(u.nick)",
    "while u.address is not None:\n    print(u.address.city)\n    u.address = None",
])
def test_attribute_narrowing(body):
    ok(NARROW_PRELUDE + "def f(u: User):\n" + textwrap.indent(body, "    ") + "\n")


@pytest.mark.parametrize("body", [
    "print(u.address.city)",
    "if u.address is not None:\n    u.address = None\n    print(u.address.city)",
    "if u.address is not None:\n    u = User('b', None, None)\n    print(u.address.city)",
    "if u.address is not None:\n    pass\nprint(u.address.city)",
    "if u.address is not None:\n    g: () -> str = lambda: u.address.city",
])
def test_attribute_narrowing_is_undone(body):
    e = err(NARROW_PRELUDE + "def f(u: User):\n" + textwrap.indent(body, "    ") + "\n")
    assert "Address? might be None" in e.message


# ---- inheritance ----------------------------------------------------------------

ANIMALS = """
class Animal:
    name: str
    def speak(self) -> str:
        return "..."
class Dog(Animal):
    def fetch(self) -> str:
        return "ball"
class Cat(Animal):
    pass
"""


def test_subclasses_are_assignable_and_join_to_their_base():
    info = ok(ANIMALS + """
a: Animal = Dog("rex")
pets = [Dog("a"), Cat("b")]
first = pets[0].speak()
""")
    assert variables(info) == ["a: Animal", "pets: list[Animal]", "first: str"]


@pytest.mark.parametrize("body", [
    "if isinstance(a, Dog):\n    print(a.fetch())",
    "if isinstance(a, (Dog, Cat)):\n    print(a.name)",
    "if not isinstance(a, Dog):\n    return\nprint(a.fetch())",
    "if a is not None and isinstance(a, Dog):\n    print(a.fetch())",
])
def test_isinstance_narrows(body):
    ok(ANIMALS + "def f(a: Animal?):\n" + textwrap.indent(body, "    ") + "\n")


@pytest.mark.parametrize("src,msg", [
    ("def f(a: Animal):\n    a.fetch()", "Animal has no method 'fetch'"),
    ("def f(d: Dog):\n    print(isinstance(d, Cat))", "a Dog can never be a Cat (they're unrelated classes)"),
    ("def f(n: int):\n    print(isinstance(n, Dog))", "isinstance() only works on class instances; a int always has the type int"),
    ("def f(a: Animal):\n    print(isinstance(a, int))", "isinstance() needs a class (or a tuple of classes) as its second argument"),
    ("class Bad(Animal):\n    def speak(self) -> int:\n        return 1", "Bad.speak() overrides Animal.speak(), so it must have the same parameter and return types: def speak() -> str"),
    ("class Bad(Animal):\n    def name(self) -> str:\n        return ''", "'name' is a field in Animal; a method can't reuse the name"),
    ("class Bad(Animal):\n    name: int", "field 'name' is already defined"),
    ("def f():\n    super().speak()", "super() only works as `super().method(...)` directly inside a method"),
    ("class Solo:\n    def f(self):\n        super().f()", "Solo has no base class to call with super()"),
    ("class D2(Animal):\n    def f(self):\n        super().nope()", "Animal has no method 'nope'"),
    ("class A1(B1): pass\nclass B1(A1): pass", "'B1' can't inherit from itself"),
])
def test_inheritance_errors(src, msg):
    assert err(ANIMALS + src).message == msg


# ---- generics -------------------------------------------------------------------


def test_generic_function_instances():
    info = ok("""
        def first[T](xs: list[T]) -> T?:
            return xs[0] if xs else None
        a = first([1, 2])
        b = first(["x"])
        c = first[float]([1, 2])
        d: str? = first([])
    """)
    assert variables(info) == ["a: int?", "b: str?", "c: float?", "d: str?"]
    assert sorted(f.cpp_name for f in info.functions if f.cpp_name) == ["first_of_float", "first_of_int", "first_of_str"]


def test_generic_class_inference_and_annotations():
    info = ok("""
        class Box[T]:
            value: T
            def get(self) -> T:
                return self.value
        a = Box(5)
        b: Box[str] = Box("s")
        c = Box[list[int]]([])
        d = a.get() + 1
    """)
    assert variables(info) == ["a: Box[int]", "b: Box[str]", "c: Box[list[int]]", "d: int"]


def test_generic_recursion_and_self_reference():
    ok("""
        class Node[T]:
            value: T
            next: Node[T]?
        def length[T](n: Node[T]?) -> int:
            if n is None:
                return 0
            return 1 + length(n.next)
        x = length(Node(1, Node(2, None)))
    """)


def test_lambda_types_come_from_the_generic_signature():
    info = ok("""
        def mapped[T, U](xs: list[T], f: (T) -> U) -> list[U]:
            return [f(x) for x in xs]
        a = mapped([1, 2], lambda n: str(n))
        b = mapped(["x"], lambda s: len(s) * 1.5)
    """)
    assert variables(info) == ["a: list[str]", "b: list[float]"]


@pytest.mark.parametrize("src,msg", [
    ("def f[T]() -> T?:\n    return None\nx = f()", "can't tell what T should be for f; write the types, e.g. f[int](...)"),
    ("def f[T](a: T, b: T) -> T:\n    return a\nx = f(1, 'a')", "argument 'b' of f() must be int, not str"),
    ("def big[T](xs: list[T]) -> T:\n    return xs[0] if xs[0] > xs[1] else xs[1]\n@value\nclass P:\n    x: int\ny = big([P(1)])",
     "in big[P]: '>' isn't supported between P and P (define __gt__ on P)"),
    ("class Box[T]:\n    v: T\nx: Box = Box(1)", "'Box' needs type arguments: Box[T]"),
    ("class Box[T]:\n    v: T\nx = Box[int, str](1)", "Box takes 1 type argument, not 2"),
    ("def f[T](x: T) -> T:\n    return x\ny: f = 1", "'f' is a generic function, not a type"),
])
def test_generic_errors(src, msg):
    assert err(src).message == msg


# ---- random -----------------------------------------------------------------------


@pytest.mark.parametrize("expr,ty", [
    ("random.choice([1, 2])", "int"),
    ("random.choice('abc')", "str"),
    ("random.choice(range(5))", "int"),
    ("random.sample(['a'], 1)", "list[str]"),
    ("random.choices([1.5], weights=[1], k=3)", "list[float]"),
    ("random.randrange(1, 10, 2)", "int"),
    ("random.gauss(sigma=2)", "float"),
    ("random.randbytes(4)", "bytes"),
])
def test_random_types(expr, ty):
    [v] = ok(f"import random\nx = {expr}\n").globals
    assert str(v.type) == ty


@pytest.mark.parametrize("src,msg", [
    ("random.choice({1: 2})", "random.choice() needs a sequence (a list, str, bytes, range or tuple), not dict[int, int]"),
    ("random.shuffle('abc')", "random.shuffle() shuffles a list in place, not a str"),
    ("random.sample([1])", "random.sample() is missing argument 'k'"),
    ("random.seed('x')", "random.seed() argument 'a' must be int?, not str"),
])
def test_random_errors(src, msg):
    assert err(f"import random\n{src}\n").message == msg


# ---- statistics -------------------------------------------------------------------


@pytest.mark.parametrize("expr,ty", [
    ("statistics.mean([1, 2, 3])", "float"),  # (Python: the int 2, as the mean of ints happens to be whole)
    ("statistics.median((1, 3, 2))", "float"),
    ("statistics.variance(range(5), xbar=2)", "float"),
    ("statistics.median_low([1, 3, 2])", "int"),
    ("statistics.median_high(['a', 'b'])", "str"),
    ("statistics.mode('abca')", "str"),
    ("statistics.multimode([1.5, 2.5])", "list[float]"),
    ("statistics.quantiles([1, 2, 3], n=10)", "list[float]"),
    ("statistics.linear_regression([1, 2], [3, 4])", "LinearRegression"),
    ("statistics.linear_regression([1, 2], [3, 4]).slope", "float"),
    ("statistics.NormalDist(1, 2) * 3 - statistics.NormalDist()", "NormalDist"),
    ("statistics.NormalDist.from_samples([1, 2]).samples(3, seed=1)", "list[float]"),
    ("-statistics.NormalDist().stdev", "float"),
])
def test_statistics_types(expr, ty):
    [v] = ok(f"import statistics\nx = {expr}\n").globals
    assert str(v.type) == ty


@pytest.mark.parametrize("src,msg", [
    ("statistics.mean(['a'])", "statistics.mean() argument 'data' needs numbers (ints or floats), not list[str]"),
    ("statistics.mean(3)", "statistics.mean() argument 'data' must be something you can loop over, not int"),
    ("statistics.median_low([{1: 2}])", "statistics.median_low() argument 'data' needs items that can be compared, "
                                        "not list[dict[int, int]]"),
    ("statistics.quantiles([1, 2], 4)", "statistics.quantiles() takes 1 positional argument but 2 were given"),
    ("statistics.covariance(x=[1], y=[2])", "statistics.covariance() got some positional-only arguments passed as "
                                            "keyword arguments: 'x, y'"),
    ("statistics.correlation([1], [2], 'ranked')", "statistics.correlation() takes 2 positional arguments but 3 were given"),
    ("statistics.fmean([1], weights=['a'])", "statistics.fmean() argument 'weights' needs numbers (ints or floats), not list[str]"),
    ("statistics.variance([1.5], xbar='a')", "statistics.variance() argument 'xbar' must be float?, not str"),
    ("statistics.mean()", "statistics.mean() is missing argument 'data'"),
    ("statistics.NormalDist() / statistics.NormalDist()", "unsupported operand types for /: NormalDist and NormalDist"),
    ("1 / statistics.NormalDist()", "unsupported operand types for /: int and NormalDist"),
    ("statistics.NormalDist().overlap(1.0)", "NormalDist.overlap() argument 'other' must be NormalDist, not float"),
])
def test_statistics_errors(src, msg):
    assert err(f"import statistics\n{src}\n").message == msg


# ---- socket ---------------------------------------------------------------------------


@pytest.mark.parametrize("expr,ty", [
    ("socket.socket()", "socket"),
    ("socket.socket().accept()", "tuple[socket, tuple[str, int]]"),
    ("socket.socket().recv(10)", "bytes"),
    ("socket.socket().send('text')", "int"),
    ("socket.create_connection(('h', 1), timeout=2).getsockname()", "tuple[str, int]"),
])
def test_socket_types(expr, ty):
    [v] = ok(f"import socket\nx = {expr}\n").globals
    assert str(v.type) == ty


@pytest.mark.parametrize("src,msg", [
    ("socket.socket().send(5)", "socket.send() argument 'data' must be bytes (or str), not int"),
    ("socket.socket().connect('host')", "socket.connect() argument 'address' must be tuple[str, int], not str"),
    ("socket.socket().recv(10, bufsize=5)", "socket.recv() got multiple values for argument 'bufsize'"),
])
def test_socket_errors(src, msg):
    assert err(f"import socket\n{src}\n").message == msg


def test_socket_annotations():
    ok("""
        import socket
        from socket import socket as Sock
        def handle(conn: socket.socket, other: Sock) -> bytes:
            with conn:
                return conn.recv(10)
    """)


# ---- dunder methods -------------------------------------------------------------------

VEC = """
class Vec:
    x: float
    def __add__(self, other: Vec) -> Vec:
        return Vec(self.x + other.x)
    def __rmul__(self, k: float) -> Vec:
        return Vec(self.x * k)
    def __lt__(self, other: Vec) -> bool:
        return self.x < other.x
    def __eq__(self, other: Vec) -> bool:
        return self.x == other.x
    def __len__(self) -> int:
        return 1
    def __getitem__(self, i: int) -> float:
        return self.x
"""


@pytest.mark.parametrize("expr,ty", [
    ("Vec(1) + Vec(2)", "Vec"),
    ("2 * Vec(1)", "Vec"),
    ("Vec(1) < Vec(2)", "bool"),
    ("Vec(1) > Vec(2)", "bool"),
    ("Vec(1) != Vec(2)", "bool"),
    ("sorted([Vec(2), Vec(1)])", "list[Vec]"),
    ("len(Vec(1))", "int"),
    ("Vec(1)[0]", "float"),
    ("Vec(1) if Vec(1) else Vec(2)", "Vec"),
])
def test_dunder_types(expr, ty):
    info = ok(VEC + f"z = {expr}\n")
    assert str(info.globals[-1].type) == ty


@pytest.mark.parametrize("src,msg", [
    ("z = Vec(1) - Vec(2)", "unsupported operand types for -: Vec and Vec (define __sub__ on Vec)"),
    ("z = Vec(1) <= Vec(2)", "'<=' isn't supported between Vec and Vec (define __le__ on Vec)"),
    ("z = -Vec(1)", "bad operand type for unary -: Vec (define __neg__ on Vec)"),
    ("v = Vec(1)\nv[0] = 2.0", "Vec doesn't support item assignment (define __setitem__)"),
    ("class A:\n    def __len__(self) -> str:\n        return ''", "__len__ must return int, not str"),
    ("class A:\n    def __eq__(self) -> bool:\n        return True", "__eq__ takes (self, other)"),
    ("class A:\n    def __iter__(self) -> int:\n        return 1", "__iter__ must return something iterable (like a list), not int"),
    ("class A:\n    def __hash__(self) -> int:\n        return 1", "a class with __hash__ also needs __eq__ (equal objects must hash the same)"),
    ("class A:\n    x: int\nz = {A(1): 1}", "dict keys must be int, float, str, bool, or a tuple of those; not A"),
])
def test_dunder_errors(src, msg):
    assert err(VEC + src).message == msg


def test_decorator_errors():
    assert err(
        "from dataclasses import dataclass\n@dataclass(frozen=True)\nclass V:\n    x: int\nv = V(1)\nv.x = 2\n"
    ).message == "V is a frozen dataclass; its field 'x' can't be changed (make a changed copy: dataclasses.replace(obj, x=...))"
    assert err(
        "from dataclasses import dataclass\n@dataclass(frozen=True)\nclass V:\n    x: int\nv = V(1)\nv.x += 2\n"
    ).message == "V is a frozen dataclass; its field 'x' can't be changed (make a changed copy: dataclasses.replace(obj, x=...))"
    assert err(
        "class T:\n    c_: float\n    @property\n    def c(self) -> float:\n        return self.c_\nt = T(1.0)\nt.c = 2.0\n"
    ).message == "property 'c' of T is read-only (add an @c.setter)"
    assert err(
        "class P:\n    x: int\n    def m(self) -> int:\n        return 1\nP.m()\n"
    ).message == "P.m() needs an instance: only @staticmethod and @classmethod methods can be called on the class"
    assert err(
        "from functools import cache\n@cache\ndef f(xs: list[int]) -> int:\n    return 0\n"
    ).message == "functools.cache needs hashable arguments; 'xs' is a list[int]"
    assert err("@staticmethod\ndef f() -> int:\n    return 1\n").message == (
        "@staticmethod only makes sense on a method inside a class"
    )
    assert err("def d(c: int) -> int:\n    return c\n@d\nclass P:\n    x: int\n").message == (
        "only @dataclass, @value and @functools.total_ordering can decorate a class (for now)"
    )
    assert err(
        "class P:\n    x: int\n    @property\n    def y(self):\n        pass\n"
    ).message == "a @property takes only self and returns a value"
    assert err(
        "from dataclasses import dataclass\n@dataclass(slots=True)\nclass P:\n    x: int\n"
    ).message == "@dataclass(slots=...) isn't supported"


def test_decorator_types():
    ok(
        "def shout(f: Callable[[str], str]) -> Callable[[str], str]:\n"
        "    return lambda s: f(s).upper()\n"
        "@shout\ndef hi(name: str) -> str:\n    return 'hi ' + name\n"
        "x = hi('a')\n"
    )
    assert err(
        "def shout(f: Callable[[str], str]) -> Callable[[str], str]:\n"
        "    return f\n"
        "@shout\ndef hi(n: int) -> str:\n    return 'hi'\n"
    ).message == "argument 'f' of shout() must be (str) -> str, not (int) -> str"


def test_looping_over_tuples():
    info = ok(
        "class A:\n    n: int\nclass B(A):\n    pass\nclass C(A):\n    pass\n"
        "for i in (1, 2):\n    pass\n"
        "for f in (1, 2.5):\n    pass\n"
        "for o in (B(1), C(2)):\n    pass\n"
        "for m in (1, None):\n    pass\n"
        "for s in ('a', 'b'):\n    pass\n"
        "total = sum((1, 2.5))\n"
    )
    assert set(variables(info)) == {"i: int", "f: float", "o: A", "m: int?", "s: str", "total: float"}
    assert err("for x in (1, 'a'):\n    pass\n").message == (
        "can't loop over tuple[int, str] (its items have different types, so there's no single type for the loop variable)"
    )
    assert err("x = sorted((1, 'a'))\n").message.startswith("sorted() argument must be something you can loop over")


COLLECTIONS = "from collections import Counter, defaultdict, deque\n"


def test_collections_types():
    info = ok(
        COLLECTIONS
        + "c = Counter('abc')\nd = deque([1, 2])\ng: defaultdict[str, list[int]] = defaultdict(list)\n"
        + "e = deque[str]()\nm = c.most_common(1)\nw = Counter({'a': 2})\np = dict([('a', 1)])\n"
    )
    assert set(variables(info)) >= {
        "c: Counter[str]", "d: deque[int]", "g: defaultdict[str, list[int]]", "e: deque[str]",
        "m: list[tuple[str, int]]", "w: Counter[str]", "p: dict[str, int]",
    }


def test_collections_errors():
    assert err(COLLECTIONS + "d = defaultdict(list)\n").message.startswith("a defaultdict needs its key and value types")
    assert err(COLLECTIONS + "d: defaultdict[str, int] = defaultdict(list)\n").message == (
        "this defaultdict holds int, but list() doesn't make one"
    )
    assert err(COLLECTIONS + "c = Counter()\n").message.startswith("a Counter needs to know what it counts")
    assert err(COLLECTIONS + "d = deque[int](['a'])\n").message == "this deque holds int, not str"
    assert err(COLLECTIONS + "d = deque([1])\nx = d[0:1]\n").message == "deque[int] can't be sliced"
    assert err(COLLECTIONS + "c: Counter[str, int] = Counter()\n").message == (
        "Counter takes 1 type argument, e.g. Counter[str]"
    )
    assert err(COLLECTIONS + "c = Counter('ab') + {'a': 1}\n").message == (
        "unsupported operand types for +: Counter[str] and dict[str, int]"
    )
    assert err("x = dict([1, 2])\n").message == "dict() needs a dict or (key, value) pairs, not list[int]"


def test_regex_types():
    info = ok(
        "import re\n"
        "m = re.match(r'(\\d+)-(\\d+)?', 'x')\n"
        "if m:\n    a = m.group(1)\n    b = m.group(2)\n    c = m.groups()\n    d = m['n' if False else 'n'] if False else ''\n"
        "p = re.compile(r'(?P<k>\\w+)=(?P<v>\\w+)')\n"
        "pairs = p.findall('a=1')\n"
        "words = re.findall(r'\\w+', 'a b')\n"
        "parts = re.split(r'(-)|(\\+)', 'a-b')\n"
        "unknown = re.compile('(' + 'x' + ')')\n"
    )
    names = set(variables(info))
    assert {"a: str", "b: str?", "c: tuple[str, str?]", "pairs: list[tuple[str, str]]", "words: list[str]",
            "parts: list[str?]", "m: re.Match?", "p: re.Pattern", "unknown: re.Pattern"} <= names


def test_regex_errors():
    assert err("import re\nre.search('(a', 'x')\n").message == (
        "invalid regular expression: missing ), unterminated subpattern at position 0"
    )
    assert err("import re\nm = re.match('(a)', 'a')\nif m:\n    x = m.group(2)\n").message == (
        "the pattern has no group 2 (it has 1 group)"
    )
    assert err("import re\nm = re.match('(?P<a>a)', 'a')\nif m:\n    x = m.group('b')\n").message == (
        "the pattern has no group named 'b'"
    )
    assert err("import re\nm = re.match('(a)?', 'a')\nif m:\n    x = m.group(1).upper()\n").message.startswith(
        "str? might be None"
    )
    assert err("import re\nx = re.sub('a', 1, 'abc')\n").message == (
        "re.sub() replacement must be a str or a function, not int"
    )
    assert err("import re\nx = re.search('a', 'b', flag=1)\n").message == (
        "re.search() got an unexpected keyword argument 'flag'"
    )


def test_subprocess_types():
    info = ok(
        "import subprocess\n"
        "t = subprocess.run(['ls'], capture_output=True, text=True).stdout\n"
        "b = subprocess.run(['ls'], stdout=subprocess.PIPE).stdout\n"
        "o = subprocess.check_output(['ls'])\n"
        "p = subprocess.Popen(['cat'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)\n"
        "out, err = p.communicate('x')\n"
        "code = p.poll()\n"
    )
    assert {"t: str", "b: bytes", "o: bytes", "out: str", "err: str?", "code: int?", "p: subprocess.Popen"} <= set(
        variables(info)
    )


def test_subprocess_errors():
    assert err("import subprocess\nr = subprocess.run(['ls'])\nx = r.stdout\n").message == (
        "subprocess.CompletedProcess.stdout: stdout wasn't captured, so it's None: pass capture_output=True to read it"
    )
    assert err("import subprocess\nt = True\nr = subprocess.run(['ls'], text=t)\n").message == (
        "text= must be True or False written out (it decides the result's type)"
    )
    assert err("import subprocess\nr = subprocess.run(['ls'], stdout=subprocess.STDOUT)\n").message == (
        "only stderr can be subprocess.STDOUT"
    )
    assert err("import subprocess\nr = subprocess.check_output(['ls'], stdout=subprocess.PIPE)\n").message == (
        "subprocess.check_output() got an unexpected keyword argument 'stdout'"
    )


def test_pathlib_types():
    info = ok(
        "from pathlib import Path\n"
        "p = Path('a') / 'b'\nq = 'x' / Path('y')\nn = p.name\nkids = p.iterdir()\nsize = p.stat().st_size\n"
        "here = Path.cwd()\nf = p.open('rb')\ns = {p, q}\n"
    )
    assert {"p: Path", "q: Path", "n: str", "kids: list[Path]", "size: int", "here: Path", "f: BinaryIO",
            "s: set[Path]"} <= set(variables(info))
    assert err("from pathlib import Path\nx = Path('a') / 3\n").message == (
        "unsupported operand types for /: Path and int"
    )
    assert err("from pathlib import Path\nx = Path('a').rename(3)\n").message == (
        "Path.rename() argument 'target' must be a str or Path, not int"
    )


def test_shutil_tempfile_types():
    info = ok(
        "import shutil\nimport tempfile\nfrom pathlib import Path\n"
        "w = shutil.which('sh')\nc = shutil.copy('a', Path('b'))\nu = shutil.disk_usage('/').free\n"
        "t = tempfile.TemporaryDirectory()\nn = t.name\n"
        "with tempfile.TemporaryDirectory() as d:\n    inside = d\n"
    )
    assert {"w: str?", "c: str", "u: int", "t: TemporaryDirectory", "n: str", "inside: str"} <= set(variables(info))
    assert err("import shutil\nshutil.rmtree(3)\n").message == (
        "shutil.rmtree() argument 'path' must be a str or Path, not int"
    )


def test_mimetypes_types():
    info = ok(
        "import mimetypes\nfrom pathlib import Path\n"
        "t, enc = mimetypes.guess_type('a.html')\ng = mimetypes.guess_type(Path('a.tgz'), strict=False)\n"
        "e = mimetypes.guess_extension('text/html')\nexts = mimetypes.guess_all_extensions('text/html', False)\n"
        "m = mimetypes.types_map\nmimetypes.add_type('text/x-sd', '.sd')\n"
    )
    assert {"t: str?", "enc: str?", "g: tuple[str?, str?]", "e: str?", "exts: list[str]",
            "m: dict[str, str]"} <= set(variables(info))
    assert err("import mimetypes\nmimetypes.guess_type(3)\n").message == (
        "mimetypes.guess_type() argument 'url' must be a str or Path, not int"
    )
    assert err("import mimetypes\nmimetypes.guess_extension(b'text/html')\n").message == (
        "mimetypes.guess_extension() argument 'type' must be str, not bytes"
    )
    assert err("import mimetypes\nmimetypes.add_type('text/x-sd')\n").message == (
        "mimetypes.add_type() is missing argument 'ext'"
    )


def test_datetime_types():
    info = ok(
        "from datetime import date, datetime, timedelta, timezone\n"
        "d = date(2024, 1, 1)\ngap = date(2024, 2, 1) - d\nlater = d + timedelta(days=3)\n"
        "ratio = timedelta(hours=1) / timedelta(minutes=5)\nnow = datetime.now(timezone.utc)\n"
        "tz = now.tzinfo\ntotal = sum([gap, gap], timedelta())\nwhen = datetime.strptime('2024', '%Y')\n"
    )
    assert {"d: date", "gap: timedelta", "later: date", "ratio: float", "now: datetime", "tz: timezone?",
            "total: timedelta", "when: datetime"} <= set(variables(info))
    assert err("from datetime import date\nx = date(2024, 1, 1) + 1\n").message == (
        "unsupported operand types for +: date and int"
    )
    assert err("from datetime import timedelta\nx = sum([timedelta()])\n").message == (
        "sum() of timedeltas needs a starting value: sum(items, timedelta())"
    )
    assert err("from datetime import datetime\nx = datetime.yesterday()\n").message == (
        "type object 'datetime' has no attribute 'yesterday'"
    )


def test_argparse_types():
    info = ok(
        "import argparse\nfrom pathlib import Path\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('files', nargs='+')\np.add_argument('--count', type=int, default=1)\n"
        "p.add_argument('--name')\np.add_argument('-v', '--verbose', action='store_true')\n"
        "p.add_argument('--out', type=Path, required=True)\np.add_argument('--tag', action='append')\n"
        "args = p.parse_args()\n"
        "files = args.files\ncount = args.count\nname = args.name\nverbose = args.verbose\nout = args.out\n"
        "tags = args.tag\n"
    )
    assert {"files: list[str]", "count: int", "name: str?", "verbose: bool", "out: Path", "tags: list[str]?"} <= set(
        variables(info)
    )


def test_argparse_errors():
    base = "import argparse\np = argparse.ArgumentParser()\np.add_argument('--count', type=int)\n"
    assert err(base + "a = p.parse_args()\nx = a.cuont\n").message == (
        "the parsed arguments have no 'cuont' (the parser's arguments are: count)"
    )
    assert err(base + "a = p.parse_args()\nx = a.count + 1\n").message == (
        "int? might be None; check it first, e.g. `if a.count is not None:`"
    )
    assert err(base + "p.add_argument('--n', type=complex)\n").message == (
        "type= must be int, float, str or Path (written out: it decides the parsed type)"
    )
    assert err(base + "p.add_argument('--k', type=int, default='x')\n").message == (
        "default= must be int for this argument, not str"
    )
    assert err(base + "p.add_argument('--count')\n").message == "'count' is already an argument of this parser"


def test_var_tuples():
    info = ok(
        "t = tuple([1, 2])\nu: tuple[str, ...] = ('a', 'b', 'c')\nfirst = t[0]\nrest = t[1:]\n"
        "e: tuple[int, ...] = ()\nboth = [t, (3, 4)]\n"
    )
    assert {"t: tuple[int, ...]", "u: tuple[str, ...]", "first: int", "rest: tuple[int, ...]", "e: tuple[int, ...]",
            "both: list[tuple[int, ...]]"} <= set(variables(info))
    assert err("x: tuple[..., int] = ()\n").message == "'...' only goes in tuple[T, ...] (a tuple of any length)"
    assert err("t: tuple[int, ...] = ('a',)\n").message == "'t' is declared as tuple[int, ...], but the value is tuple[str]"


SUBCOMMANDS = (
    "import argparse\np = argparse.ArgumentParser()\ns = p.add_subparsers(dest='command', required=True)\n"
    "a = s.add_parser('add')\na.add_argument('text')\nr = s.add_parser('rm')\nr.add_argument('n', type=int)\n"
    "args = p.parse_args()\n"
)


def test_argparse_subcommand_narrowing():
    info = ok(SUBCOMMANDS + "cmd = args.command\nif args.command == 'add':\n    t = args.text\nmaybe = args.text\n")
    assert {"cmd: str", "t: str", "maybe: str?"} <= set(variables(info))
    assert err(SUBCOMMANDS + "x = args.text.upper()\n").message == (
        "str? might be None; check it first, e.g. `if args.text is not None:`"
    )
    assert err(SUBCOMMANDS + "if args.command == 'rm':\n    x = args.text.upper()\n").message.startswith(
        "str? might be None"
    )


GEN = "from typing import Iterator\n"


def test_generator_types():
    info = ok(
        GEN + "def gen(n: int) -> Iterator[int]:\n    yield n\n"
        "g = gen(1)\nfirst = next(g)\nmaybe = next(g, None)\nfallback = next(g, 0)\nit = iter([1.5])\nall_of = list(g)\n"
    )
    assert {"g: Iterator[int]", "first: int", "maybe: int?", "fallback: int", "it: Iterator[float]",
            "all_of: list[int]"} <= set(variables(info))


def test_generator_errors():
    assert err("def gen():\n    yield 1\n").message == (
        "'gen' is a generator (it has 'yield'), so its return type is Iterator[T]: write `-> Iterator[int]` "
        "(with the type it yields)"
    )
    assert err(GEN + "def gen() -> Iterator[int]:\n    yield 'a'\n").message == "'gen' yields int, not str"
    assert err(GEN + "def gen() -> Iterator[int]:\n    yield from ['a']\n").message == (
        "'gen' yields int, but this gives str"
    )
    assert err(GEN + "def gen() -> Iterator[int]:\n    yield 1\n    return 5\n").message == (
        "a generator can only `return` without a value (to finish early)"
    )
    assert err(GEN + "def f():\n    def g() -> Iterator[int]:\n        yield 1\n").message == (
        "nested functions can't be generators yet; move it to the top level"
    )
    assert err("x = next([1, 2])\n").message == "next() needs an iterator (a generator, or iter(...)), not list[int]"
    assert err("yield 1\n").message == "'yield' outside a function"


def test_itertools_types():
    info = ok(
        "import itertools\nfrom itertools import count, permutations, zip_longest, product, groupby, tee\n"
        "c = count(0.5)\np2 = permutations([1, 2, 3], 2)\npn = permutations('ab')\n"
        "z = zip_longest([1], ['a'])\nzf = zip_longest([1], [2], fillvalue=0)\npr = product('ab', repeat=2)\n"
        "g = groupby([1, 2], key=lambda x: x > 1)\nt = tee([1], 3)\nf = itertools.chain.from_iterable([[1]])\n"
    )
    assert {"c: Iterator[float]", "p2: Iterator[tuple[int, int]]", "pn: Iterator[tuple[str, ...]]",
            "z: Iterator[tuple[int?, str?]]", "zf: Iterator[tuple[int, int]]", "pr: Iterator[tuple[str, str]]",
            "g: Iterator[tuple[bool, Iterator[int]]]", "t: tuple[Iterator[int], Iterator[int], Iterator[int]]",
            "f: Iterator[int]"} <= set(variables(info))
    assert err("from itertools import starmap\nx = starmap(abs, [1, 2])\n").message == (
        "starmap() needs an iterable of tuples (the arguments), not of int"
    )
    assert err("from itertools import tee\nn = 2\nx = tee([1], n)\n").message == (
        "tee()'s n must be a number written out (it decides how many you get)"
    )


def test_star_import():
    info = ok("from itertools import *\nfrom math import *\nx = list(islice(count(), 2))\ny = sqrt(4.0)\n")
    assert {"x: list[int]", "y: float"} <= set(variables(info))


def test_zoneinfo_types():
    info = ok(
        "import datetime\nfrom zoneinfo import ZoneInfo, available_timezones\n"
        "tz = ZoneInfo('Europe/Paris')\nnow = datetime.datetime.now(tz)\nnames = available_timezones()\n"
        "def at(z: datetime.tzinfo) -> datetime.datetime:\n    return datetime.datetime(2024, 1, 1, tzinfo=z)\n"
        "d = at(tz)\n"
    )
    assert {"tz: timezone", "now: datetime", "names: set[str]", "d: datetime"} <= set(variables(info))


def test_hashlib_types_and_errors():
    info = ok("import hashlib\nimport hmac\nh = hashlib.sha256(b'x')\nd = h.hexdigest()\nm = hmac.new(b'k', b'm', hashlib.sha1)\n"
              "raw = m.digest()\nsame = hmac.compare_digest(raw, raw)\n")
    assert {"h: hash", "d: str", "m: HMAC", "raw: bytes", "same: bool"} <= set(variables(info))
    assert err("import hashlib\nh = hashlib.sha256('text')\n").message == (
        "hashlib.sha256() data: strings must be encoded before hashing; use s.encode()"
    )
    assert err("import hmac\nm = hmac.new(b'k', b'm')\n").message == (
        "hmac.new() needs digestmod= (like hashlib.sha256 or 'sha256')"
    )
    assert err("import hmac\nx = hmac.compare_digest('a', b'a')\n").message == (
        "compare_digest() compares two str or two bytes, not str and bytes"
    )


FUTURES = "from concurrent.futures import ThreadPoolExecutor, Future\n"


def test_futures_types_and_races():
    info = ok(FUTURES + "def sq(n: int) -> int:\n    return n * n\n"
              "with ThreadPoolExecutor() as pool:\n    f = pool.submit(sq, 2)\n    r = f.result()\n"
              "    squares = list(pool.map(sq, [1, 2]))\n    g: Future[int] = f\n")
    assert {"f: Future[int]", "r: int", "squares: list[int]", "g: Future[int]"} <= set(variables(info))
    assert err(FUTURES + "def sq(n: int) -> int:\n    return n\nwith ThreadPoolExecutor() as pool:\n    f = pool.submit(sq)\n"
               ).message == "the function takes (int), but it's given (none)"


def test_logging_types_and_errors():
    info = ok("import logging\nlog = logging.getLogger('x')\nlevel = log.getEffectiveLevel()\nname = log.name\n"
              "h = logging.StreamHandler()\nlog.info('%d items', 3)\n")
    assert {"log: Logger", "level: int", "name: str", "h: Handler"} <= set(variables(info))
    assert err("import logging\nlogging.basicConfig(levle=10)\n").message == (
        "basicConfig() got an unexpected keyword argument 'levle'"
    )
    assert err("import logging\nlogging.getLogger().setLevel(1.5)\n").message == (
        "setLevel() must be a level: logging.INFO (an int) or 'INFO', not float"
    )
    assert err("import logging\nlogging.info()\n").message == "logging.info() needs a message"


def test_csv_types_and_errors():
    info = ok("import csv\nrows = list(csv.reader(['a,b']))\nw = csv.DictReader(['a', '1'])\nnames = w.fieldnames\n"
              "first = next(w)\n")
    assert {"rows: list[list[str]]", "w: csv.DictReader", "names: list[str]", "first: dict[str, str]"} <= set(variables(info))
    assert err("import csv\nr = csv.reader([1, 2])\n").message == (
        "csv.reader() reads lines of text (a file opened in text mode, or strs), not int"
    )
    assert err("import csv\nr = csv.DictReader(['a'], restkey='x')\n").message.startswith("restkey isn't supported")
    assert err("import csv\nr = csv.reader(['a'], delimeter=';')\n").message == (
        "csv.reader() got an unexpected keyword argument 'delimeter'"
    )


def test_urllib_types_and_errors():
    info = ok("import urllib.request\nfrom urllib.parse import urlparse\nfrom urllib.error import HTTPError\n"
              "r = urllib.request.urlopen('http://h/')\nbody = r.read()\nct = r.headers['Content-Type']\n"
              "p = urlparse('http://h:1/')\nport = p.port\n"
              "try:\n    urllib.request.urlopen('http://h/')\nexcept HTTPError as e:\n    page = e.read()\n    code = e.code\n")
    assert {"body: bytes", "ct: str?", "port: int?", "page: bytes", "code: int"} <= set(variables(info))
    assert err("import urllib.request\nr = urllib.request.urlopen('http://h/', data='x=1')\n").message == (
        "urlopen() data must be bytes, not str; use s.encode() (or urlencode(fields).encode() for a form)"
    )
    assert err("import urllib.request\nr = urllib.request.urlopen(3)\n").message == (
        "urlopen() needs a URL (str) or a Request, not int"
    )
    assert err("from urllib.parse import urlencode\nq = urlencode('a=1')\n").message == (
        "urlencode() needs a dict or a list of (key, value) pairs, not str"
    )



def test_http_client_types_and_errors():
    h = "import http.client\nc = http.client.HTTPConnection('h')\n"
    info = ok(h + "import ssl\nctx = ssl.create_default_context()\nctx.check_hostname = False\n"
              "ctx.verify_mode = ssl.CERT_NONE\ns = http.client.HTTPSConnection('h', 443, context=ctx)\n"
              "s.request('GET', '/', 'body', {'A': 'b'})\nr: http.client.HTTPResponse = s.getresponse()\n"
              "m: http.client.HTTPMessage = r.msg\nv = r.version\ndone = r.isclosed()\nport = s.port\n")
    assert {"v: int", "done: bool", "port: int"} <= set(variables(info))
    assert err(h + "c.request('POST', '/', body=3)\n").message == "request() body must be bytes or str, not int"
    assert err(h + "c.request('GET', '/', headers={'a': 1})\n").message == (
        "request() headers must be dict[str, str], not dict[str, int]"
    )
    assert err("import ssl\nx = ssl.create_default_context()\nx.verify_mode = 'no'\n").message == (
        "field 'verify_mode' is int, can't assign str"
    )
    assert err("import urllib.request\nr = urllib.request.urlopen('https://h/', context=3)\n").message == (
        "urlopen() context must be SSLContext, not int"
    )

def test_email_utils_types_and_errors():
    info = ok("import email.utils\nfrom email.utils import parsedate_tz, parsedate\nfrom datetime import datetime\n"
              "a = email.utils.formatdate(5)\nb = email.utils.format_datetime(datetime.now(), usegmt=False)\n"
              "c = email.utils.parsedate_to_datetime(a)\nd = parsedate_tz(a)\ne = parsedate(a)\n")
    assert {"a: str", "b: str", "c: datetime", "d: tuple[int, int, int, int, int, int, int, int, int, int]?",
            "e: tuple[int, int, int, int, int, int, int, int, int]?"} <= set(variables(info))
    assert err("import email.utils\nemail.utils.formatdate('now')\n").message == (
        "email.utils.formatdate() argument 'timeval' must be float?, not str"
    )
    assert err("from email.utils import format_datetime\nformat_datetime('x')\n").message == (
        "email.utils.format_datetime() argument 'dt' must be datetime, not str"
    )
    assert err("import email.utils\nemail.utils.parsedate_to_datetime(None)\n").message == (
        "email.utils.parsedate_to_datetime() argument 'data' must be str, not None"
    )
    assert err("import email\nemail.utils.mktime_tz(1)\n").message == "module 'email.utils' has no member 'mktime_tz'"

MATCH_HEADER = """
from dataclasses import dataclass

@dataclass
class Point:
    x: int
    y: int

class Animal:
    name: str
    def __init__(self, name: str):
        self.name = name

class Dog(Animal):
    def __init__(self, name: str):
        super().__init__(name)
"""


def test_match_bindings_and_narrowing():
    info = ok(MATCH_HEADER + """
def f(p: Point, xs: list[str], n: int?, t: tuple[int, str], a: Animal) -> None:
    match p:
        case Point(x, y=0) as q:
            pass
    match xs:
        case [first, *rest]:
            pass
    match n:
        case None:
            pass
        case m:
            pass
    match t:
        case (i, s):
            pass
    match a:
        case Dog() as d:
            pass
""")
    assert {"x: int", "q: Point", "first: str", "rest: list[str]", "m: int", "i: int", "s: str", "d: Dog"} <= set(
        variables(info, "f"))


@pytest.mark.parametrize("body, message", [
    ("match n:\n    case x:\n        pass\n    case 1:\n        pass\n",
     "name capture 'x' makes remaining patterns unreachable"),
    ("match n:\n    case _:\n        pass\n    case 1:\n        pass\n", "wildcard makes remaining patterns unreachable"),
    ("match n:\n    case 'one':\n        pass\n", "this pattern can never match: an int is never equal to a str"),
    ("match n:\n    case None:\n        pass\n", "this pattern can never match: an int is never None"),
    ("match n:\n    case True:\n        pass\n",
     "this pattern can never match: `case True:` only matches a bool (it compares with `is`), not an int"),
    ("match s:\n    case [c, *_]:\n        pass\n",
     "this pattern can never match: sequence patterns don't match a str (as in Python); compare it, or use a guard"),
    ("match t:\n    case (a, b, c):\n        pass\n",
     "this pattern can never match: a tuple[int, str] has 2 items, not 3"),
    ("match t:\n    case (a, *rest, b, c):\n        pass\n",
     "this pattern can never match: a tuple[int, str] has 2 items, fewer than 3"),
    ("match t:\n    case (a, *rest, b):\n        pass\n", "*rest is always empty here; leave it out"),
    ("match xs:\n    case [x, x]:\n        pass\n", "multiple assignments to name 'x' in pattern"),
    ("match xs:\n    case [x] | [y]:\n        pass\n", "alternative patterns bind different names"),
    ("match xs:\n    case {'a': 1}:\n        pass\n", "this pattern can never match: a list[int] isn't a dict"),
    ("match p:\n    case Dog():\n        pass\n", "this pattern can never match: a Point is never a Dog"),
    ("match a:\n    case Animal('rex'):\n        pass\n",
     "Animal() accepts no positional sub-patterns (it isn't a @dataclass); name the fields instead, like Animal(name=...)"),
    ("match p:\n    case Point(1, 2, 3):\n        pass\n", "Point() accepts 2 positional sub-patterns (3 given)"),
    ("match p:\n    case Point(1, x=2):\n        pass\n", "Point() got multiple sub-patterns for attribute 'x'"),
    ("match p:\n    case Point(z=2):\n        pass\n", "Point has no field 'z'"),
    ("match n:\n    case float():\n        pass\n", "this pattern can never match: an int is never a float"),
    ("match o:\n    case None:\n        pass\n    case int():\n        pass\n    case 3:\n        pass\n",
     "this case can never run: the cases before it already match everything"),
])
def test_match_errors(body, message):
    source = MATCH_HEADER + "def f(n: int, s: str, t: tuple[int, str], xs: list[int], p: Point, a: Animal, o: int?) -> None:\n"
    source += textwrap.indent(body, "    ")
    assert err(source).message == message


def test_match_exhaustive_assigns():
    ok("def f(n: int?) -> str:\n    match n:\n        case None:\n            return 'none'\n        case int():\n"
       "            return 'int'\n")
    assert err("def f(n: int) -> str:\n    match n:\n        case 1:\n            return 'one'\n").message == (
        "function 'f' can reach its end without returning a value (it's declared to return str)")


@pytest.mark.parametrize("line,msg", [
    ('f"{n:,x}"', "bad format spec ':,x' for an int: Cannot specify ',' with 'x'."),
    ('f"{n:.2}"', "bad format spec ':.2' for an int: Precision not allowed in integer format specifier"),
    ('f"{s:+}"', "bad format spec ':+' for a str: Sign not allowed in string format specifier"),
    ('f"{s:d}"', "bad format spec ':d' for a str: Unknown format code 'd' for object of type 'str'"),
    ('f"{n!r:d}"', "bad format spec ':d' for a str: Unknown format code 'd' for object of type 'str'"),
    ('f"{x:x}"', "bad format spec ':x' for a float: Unknown format code 'x' for object of type 'float'"),
    ('f"{x:10.}"', "bad format spec ':10.' for a float: Format specifier missing precision"),
    ('f"{xs:>10}"', "a format spec needs an int, float, str or date, not a list[int]; convert it first, e.g. with str()"),
    ('f"{xs:{n}}"', "a format spec needs an int, float, str or date, not a list[int]"),
    ('f"{m:>5}"', "int? might be None; check it first, e.g. `if m is not None:`"),
])
def test_format_spec_errors(line, msg):
    e = err(f"""
        n = 5
        x = 2.5
        s = "a"
        xs = [1]
        def f(m: int | None):
            print({line})
    """)
    assert msg in e.message


def test_format_specs_that_are_fine():
    ok("""
        from datetime import date
        n = 5
        w = 3
        print(f"{n:,} {n:_x} {2.5:z.1%} {'s':^{w}} {n:{w}.{w}} {date.today():%Y} {n:} {[1]:} {n!r:>5}")
    """)


@pytest.mark.parametrize("line,msg", [
    ('"{} {}".format(1)', "the format string needs at least 2 arguments, but 1 was given"),
    ('"{2}".format(1, 2)', "the format string needs at least 3 arguments, but 2 were given"),
    ('"{name}".format(1)', "the format string uses {name}, but there's no keyword argument 'name'"),
    ('"{}{0}".format(1)', "bad format string: cannot switch from automatic field numbering to manual field specification"),
    ('"{0}{}".format(1)', "bad format string: cannot switch from manual field specification to automatic field numbering"),
    ('"{".format(1)', "bad format string: Single '{' encountered in format string"),
    ('"{!x}".format(1)', "bad format string: Unknown conversion specifier x"),
    ('"{:,x}".format(1)', "bad format spec ':,x' for an int: Cannot specify ',' with 'x'."),
    ('"{:>5}".format(xs)', "a format spec needs an int, float, str or date, not a list[int]"),
    ('"{0.nope}".format(xs)', "list[int] has no attribute 'nope'"),
    ('"{0[k]}".format(xs)', "list index must be int"),
    ('format(xs, ">5")', "a format spec needs an int, float, str or date, not a list[int]"),
    ('format(1, ".2")', "bad format spec ':.2' for an int: Precision not allowed in integer format specifier"),
    ('format(1, 2)', "format() argument must be str, not int"),
    ('format(m, "5")', "int? might be None; check it first"),
])
def test_str_format_errors(line, msg):
    e = err(f"""
        xs = [1]
        def f(m: int | None):
            print({line})
    """)
    assert msg in e.message


def test_str_format_that_is_fine():
    ok("""
        xs = [1]
        fmt = "{}"
        print("{0} {x!r:>5} {0[0]:{w}}".format(xs, x="a", w=3), fmt.format(xs, 2), format(xs), format(xs, ""))
    """)


VALUE_HEADER = """
from seadash import value
import threading
class Customer:
    n: int
@value
class P:
    x: int
    def move(self):
        self.x += 1
@value
class T:
    members: list[str]
"""


@pytest.mark.parametrize("field,msg", [
    ("customer: Customer", "fields of a @value class must be values, but 'customer' is Customer, an ordinary class "
                           "(a shared reference). Copying Order would share it: store an id instead, remove @value to "
                           "make Order an ordinary class, or make Customer a @value class"),
    ("names: list[Customer]", "but 'names' holds Customer, an ordinary class"),
    ("lock: threading.Lock", "but 'lock' is a Lock, a thread-safe object shared by reference"),
    ("callback: (int) -> int", "but 'callback' is a function"),
    ("counts: dict[str, list[Customer]]", "but 'counts' holds Customer"),
])
def test_value_class_fields_must_be_values(field, msg):
    e = err(VALUE_HEADER + f"@value\nclass Order:\n    {field}\n")
    assert msg in e.message


@pytest.mark.parametrize("body,msg", [
    ("p = ps[0]\n    p.x += 1", "this changes 'p', a copy of ps[0], and then never uses it. Write it back (`ps[0] = p`), "
                               "or change ps[0] in place"),
    ("for p in ps:\n        p.x += 1", "this changes 'p', a copy of an item of ps (looping over @value classes gives "
                                       "copies), and then never uses it. Loop over the indexes and change ps[i] instead"),
    ("for p in ps:\n        p.move()", "a copy of an item of ps"),
    ("xs = t.members\n    xs.append('a')", "this changes 'xs', a copy of t.members, and then never uses it. "
                                          "Write it back (`t.members = xs`)"),
    ("p = ps[0]\n    print(p)\n    p.x = 5", "this changes 'p', a copy of ps[0]"),
    ("p = ps[0]\n    p.x = 5\n    p = ps[1]\n    print(p)", "this changes 'p', a copy of ps[0]"),
    ("q.x = 3", "this changes 'q', the function's own copy of the caller's P, and then never uses it. Return it"),
])
def test_changing_a_copy_and_dropping_it(body, msg):
    e = err(VALUE_HEADER + f"def f(ps: list[P], t: T, q: P):\n    {body}\n")
    assert msg in e.message


def test_changing_a_copy_and_using_it():
    ok(VALUE_HEADER + """
def f(ps: list[P], t: T, q: P) -> P:
    p = ps[0]
    p.x += 1
    ps[0] = p
    for r in ps:
        r.move()
        print(r)
    for i in range(len(ps)):
        ps[i].x += 1
    xs = t.members
    xs.append("a")
    t.members = xs
    t.members.append("in place")
    print(t)
    fresh = P(1)
    fresh.x = 2
    q.x = 3
    return q
""")


FROZEN_HEADER = """
from dataclasses import dataclass, replace
from seadash import value
@value
class In:
    x: int
    def bump(self):
        self.x += 1
    def show(self) -> int:
        return self.x
@value
@dataclass(frozen=True)
class T:
    name: str
    tags: list[str]
    inner: In
    grid: list[list[int]]
    def rename(self, n: str) -> "T":
        return replace(self, name=n)
def fill(xs: list[str]):
    xs.append("filled")
"""


@pytest.mark.parametrize("body", [
    "t.tags.append('x')",
    "t.tags[0] = 'x'",
    "t.inner.x = 5",
    "t.inner.bump()",
    "for row in t.grid:\n        row.append(0)",
])
def test_frozen_value_classes_are_frozen_all_the_way_down(body):
    e = err(FROZEN_HEADER + f"def f(t: T):\n    {body}\n    print(t)\n")
    assert e.message.startswith(
        "T is a frozen @value class, so nothing in 't' can change, lists included (they're part of its value). "
        "Make a changed copy with dataclasses.replace(t, ...), or copy the list first"
    )


def test_frozen_value_classes_can_be_read_and_copied():
    ok(FROZEN_HEADER + """
def f(t: T) -> T:
    xs = t.tags
    xs.append("mine")
    fill(t.tags)
    print(t.inner.show(), len(t.tags), [len(r) for r in t.grid], xs)
    return replace(t.rename("n"), tags=xs)
""")


def test_a_frozen_class_can_set_its_fields_in_init():
    ok("""
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Temp:
            celsius: float
            def __init__(self, fahrenheit: float):
                self.celsius = (fahrenheit - 32) * 5 / 9
        print(Temp(212.0))
    """)


@pytest.mark.parametrize("line,msg", [
    ("replace(p, z=1)", "P has no field 'z'"),
    ("replace(p, x='a')", "field 'x' is int, not str"),
    ("replace(3, x=1)", "replace() needs a dataclass or @value class instance, not int"),
    ("replace(p, 1)", "replace() takes the object, then fields as keywords: replace(obj, x=1) (2 given)"),
    ("replace(c, n=1)", "replace() rebuilds an object from its fields, but C has its own __init__"),
])
def test_replace_errors(line, msg):
    e = err(f"""
        from dataclasses import dataclass, replace
        @dataclass(frozen=True)
        class P:
            x: int
        class C:
            n: int
            def __init__(self):
                self.n = 0
        p = P(1)
        c = C()
        q = {line}
    """)
    assert e.message == msg


@pytest.mark.parametrize("line,msg", [
    ('open("x.txt", closefd=False)', "Cannot use closefd=False with file name"),
    ('os.fdopen("x.txt")', "os.fdopen() argument must be a file descriptor (int), not str"),
    ('os.open("x.txt")', "missing argument 'flags'"),
    ('os.write(1, "text")', "must be bytes, not str"),
    ('open(3, "q")', "invalid mode: 'q'"),
])
def test_file_descriptor_errors(line, msg):
    e = err(f"import os\nx = {line}\n")
    assert msg in e.message


@pytest.mark.parametrize("line,msg", [
    ('"ab".translate({"a": "b"})', "translate() takes a table from str.maketrans() (a dict[int, str | int | None]), not dict[str, str]"),
    ('"{a}".format_map({1: "x"})', "format_map() takes a dict with str keys, not dict[int, str]"),
    ('str.maketrans({1: 2})', "str.maketrans() with one argument takes a dict[str, str], not dict[int, int]"),
    ('"ab".ljust("3")', "argument 'width' must be int, not str"),
    ('"ab".split(maxsplit="1")', "argument 'maxsplit' must be int, not str"),
    ('b"ab".find("a")', "argument 'sub' must be bytes, not str"),
    ('bytes.fromhex(b"ab")', "must be str, not bytes"),
])
def test_string_method_errors(line, msg):
    e = err(f"x = {line}\n")
    assert msg in e.message


@pytest.mark.parametrize("src,msg", [
    ("x = int(3.5, 10)", "int() can't convert non-string with explicit base"),
    ('x = int("1", "2")', "int() base must be an int, not str"),
    ('x = int("1", 2, base=2)', "int() got multiple values for argument 'base'"),
    ("x = bin(1.5)", "'float' object cannot be interpreted as an integer"),
    ('x = divmod("a", 1)', "divmod() arguments must be numbers, not str"),
    ("x = (1.5).bit_length()", "float has no method 'bit_length'"),
    ('x = (5).to_bytes(2, byteorder=1)', "int.to_bytes() argument 'byteorder' must be str, not int"),
    ('x = int.from_bytes("ab")', "int.from_bytes() argument 'bytes' must be bytes, not str"),
    ("x = min(1, 2, default=0)", "Cannot specify a default for min() with multiple positional arguments"),
    ('x = max([1], default="x")', "max() default must be int (or None), not str"),
    ('x = str("a", "utf-8")', "decoding str is not supported"),
    ('x = str(5, encoding="utf-8")', "str() with an encoding needs bytes to decode"),
    ('x = bytes(5, "utf-8")', "encoding without a string argument"),
    ('x = list(zip([1], strict=1))', "zip() argument 'strict' must be bool, not int"),
    ('x = list(map(lambda a: a, [1], [2]))', "this lambda takes 1 parameter, but (int, int) -> ? is expected here"),
    ('x = dict(a=1, b="x")', "dict() values must all be one type, not int and str"),
    ('x = dict({1: 2}, a=3)', "dict() keywords are str keys, but this has int keys"),
    ('d = {"a": 1}\nx = dict(d, b="x")', "dict() argument 'b' must be int, not str"),
    ("d = {1: 2}\nd.update(k=3)", "dict.update() keywords are str keys, but this dict has int keys"),
    ('d = {"a": 1}\nd.update([("b", "c")])', "dict.update() needs a dict[str, int] or (str, int) pairs, not list[tuple[str, str]]"),
    ('d = {"a": 1}\nd.update({}, {})', "update expected at most 1 argument, got 2"),
    ('x = dict.fromkeys(["a"])', "dict.fromkeys() without a value makes every value None: say what they'll hold later, "
                                 "e.g. `d: dict[str, int | None] = dict.fromkeys(names)`"),
    ('x = {1}.union(["a"])', "set.union() needs int items, not str"),
    ("x = {1}.isdisjoint(5)", "set.isdisjoint() argument must be something you can loop over, not int"),
    ("x = {1} < [1]", "'<' isn't supported between set[int] and list[int]"),
    ('x = (1, 2).index("a")', "a str can never be in a tuple[int, int]"),
    ("x = reversed(5)", "reversed() argument must be a list, tuple, str or range, not int"),
    ('f = open("x")\nx = f.seek("a")', "TextIO.seek() argument must be int, not str"),
    ("exit('bye')", "exit() argument must be int, not str"),
])
def test_builtin_function_and_method_errors(src, msg):
    assert err(src + "\n").message == msg


@pytest.mark.parametrize("src,msg", [
    ("class A:\n    v = [1]\n", "a class attribute must be a constant (a number, string, bytes, bool, None, or a tuple of "
                                 "those); for anything else, use a field (`v: T = ...`) or a module-level variable"),
    ("class A:\n    v = 1\n    v = 2\n", "class attribute 'v' is already defined"),
    ("class A:\n    x: int\n    x = 1\n", "'x' is a field of A; a class attribute can't reuse the name"),
    ("class A:\n    v = 1\n    def v(self):\n        pass\n", "'v' is both a class attribute and a method of A"),
    ("class A:\n    v = 1\nclass B(A):\n    v = 'x'\n", "B.v redefines v as str, but it's int in the base class"),
    ("class A:\n    v = 1\n    def f(self):\n        self.v = 2\n",
     "'v' is a class attribute of A, a constant, so it can't be set on an object (Python would give this object its own "
     "'v'). Make it a field to change it per object: `v: int = ...`"),
    ("class A:\n    v = 1\nA.v = 2\n", "A.v is a class attribute, a constant: it can't be changed"),
])
def test_class_attribute_errors(src, msg):
    assert err(src).message == msg


@pytest.mark.parametrize("src,msg", [
    ("def f(*args):\n    pass\n", "parameter 'args' needs a type annotation, e.g. `*args: str`"),
    ("def f(*args: int, x: int):\n    pass\nf(1, 2)\n", "f() is missing keyword-only argument 'x'"),
    ("def f(*args: int = 1):\n    pass\n", "*args can't have a default value"),
    ("def f(*args: int):\n    pass\nf(1, 'a')\n", "*args of f() takes int arguments, not str"),
    ("def f(*args: int):\n    pass\nf(args=(1,))\n", "f() got an unexpected keyword argument 'args'"),
    ("def f(a: int, *rest: int):\n    pass\nf()\n", "f() is missing argument 'a'"),
])
def test_star_args_errors(src, msg):
    with pytest.raises(CompileError) as info:  # (some are found by the parser)
        ok(src)
    assert info.value.message == msg


FUNCTOOLS = "from functools import reduce, cmp_to_key, total_ordering, cached_property, wraps\nfrom dataclasses import dataclass\n"


@pytest.mark.parametrize("src,msg", [
    ("x = reduce(lambda a, b: a + b, [1, 2], initial=0)\n", "reduce() takes no keyword arguments"),
    ("x = reduce(lambda a, b: a / b, [1, 2])\n",
     "reduce()'s function returns float, but the running value is int: give it an initial value (e.g. reduce(f, items, 0.0))"),
    ("x = sorted([1, 2], key=cmp_to_key(lambda a, b: 'x'))\n",
     "cmp_to_key()'s function must return a number (negative, zero or positive), not str"),
    ("def f(a: int, b: str) -> int:\n    return 0\nk = cmp_to_key(f)\n",
     "cmp_to_key() needs a function comparing two values of one type, like (int, int) -> int, not (int, str) -> int"),
    ("@total_ordering\nclass A:\n    n: int\n", "must define at least one ordering operation: < > <= >="),
    ("@total_ordering\nclass A:\n    n: int\n    def __lt__(self, other: 'A') -> int:\n        return 0\n",
     "@total_ordering needs __lt__(self, other) -> bool"),
    ("class A:\n    @cached_property\n    def p(self, x: int) -> int:\n        return x\n",
     "a @cached_property takes only self and returns a value"),
    ("@dataclass(frozen=True)\nclass A:\n    n: int\n    @cached_property\n    def p(self) -> int:\n        return 1\n",
     "@cached_property can't be used in a frozen class (it stores the value it computes)"),
    ("def d(f: Callable[[int], int]) -> Callable[[int], int]:\n    @wraps\n    def g(x: int) -> int:\n        return x\n"
     "    return g\n", "@wraps takes the function being wrapped: @wraps(f)"),
    ("def d(f: Callable[[int], int]) -> Callable[[int], int]:\n    @print\n    def g(x: int) -> int:\n        return x\n"
     "    return g\n", "decorators on nested functions aren't supported yet (except @functools.wraps)"),
])
def test_functools_errors(src, msg):
    assert err(FUNCTOOLS + src).message == msg


PARTIAL = "from functools import partial\ndef f(a: int, b: int, c: int = 3) -> int:\n    return a + b + c\n"


@pytest.mark.parametrize("src,msg", [
    ("p = partial(print, 'x')\n", "partial() of a built-in or library function isn't supported yet; use a lambda, "
                                   "e.g. `lambda x: print(x, end='')`"),
    ("p = partial(3)\n", "partial() needs a function or a class, not int"),
    ("p = partial(f, 1, 2, 3, 4)\n", "partial(f) gives 4 arguments, but the function takes 3"),
    ("p = partial(f, d=1)\n", "partial(f): the function has no parameter 'd'"),
    ("p = partial(f, 1, a=2)\n", "partial(f) got multiple values for argument 'a'"),
    ("p = partial(f, 'x')\n", "partial(f): argument 'a' must be int, not str"),
    ("p = partial(f, a=1)\nx = p(2)\n", "f() takes 0 positional arguments but 1 were given"),
    ("p = partial(f, 1)\nx = p(2, d=4)\n", "f() got an unexpected keyword argument 'd'"),
    ("g: Callable[[int], int] = lambda x: x\np = partial(g, x=1)\n",
     "partial() of a function value can't bind keywords (its parameters have no names here)"),
])
def test_partial_errors(src, msg):
    assert err("from typing import Callable\n" + PARTIAL + src).message == msg


def test_fnmatch_glob_types():
    info = ok("import fnmatch\nimport glob\nfrom pathlib import Path\n"
              "a = fnmatch.filter({'x'}, '*')\nb = glob.glob('*', root_dir=Path('.'), recursive=True)\n"
              "c = glob.iglob('**', include_hidden=True)\nd = fnmatch.translate('*')\ne = glob.translate('*', seps='/')\n")
    assert {"a: list[str]", "b: list[str]", "c: Iterator[str]", "d: str", "e: str"} <= set(variables(info))


@pytest.mark.parametrize("src,msg", [
    ("glob.glob('*', '.')\n", "glob.glob() takes 1 positional argument but 2 were given"),
    ("glob.glob('*', root_dir=3)\n", "glob.glob() argument 'root_dir' must be a str, Path or None, not int"),
    ("glob.iglob('*', recursive=1)\n", "glob.iglob() argument 'recursive' must be bool, not int"),
    ("glob.glob('*', hidden=True)\n", "glob.glob() got an unexpected keyword argument 'hidden'"),
    ("fnmatch.filter([1, 2], '*')\n", "fnmatch.filter() names must be strings, not int"),
    ("fnmatch.filter(3, '*')\n", "fnmatch.filter() argument 'names' must be something you can loop over, not int"),
    ("fnmatch.fnmatch('a', b'*')\n", "fnmatch.fnmatch() argument 'pat' must be str, not bytes"),
])
def test_fnmatch_glob_errors(src, msg):
    assert err("import fnmatch\nimport glob\n" + src).message == msg


def test_partial_types():
    info = ok("from typing import Callable\n" + PARTIAL + "p = partial(f, 1)\nq = partial(f, b=2, c=4)\nr = partial(f, 1, 2, 3)\n"
              "s: Callable[[int, int], int] = partial(f, 1)\n")
    assert {"p: (int, int) -> int", "q: (int) -> int", "r: () -> int", "s: (int, int) -> int"} <= set(variables(info))



HEAPQ_BISECT = ("import heapq\nfrom bisect import bisect, bisect_left, insort\nclass P:\n    n: int\n"
                "    def __init__(self, n: int):\n        self.n = n\n")


@pytest.mark.parametrize("src,msg", [
    ("h: list[dict[str, int]] = []\nheapq.heappush(h, {})\n", "heapq.heappush() can't compare dict[str, int] values"),
    ("ps: list[P] = []\nheapq.heapify(ps)\n", "heapq.heapify() can't compare P values (give P a __lt__ method)"),
    ("h: list[int] = []\nheapq.heappush(h, 'x')\n", "heapq.heappush() item must be int (the list's item type), not str"),
    ("h = (1, 2)\nheapq.heapify(h)\n", "heapq.heapify() needs a list, not tuple[int, int]"),
    ("h: list[int] = []\nheapq.heappush(h)\n", "heapq.heappush() takes exactly 2 arguments (1 given)"),
    ("x = heapq.nlargest('2', [1, 2])\n", "heapq.nlargest() n must be int, not str"),
    ("x = heapq.nlargest(2, [P(1)])\n", "heapq.nlargest() can't compare P values (give P a __lt__ method, or pass key=)"),
    ("x = heapq.nsmallest(2, [1], 3, 4)\n", "heapq.nsmallest() takes from 2 to 3 positional arguments but 4 were given"),
    ("x = heapq.merge([1], ['a'])\n", "heapq.merge() needs iterables of the same kind of item"),
    ("x = heapq.merge()\n", "heapq.merge() needs at least one iterable to merge"),
    ("x = heapq.merge([P(1)], reverse=True)\n", "heapq.merge() can't compare P values (give P a __lt__ method, or pass key=)"),
    ("x = bisect_left([(1, 'a')], (1, 'a'), key=lambda p: p[0])\n",
     "bisect.bisect_left() compares x with key(item), so it must be int, not tuple[int, str]"),
    ("xs = [(1, 'a')]\ninsort(xs, 1, key=lambda p: p[0])\n",
     "bisect.insort() x must be tuple[int, str] (the list's item type), not int"),
    ("x = bisect([P(1)], P(2), key=lambda p: {p.n: p})\n",
     "bisect.bisect() key must return something comparable, not dict[int, P]"),
    ("x = bisect([1], 1, 0, 1, None)\n", "bisect.bisect() takes at most 4 positional arguments (5 given)"),
    ("x = bisect([1], 1, hi=1.5)\n", "bisect.bisect() hi must be int?, not float"),
    ("x = bisect({1: 2}, 1)\n", "bisect.bisect() needs a list, not dict[int, int]"),
])
def test_heapq_bisect_errors(src, msg):
    assert err(HEAPQ_BISECT + src).message == msg


def test_heapq_bisect_types():
    info = ok(HEAPQ_BISECT + "h = [(2, 'b'), (1, 'a')]\nheapq.heapify(h)\na = heapq.heappop(h)\nb = heapq.nlargest(1, [1.5])\n"
              "c = heapq.merge([1], [2], key=lambda x: -x)\nd = bisect([P(1)], 1, key=lambda p: p.n)\n")
    assert {"a: tuple[int, str]", "b: list[float]", "c: Iterator[int]", "d: int"} <= set(variables(info))


COPY = "import copy\nimport threading\nfrom seadash import Synchronized\n"


@pytest.mark.parametrize("src,msg", [
    ("copy.copy(threading.Lock())\n",
     "copy.copy() can't copy a Lock (Python raises TypeError: cannot pickle it); make a new one, or share this one"),
    ("copy.deepcopy([threading.RLock()])\n",
     "copy.deepcopy() can't copy list[RLock]: it holds a RLock, which can't be copied (Python raises TypeError: "
     "cannot pickle it); make a new one, or share this one"),
    ("copy.copy(open('x'))\n",
     "copy.copy() can't copy a file (Python raises TypeError: cannot pickle it); open it again, or share this one"),
    ("class C:\n    n: int\n    lock: threading.Lock\nc = copy.deepcopy(C(1, threading.Lock()))\n",
     "copy.deepcopy() can't copy C: C.lock holds a Lock, which can't be copied (Python raises TypeError: cannot "
     "pickle it); make a new one, or share this one"),
    ("class B:\n    x: int\nclass D(B):\n    lock: threading.Lock\nbs = copy.deepcopy([B(1)])\n",
     "copy.deepcopy() can't copy list[B]: subclass D.lock holds a Lock, which can't be copied (Python raises "
     "TypeError: cannot pickle it); make a new one, or share this one"),
    ("class S(Synchronized):\n    n: int\ns = copy.copy(S(1))\n",
     "copy.copy() can't copy S: a Synchronized object's lock can't be copied; make a new one from its fields"),
    ("q: dict[str, threading.Event] = {}\nd = copy.deepcopy(q)\n",
     "copy.deepcopy() can't copy dict[str, Event]: it holds an Event, which can't be copied; share this one, or "
     "make a new one"),
    ("class C:\n    x: int\n    def __deepcopy__(self, memo: dict[int, int]) -> 'C':\n        return C(self.x)\n"
     "d = copy.deepcopy(C(1))\n",
     "copy.deepcopy() can't copy C, which defines __deepcopy__ (seadash doesn't call __deepcopy__ yet: it has no "
     "type for the memo); remove __deepcopy__ to copy every field, or copy it yourself"),
    ("class C:\n    x: int\n    def __copy__(self) -> int:\n        return 1\nd = copy.copy(C(1))\n",
     "C.__copy__ must take only self and return a C (def __copy__(self) -> C:), for copy.copy()"),
    ("d = copy.deepcopy([1], {})\n", "deepcopy()'s memo argument isn't supported yet: call deepcopy(x)"),
    ("d = copy.copy(ValueError('x'))\n", "copy.copy() can't copy ValueError: copying an exception isn't supported yet"),
])
def test_copy_errors(src, msg):
    assert err(COPY + src).message == msg


def test_copy_types():
    info = ok(COPY + "class C:\n    lock: threading.Lock\n"
              "class B:\n    x: int\n    def __copy__(self) -> 'B':\n        return B(self.x)\nclass D(B):\n    y: int = 0\n"
              "a = copy.copy(C(threading.Lock()))\n"  # (a shallow copy shares the lock, as in Python)
              "b = copy.copy(D(1))\n"  # (D's __copy__ is B's, which makes a B)
              "c = copy.deepcopy({'k': [(1, 'x')]})\n"
              "e: D | None = None\n"
              "d = copy.copy(e)\n")
    assert {"a: C", "b: B", "c: dict[str, list[tuple[int, str]]]", "d: B?"} <= set(variables(info))


CONTEXTLIB = ("from contextlib import contextmanager, suppress, closing, nullcontext, ExitStack\n"
              "from typing import Iterator\nimport threading\n")


@pytest.mark.parametrize("src,msg", [
    ("@contextmanager\ndef f() -> Iterator[int]:\n    return iter([1])\n",
     "'f' is a @contextmanager function, so it must `yield` (once) the value `with f(...) as x:` gives: the code "
     "before the yield runs when the with block starts, the code after it when the block ends"),
    ("@contextmanager\ndef f() -> Iterator[int]:\n    yield 1\n    yield 2\n",
     "this yield always runs after another one, but a @contextmanager function must yield exactly once (a second "
     "yield is Python's \"generator didn't stop\" error)"),
    ("@contextmanager\ndef f(a: bool) -> Iterator[int]:\n    if a:\n        yield 1\n    else:\n        yield 2\n"
     "    try:\n        pass\n    finally:\n        print()\n    yield 3\n",
     "this yield always runs after another one, but a @contextmanager function must yield exactly once (a second "
     "yield is Python's \"generator didn't stop\" error)"),
    ("@contextmanager\ndef f() -> Iterator[int]:\n    yield from [1]\n",
     "a @contextmanager function yields once: `yield from` isn't supported there"),
    ("from functools import cache\n@contextmanager\n@cache\ndef f() -> Iterator[int]:\n    yield 1\n",
     "@contextmanager can't be combined with other decorators yet"),
    ("@contextmanager\ndef f() -> int:\n    yield 1\n",
     "'f' is a generator (it has 'yield'), so its return type is Iterator[T]: write `-> Iterator[int]` (with the "
     "type it yields)"),
    ("@contextmanager\ndef f() -> Iterator[None]:\n    yield\nwith f() as x:\n    pass\n",
     "this ContextManager[None] gives None (a bare `yield`, or nothing to enter), so there's nothing to bind with 'as'"),
    ("@contextmanager\ndef f() -> Iterator[int]:\n    try:\n        pass\n    except ValueError:\n        yield 1\n",
     "a generator can't yield inside an 'except' block yet; set a flag there and yield after the try statement"),
    ("def g() -> Iterator[int]:\n    try:\n        pass\n    finally:\n        yield 1\n",
     "a generator can't yield inside a 'finally' block yet; set a flag there and yield after the try statement"),
    ("with suppress(ValueError, 3):\n    pass\n",
     "suppress() takes exception classes, like suppress(FileNotFoundError, KeyError)"),
    ("with suppress(exc=ValueError):\n    pass\n", "suppress() takes exception classes, not keyword arguments"),
    ("with closing(3):\n    pass\n", "closing() needs something with a close() method (taking no arguments), not int"),
    ("s = ExitStack()\ns.enter_context(3)\n",
     "int can't be used in a 'with' statement (it needs __enter__ and __exit__ methods)"),
    ("s = ExitStack()\ns.enter_context(threading.Lock())\n",
     "enter_context() can't hold Lock (a lock is held by a with statement's block)"),
    ("def g(a: int) -> None:\n    pass\ns = ExitStack()\ns.callback(g)\n",
     "callback(): the function takes 1 argument, but 0 are given"),
    ("def g(a: int) -> None:\n    pass\ns = ExitStack()\ns.callback(g, 'x')\n",
     "callback(): this argument must be int, not str"),
    ("s = ExitStack()\ns.callback(3)\n", "callback() needs a function to call, not int"),
    # suppress() may swallow the exception, so the code after the with block can be reached
    ("def g() -> int:\n    with suppress(ValueError):\n        return int('x')\n",
     "function 'g' can reach its end without returning a value (it's declared to return int)"),
    ("@contextmanager\ndef f() -> Iterator[None]:\n    try:\n        yield\n    except ValueError:\n        pass\n"
     "def g() -> int:\n    with f():\n        return 1\n",
     "function 'g' can reach its end without returning a value (it's declared to return int)"),
    ("def g() -> int:\n    with ExitStack() as s:\n        s.enter_context(suppress(KeyError))\n        return 1\n",
     "function 'g' can reach its end without returning a value (it's declared to return int)"),
    ("def h(s: ExitStack) -> None:\n    pass\ndef g() -> int:\n    with ExitStack() as s:\n        h(s)\n        return 1\n",
     "function 'g' can reach its end without returning a value (it's declared to return int)"),
])
def test_contextlib_errors(src, msg):
    assert err(CONTEXTLIB + src).message == msg


def test_contextlib_types():
    info = ok(CONTEXTLIB + "@contextmanager\ndef f(n: int) -> Iterator[str]:\n    try:\n        yield str(n)\n"
              "    except ValueError:\n        raise\n    finally:\n        print('done')\n"
              "def g() -> int:\n    with f(1) as s, nullcontext(2) as n, closing(open('x')):\n        return len(s) + n\n"
              "cm = f(2)\nwith cm as text:\n    pass\nstack = ExitStack()\nfirst = stack.enter_context(f(3))\n"
              "file = stack.enter_context(open('x'))\nkeep = stack.callback(lambda: print('bye'))\n"
              "nothing = nullcontext()\n"
              "def k() -> int:\n    with ExitStack() as s:\n        s.enter_context(f(1))\n"
              "        s.callback(lambda: print('x'))\n        s.pop_all().close()\n        return 1\n")
    assert {"cm: ContextManager[str]", "text: str", "stack: ExitStack", "first: str", "file: TextIO",
            "keep: () -> None", "nothing: ContextManager[None]"} <= set(variables(info))


# ---- enum -------------------------------------------------------------------

ENUM = ("from enum import Enum, IntEnum, StrEnum, Flag, IntFlag, auto, unique\n"
        "class Color(Enum):\n    RED = 1\n    GREEN = 2\n")
MATCH_ONE = "def f(c: Color) -> str:\n    match c:\n        case Color.RED:\n            return 'r'\n"


@pytest.mark.parametrize("src,msg", [
    ("print(Color.PURPLE)\n", "type object 'Color' has no attribute 'PURPLE'"),
    ("print(Color.REDD)\n", "type object 'Color' has no attribute 'REDD'; did you mean 'RED'?"),
    ("print(Color.RED.x)\n", "'Color' object has no attribute 'x' (an enum member has .name, .value and its class's methods)"),
    ("Color.RED = 5\n", "cannot reassign member 'RED': an enum's members are fixed when it's defined"),
    ("Color.PINK = 5\n", "cannot add a member 'PINK': an enum's members are fixed when it's defined"),
    ("c = Color.RED\nc.value = 3\n", "Color is an enum: its members can't be changed (cannot set attribute 'value')"),
    ("Color.RED.value += 1\n", "Color is an enum: its members can't be changed (cannot set attribute 'value')"),
    (MATCH_ONE, "function 'f' can reach its end without returning a value (it's declared to return str)"),
    ("def f(c: Color) -> None:\n    match c:\n        case 1:\n            pass\n",
     "this pattern can never match: a Color is never equal to an int"),
    ("print(Color.RED < Color.GREEN)\n", "'<' isn't supported between Color and Color (define __lt__ on Color)"),
    ("print(Color.RED == 1)\n", "comparing Color with int using '==' is always False"),
    ("print(Color('x'))\n", "Color() looks a member up by its value, an int, not a str"),
    ("print(Color[1])\n", "Color[...] (a member's name) must be str, not int"),
    ("print(Color)\n", "print() argument must be something printable, not type[Color]"),
    ("print(Color.RED + 1)\n", "unsupported operand types for +: Color and int (define __add__ on Color)"),
    ("@unique\nclass D(Enum):\n    A = 1\n    B = 1\n    C = 2\n    E = 2\n",
     "duplicate values found in <enum 'D'>: B -> A, E -> C"),
    ("class D(Enum):\n    A = 1\n    B = 'x'\n", "an enum's values must all have the same type, but A is int and B is str"),
    ("class D(Enum):\n    A = 1\n    A = 2\n", "'A' already defined as 1"),
    ("class D(StrEnum):\n    A = 1\n", "1 is not a string (a StrEnum's values are strs)"),
    ("class D(IntEnum):\n    A = 'a'\n", "IntEnum members must be ints, not str"),
    ("class D(Enum):\n    A = 'a'\n    B = auto()\n", "auto() can't follow a value that isn't an int: unable to increment 'a'"),
    ("class D(Enum):\n    A = [1]\n", "an enum member's value must be a constant (a number, string, bytes, bool, or a "
                                     "tuple of those) or auto()"),
    ("class D(Enum):\n    x: int\n", "an enum's body has members (`RED = 1`) and methods, not fields"),
    ("class D(Enum):\n    A = 1\n    def __init__(self):\n        pass\n",
     "an enum can't define __init__() (its members are fixed values)"),
    ("class D(Enum):\n    _A = 1\n", "enum members can't start with '_' ('_A'): those names are reserved"),
    ("class D(Enum):\n    name = 1\n", "an enum member can't be called 'name': every member has .name and .value"),
    ("class D(Color):\n    pass\n", "an enum with members can't be inherited from: 'Color' is a fixed set of values"),
    ("@unique\nclass D:\n    x: int\n", "@unique is for enums (`class Color(Enum):`)"),
    ("x = auto()\n", "auto() can only be the value of a member in an enum's body (`RED = auto()`)"),
    ("class P(Flag):\n    A = 1\nprint(P.A | 2)\n", "unsupported operand types for |: P and int (define __or__ on P)"),
    ("class P(Flag):\n    A = -1\n", "a flag's values can't be negative (A is -1)"),
    ("import json\nprint(json.dumps(Color.RED))\n",
     "Color is an enum; convert its members with .value (and back with Color(value))"),
])
def test_enum_errors(src, msg):
    assert err(ENUM + src).message == msg


def test_enum_match_covering_every_member_needs_no_return():
    ok(ENUM + MATCH_ONE + "        case Color.GREEN:\n            return 'g'\n")
    ok(ENUM + "def f(c: Color | None) -> str:\n    match c:\n        case Color.RED | Color.GREEN:\n            return 'x'\n"
       "        case None:\n            return '-'\n")
    assert "can reach its end" in err(ENUM + "def f(c: Color | None) -> str:\n    match c:\n        case Color.RED:\n"
                                      "            return 'r'\n        case Color.GREEN:\n            return 'g'\n").message


def test_enum_types():
    info = ok(ENUM + "class N(IntEnum):\n    A = auto()\nclass P(Flag):\n    R = auto()\n"
              "a = Color.RED\nb = Color(2).value\nc = Color['RED'].name\nd = N.A + 1\ne = P.R | P.R\nf = P.R.name\n"
              "g = list(Color)\nh = len(Color)\n")
    assert {"a: Color", "b: int", "c: str", "d: int", "e: P", "f: str?", "g: list[Color]", "h: int"} <= set(variables(info))


def test_enum_auto_values():
    info = ok(ENUM + "class A(Enum):\n    X = auto()\n    Y = 10\n    Z = auto()\n"
              "class F(Flag):\n    A = 3\n    B = auto()\nclass S(StrEnum):\n    Hello = auto()\n")
    [a] = [st for st in info.structs if st.name == "A"]
    [f] = [st for st in info.structs if st.name == "F"]
    [s] = [st for st in info.structs if st.name == "S"]
    assert [m.value for m in a.enum.members.values()] == [1, 10, 11]
    assert [m.value for m in f.enum.members.values()] == [3, 4]
    assert [m.value for m in s.enum.members.values()] == ["hello"]


@pytest.mark.parametrize("src,msg", [
    ("x = 1\ndel x\nprint(x)\n", "'x' was deleted (line 2): give it a new value before using it again"),
    ("x = 1\nif len('a') > 0:\n    del x\nprint(x)\n",
     "'x' might have been deleted (line 3): give it a new value before using it again"),
    ("del nope\n", "name 'nope' is not defined"),
    ("class P:\n    a: int\np = P(1)\ndel p.a\n", "can't delete attribute 'a': an object's fields are fixed by its class"),
    ("s = 'abc'\ndel s[0]\n", "str doesn't support item deletion"),
    ("t = (1, 2)\ndel t[0]\n", "tuple[int, int] doesn't support item deletion"),
    ("d = {1: 2}\ndel d[1:2]\n", "'del' of a slice needs a list, not dict[int, int]"),
    ("d = {'a': 1}\ndel d[1]\n", "dict key must be str, not int"),
    ("xs = [1]\ndel xs['a']\n", "list index must be int, not str"),
    ("x = 1\ndef f():\n    global x\n    del x\n",
     "'del' of a global or nonlocal variable isn't supported; give 'x' a new value instead"),
    ("def f():\n    x = 1\n    g = lambda: x\n    del x\n    return g\n",
     "'x' is used by a nested function or lambda, so it can't be deleted"),
    ("x = 1\ndel x\ndef f() -> int:\n    return x\n", "functions can't use the module-level 'x': it's deleted (line 2)"),
    ("class C:\n    n: int\nc = C(1)\ndel c[0]\n", "C doesn't support item deletion: give it a __delitem__ method"),
])
def test_del_errors(src, msg):
    assert err(src).message == msg


def test_del_then_rebind():
    assert variables(ok("xs = [1, 2]\ndel xs\nxs = 'now a str'\nprint(xs)\n")) == ["xs: list[int]", "xs_1: str"]


@pytest.mark.parametrize("src,msg", [
    ("xs = [1, 2]\nxs[0:1] = 5\n", "can only assign an iterable to a slice, not int"),
    ("xs = [1, 2]\nxs[0:1] = ['a']\n", "can't store items of type str in a list[int]"),
    ("xs = [1]\nxs['a':] = [2]\n", "slice index must be int, not str"),
    ("s = 'ab'\ns[0:1] = 'x'\n", "str can't be changed in place (it's immutable)"),
    ("from collections import deque\nq = deque([1])\nq[0:1] = [2]\n", "can't assign to a slice of a deque[int], only of a list"),
    ("xs = [1, 2]\nxs[0:1] += [3]\n",
     "augmented assignment to a slice isn't supported; write it out: `xs[a:b] = xs[a:b] + ...`"),
])
def test_slice_assignment_errors(src, msg):
    assert err(src).message == msg


def test_slice_assignment_of_an_empty_list_to_a_field():
    ok("""
    class Bag:
        items: list[int]
        def clear_middle(self):
            self.items[1:-1] = []
    """)


@pytest.mark.parametrize("src,msg", [
    ("a, b, *c = (1,)\n", "not enough values to unpack (expected at least 2, got 1)"),
    ("a, *b = (1,)\n", "'*b' would always be empty here, so its type can't be told"),
    ("a, *b = (1, 'x', 2)\n", "the starred items have different types: str, int"),
    ("a, b = 5\n", "can't unpack int: it isn't iterable"),
    ("print([*5])\n", "can't unpack int with '*': it isn't iterable"),
    ("print({**[1]})\n", "'**' needs a dict, not list[int]"),
    ("print([*[1], 'a'])\n", "list items have different types: int and str"),
    ("xs = [1, 2]\nprint(max(*xs))\n",
     "unpacking a list with '*' isn't supported in this call yet; unpack a tuple, or pass the items one by one"),
    ("def f(a: int, b: int = 2) -> int:\n    return a + b\nprint(f(*[1]))\n",
     "a list unpacked with '*' can't fill 'b' of f(), which has a default (the list's length isn't known until it "
     "runs); pass it by name, or unpack a tuple"),
    ("def f(a: int, b: int) -> int:\n    return a + b\nprint(f(*[1], 2))\n",
     "only the last positional argument of f() can be unpacked from a list (a tuple can be unpacked anywhere)"),
    ("def f(a: int) -> int:\n    return a\nprint(f(*(1, 2)))\n", "f() takes 1 argument but 2 were given"),
    ("def f(*a: int) -> int:\n    return len(a)\nprint(f(*['x']))\n", "*a of f() takes int arguments, not str (from *)"),
    ("print(*5)\n", "can't unpack int with '*': it isn't iterable"),
])
def test_unpacking_errors(src, msg):
    assert err(src).message == msg


def test_starred_display_types():
    assert variables(ok("xs = [1]\na = [*xs, 2.5]\nt = (*xs, 3)\nu = (*(1, 'a'), 2)\nd = {**{'k': 1}, 'j': 2}\n")) == [
        "xs: list[int]", "a: list[float]", "t: tuple[int, ...]", "u: tuple[int, str, int]", "d: dict[str, int]"]


def test_unpacking_types():
    assert variables(ok("first, *rest = 'a b c'.split()\nx, *mid, y = (1, 2, 2.5, 'z')\n")) == [
        "first: str", "rest: list[str]", "x: int", "mid: list[float]", "y: str"]


@pytest.mark.parametrize("src,msg", [
    ("def f(a: int, *, b: int = 2, c: int) -> int:\n    return a\nf(1, 2, 3)\n",
     "f() takes 1 positional argument but 3 were given"),
    ("def f(a: int, *, b: int = 2, c: int) -> int:\n    return a\nf(1)\n", "f() is missing keyword-only argument 'c'"),
    ("def g(a: int, b: int, /, c: int) -> int:\n    return a\ng(1, b=2, c=3)\n",
     "g() got some positional-only arguments passed as keyword arguments: 'b'"),
    ("def g(a: int, b: int, /, c: int) -> int:\n    return a\ng(a=1, b=2, c=3)\n",
     "g() got some positional-only arguments passed as keyword arguments: 'a, b'"),
])
def test_keyword_and_positional_only_errors(src, msg):
    assert err(src).message == msg


def test_bool_arithmetic():
    assert variables(ok("a = True + 1\nb = -True\nc = True & False\nd = sum([True, False])\ne = True / 2\n")) == [
        "a: int", "b: int", "c: bool", "d: int", "e: float"]
    assert err("print(~True)\n").message == (
        "'~' on a bool is deprecated in Python (it gives -2 for True); use 'not' to negate it, or ~int(x) for the int's bits")


@pytest.mark.parametrize("src,msg", [
    ("rows = [[1], [2]]\nx = zip([0], *rows)\n", "zip() can unpack a list with '*' only when it's the only argument"),
    ("rows = [1, 2]\nx = zip(*rows)\n", "zip() needs iterables, not int"),
    ("import os\nparts = [1]\nx = os.path.join('a', *parts)\n", "os.path.join() needs strs, not list[int] unpacked"),
])
def test_star_builtin_errors(src, msg):
    assert err(src).message == msg


def test_star_builtin_types():
    assert variables(ok("import itertools\nrows = [[1], [2]]\nz = list(zip(*rows))\np = list(itertools.product(*rows))\n"
                        "c = list(itertools.chain(*rows))\n")) == [
        "rows: list[list[int]]", "z: list[tuple[int, ...]]", "p: list[tuple[int, ...]]", "c: list[int]"]
