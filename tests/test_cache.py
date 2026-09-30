"""The build cache: finished binaries are reused, and a changed program is rebuilt."""

import subprocess

import pytest

from seadash import cli, driver
from seadash.driver import BuildOptions, cache_dir, compile_cpp, translate


@pytest.fixture(autouse=True)
def no_precompiled_header(monkeypatch):
    # (building one per test would be slow; the program tests use it)
    monkeypatch.setattr(driver, "precompiled_header", lambda cxx, flags: None)


def build(tmp_path, source: str, name: str, cache: bool = True):
    cpp = tmp_path / f"{name}.cpp"
    cpp.write_text(translate(source).cpp)
    binary = tmp_path / name
    compile_cpp(cpp, binary, BuildOptions(optimize=False, cache=cache))
    return subprocess.run([str(binary)], capture_output=True, text=True).stdout


def test_binaries_are_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("SEADASH_CACHE_DIR", str(tmp_path / "cache"))
    assert build(tmp_path, 'print("one")\n', "a") == "one\n"
    cached = list((tmp_path / "cache" / "bin").iterdir())
    assert len(cached) == 1
    assert build(tmp_path, 'print("one")\n', "b") == "one\n"  # same C++: from the cache
    assert list((tmp_path / "cache" / "bin").iterdir()) == cached
    assert build(tmp_path, 'print("two")\n', "c") == "two\n"  # changed: compiled again
    assert len(list((tmp_path / "cache" / "bin").iterdir())) == 2


def test_no_cache_and_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SEADASH_CACHE_DIR", str(tmp_path / "cache"))
    assert build(tmp_path, 'print("x")\n', "a", cache=False) == "x\n"
    assert not (tmp_path / "cache" / "bin").exists()
    build(tmp_path, 'print("x")\n', "b")
    assert cache_dir().exists()
    assert cli.main(["clean"]) == 0
    assert not cache_dir().exists()
