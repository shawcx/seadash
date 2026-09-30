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

