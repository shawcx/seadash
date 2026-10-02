"""The programs in tests/programs/ (not seadash/) are valid Python: python3 must give the
output in their .out files too, or seadash and Python have drifted apart.

Programs that use `from seadash import ...` get the Python versions in seadash/__init__.py.
A program whose output depends on the Python version is skipped on older ones."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PROGRAMS = ROOT / "tests" / "programs"
CASES = sorted(p.stem for p in PROGRAMS.glob("*.sd"))

# The Python each program's output needs (its .out was made with it).
NEEDS_PYTHON = {
    "gzip_module": (3, 13),  # (gzip.compress writes OS = 255 from 3.13)
    "struct_module": (3, 13),
    "copy_module": (3, 13),  # (copy.replace)
    "statistics_module": (3, 13),  # (geometric_mean's error for an empty dataset)
    "zipfile_module": (3, 13),  # (ZipInfo.compress_level, ZipExtFile.mode)
    "fnmatch_glob": (3, 14),  # (fnmatch.filterfalse)
    "tarfile_module": (3, 14),  # (extraction filters are the default)
    "str_replace": (3, 13),  # (replace's count by keyword)
}


@pytest.mark.parametrize("name", CASES)
def test_python_gives_the_same_output(name: str, tmp_path):
    if sys.version_info < NEEDS_PYTHON.get(name, (0,)):
        pytest.skip(f"needs Python {'.'.join(map(str, NEEDS_PYTHON[name]))}")
    path = PROGRAMS / f"{name}.sd"
    shutil.copytree(PROGRAMS.parent / "certs", tmp_path / "certs")  # (the HTTPS programs' certificate)
    r = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=120, cwd=tmp_path,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.stdout == path.with_suffix(".out").read_text()
    err_file, exit_file = path.with_suffix(".err"), path.with_suffix(".exit")
    if err_file.exists():  # an uncaught exception: Python also shows a traceback, seadash only its last line
        assert r.stderr.splitlines()[-1:] == err_file.read_text().splitlines()[-1:]
    expected_code = int(exit_file.read_text()) if exit_file.exists() else (1 if err_file.exists() else 0)
    assert r.returncode == expected_code
