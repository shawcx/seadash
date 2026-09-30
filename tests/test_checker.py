import textwrap

import pytest

from seadash.checker import ModuleInfo, check
from seadash.errors import CheckError, Loc
from seadash.parser import parse


def ok(source: str) -> ModuleInfo:
    return check(parse(textwrap.dedent(source)))


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
        struct P:
            x: int
        def f(p: P?) -> int:
            return p.x
    """)
    assert e.message == "P? might be None; check it first, e.g. `if p is not None:`"


@pytest.mark.parametrize("condition", ["p is not None", "p", "p != None"])
def test_narrowing_in_if(condition):
    ok(f"""
        struct P:
            x: int
        def f(p: P?) -> int:
            if {condition}:
                return p.x
            return 0
    """)


def test_narrowing_after_early_return():
    ok("""
        struct P:
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
        struct Point:
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
        struct Point:
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
        struct Node:
            value: int
            next: Node?
    """)
    assert e.message == "struct 'Node' can't contain itself (field 'next'); make it a class, or use a list"
    ok("""
        class Node:
            value: int
            next: Node?
    """)


def test_struct_body_restrictions():
    e = err("""
        struct P:
            x = 1
    """)
    assert e.message == "a struct body can only contain fields (`x: int`) and methods (`def ...`)"


def test_method_needs_self():
    assert err("struct P:\n    def f(): pass\n").message == "method 'f' needs 'self' as its first parameter"


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
    ("struct A: pass\nstruct B(A): pass", "structs can't inherit (they're values; use a class): `class B(A):`"),
    ("struct A: pass\nclass B(A): pass", "can't inherit from struct 'A'; only classes can be inherited from"),
    ("def f():\n    class C: pass", "a class can only be defined at the top level of a module"),
])
def test_not_yet_supported(src, msg):
    assert err(src).message == msg


# ---- exceptions -------------------------------------------------------------


@pytest.mark.parametrize("src,msg", [
    ("raise", "a bare 'raise' can only re-raise inside an 'except' block"),
    ("raise 5", "can only raise exceptions, not int"),
    ("struct P:\n    x: int\nraise P", "can only raise exceptions, not P"),
    ("class E(Exception):\n    code: int\nraise E", "E needs arguments: `raise E(...)`"),
    ("try:\n    pass\nexcept int:\n    pass", "'except' needs an exception class, like `except ValueError:`"),
    ("struct S(Exception):\n    pass", "exceptions must be classes, not structs: `class S(Exception):`"),
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
        struct P:
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
    ("f: (int) -> int = lambda a: a\nf('x')", "argument 1 must be int, not str"),
    ("f: (int) -> int = lambda a: a\nf(1, 2)", "this function takes 1 argument but 2 were given"),
    ("f: (int) -> int = lambda a: a\nf(a=1)", "keyword arguments can't be used when calling a function value"),
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
    ('x = bytes("abc")', "bytes(str) needs an encoding; use s.encode() instead"),
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
        struct P:
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
struct Address:
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
    ("def big[T](xs: list[T]) -> T:\n    return xs[0] if xs[0] > xs[1] else xs[1]\nstruct P:\n    x: int\ny = big([P(1)])",
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
    ).message == "V is a frozen dataclass; its field 'x' can't be changed"
    assert err(
        "from dataclasses import dataclass\n@dataclass(frozen=True)\nclass V:\n    x: int\nv = V(1)\nv.x += 2\n"
    ).message == "V is a frozen dataclass; its field 'x' can't be changed"
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
        "only @dataclass can decorate a class (for now)"
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
