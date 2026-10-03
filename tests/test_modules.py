"""Importing .sd files: resolution, errors, and error locations."""

import textwrap

import pytest

from seadash.driver import translate
from seadash.errors import CompileError


def write(tmp_path, files: dict[str, str]):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    return tmp_path / "main.sd"


def compile_error(tmp_path, files) -> CompileError:
    main = write(tmp_path, files)
    with pytest.raises(CompileError) as info:
        translate(main.read_text(), main)
    return info.value


def test_modules_compile_in_dependency_order(tmp_path):
    main = write(tmp_path, {
        "main.sd": "import b\nprint(b.twice(2))\n",
        "b.sd": "import a\ndef twice(n: int) -> int:\n    return a.add(n, n)\n",
        "a.sd": "def add(x: int, y: int) -> int:\n    return x + y\n",
    })
    cpp = translate(main.read_text(), main).cpp
    assert cpp.index("namespace sdm::a") < cpp.index("namespace sdm::b") < cpp.index("namespace prog")
    assert "sdm::a::module_main(); sdm::b::module_main(); prog::module_main();" in cpp


def test_missing_module(tmp_path):
    e = compile_error(tmp_path, {"main.sd": "import nothere\n"})
    assert e.message.startswith("no module named 'nothere'")


def test_circular_import(tmp_path):
    e = compile_error(tmp_path, {
        "main.sd": "import a\n",
        "a.sd": "import b\n",
        "b.sd": "import a\n",
    })
    assert e.message == "circular import: a -> b -> a"
    assert e.file.endswith("b.sd")


def test_errors_in_imported_modules_name_that_file(tmp_path):
    e = compile_error(tmp_path, {
        "main.sd": "import lib\n",
        "lib.sd": "x = 1\ny = x + 'a'\n",
    })
    assert e.file.endswith("lib.sd")
    rendered = e.render("ignored", "main.sd")
    assert rendered.startswith(str(tmp_path / "lib.sd") + ":2:7: error: unsupported operand types")
    assert "y = x + 'a'" in rendered


def test_missing_member(tmp_path):
    e = compile_error(tmp_path, {"main.sd": "from lib import nope\n", "lib.sd": "x = 1\n"})
    assert e.message == "module 'lib' has no member 'nope'"


def test_local_module_shadows_builtin_like_python(tmp_path):
    main = write(tmp_path, {
        "main.sd": "import json\nprint(json.fake())\n",
        "json.sd": "def fake() -> str:\n    return 'local json'\n",
    })
    assert "sdm::json::fake()" in translate(main.read_text(), main).cpp


def test_super_across_modules(tmp_path):
    # A class here puts its own class between an imported one's method and its super() call.
    main = write(tmp_path, {
        "main.sd": """
            from mixlib import Base, Logged
            class Cached(Base):
                def save(self) -> str:
                    return "cached(" + super().save() + ")"
            class Store(Logged, Cached):
                pass
            print(Store().save())
        """,
        "mixlib.sd": """
            class Base:
                def save(self) -> str:
                    return "Base"
            class Logged(Base):
                def save(self) -> str:
                    return "logged(" + super().save() + ")"
        """,
    })
    cpp = translate(main.read_text(), main).cpp
    mine = cpp[cpp.index("namespace prog"):]
    assert "struct Base " not in mine and "struct Logged " not in mine  # (defined with their module)
    assert "std::string sd_next_Logged_0() override { return this->Cached::save(); }" in mine


def test_generics_across_modules(tmp_path):
    main = write(tmp_path, {
        "main.sd": """
            import coll
            from coll import Stack
            s = Stack[int]()
            s.push(1)
            t: coll.Stack[str] = coll.Stack()
            print(coll.first([1.5]), s.items, t.items)
        """,
        "coll.sd": """
            def first[T](xs: list[T]) -> T?:
                return xs[0] if xs else None
            class Stack[T]:
                items: list[T]
                def __init__(self):
                    self.items = []
                def push(self, x: T):
                    self.items.append(x)
        """,
    })
    cpp = translate(main.read_text(), main).cpp
    assert "struct Stack_of_int" in cpp and "struct Stack_of_str" in cpp
    assert cpp.index("struct Stack_of_int") < cpp.index("namespace prog")  # generated inside module coll


def test_generic_with_a_class_from_the_importing_module_is_rejected_for_now(tmp_path):
    e = compile_error(tmp_path, {
        "main.sd": "import coll\nclass P:\n    x: int\ny = coll.first([P(1)])\n",
        "coll.sd": "def first[T](xs: list[T]) -> T?:\n    return xs[0] if xs else None\n",
    })
    assert e.message == "first (from module 'coll') can't be used with P from module '__main__' yet"


def test_printing_modules(tmp_path):
    main = write(tmp_path, {
        "main.sd": "import math\nimport geo\nimport os.path\nprint(math, geo, os.path)\n",
        "geo.sd": "X = 1\n",
    })
    cpp = translate(main.read_text(), main).cpp
    assert "sd::ModuleRef{\"<module 'math' (built-in)>\"s}" in cpp
    assert f"sd::ModuleRef{{\"<module 'geo' from '{tmp_path / 'geo.sd'}'>\"s}}" in cpp
    assert "<module 'path' (built-in)>" in cpp or "<module 'os.path' (built-in)>" in cpp


def test_modules_cant_be_stored(tmp_path):
    e = compile_error(tmp_path, {"main.sd": "import math\nm = math\n"})
    assert e.message == "a module can't be stored in a variable; use `import ... as name` to rename it"
