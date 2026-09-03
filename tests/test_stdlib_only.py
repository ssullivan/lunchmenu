"""menu.py, config.py, and notify.py must stay importable with plain
python3 -- no third-party dependency, ever (see each module's docstring and
CLAUDE.md's "stdlib-only" rule). This walks each file's AST and asserts every
top-level import resolves to a name in sys.stdlib_module_names, so a
regression is caught in CI rather than discovered the next time someone
tries to run menu.py outside the venv."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "lunchmenu"
STDLIB_ONLY_MODULES = ["menu.py", "config.py", "notify.py"]

# `lunchmenu` itself (the package these modules live in, imported as
# `from . import config` etc.) is not a stdlib module but is exactly what
# stdlib-only code in this codebase is allowed to import from -- it's this
# package's own sibling modules, not a third-party dependency.
ALLOWED_NON_STDLIB = {"lunchmenu"}


def _top_level_module(name: str) -> str:
    return name.split(".")[0]


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(_top_level_module(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # a relative import (`from . import config`) -- always
                # allowed, it's this package's own code.
                names.add("lunchmenu")
            elif node.module:
                names.add(_top_level_module(node.module))
    return names


@pytest.mark.parametrize("filename", STDLIB_ONLY_MODULES)
def test_module_imports_are_stdlib_only(filename):
    path = SRC / filename
    tree = ast.parse(path.read_text(), filename=str(path))
    imported = _imported_names(tree)
    allowed = set(sys.stdlib_module_names) | ALLOWED_NON_STDLIB
    offenders = imported - allowed
    assert not offenders, f"{filename} imports non-stdlib module(s): {sorted(offenders)}"
