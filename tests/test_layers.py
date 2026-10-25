"""The layer rules from PLAN.md, checked on the imports of every module."""

import ast
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parent.parent / "src" / "lumina"

# layer -> top-level modules it must not import
FORBIDDEN = {
    "core": {"PySide6", "shiboken6", "lumina.qt_models", "lumina.integration", "lumina.ui"},
    "ui": {"rawpy", "PIL", "sqlite3", "onnxruntime", "subprocess", "concurrent", "cv2"},
    "qt_models": {"lumina.ui"},
    "integration": {"lumina.ui", "lumina.qt_models"},
}


# The domain model the UI shares with core (#109): records, their constants and errors,
# plain data that imports nothing but the standard library and each other. The rest of
# core is reached through qt_models
DOMAIN = {
    "lumina.core.dbtypes",
    "lumina.core.edit.export_options",
    "lumina.core.edit.params",
    "lumina.core.errors",
    "lumina.core.keywords",
    "lumina.core.smart",
}


def _is_module(name):
    path = PACKAGE.parent.joinpath(*name.split("."))
    return path.with_suffix(".py").exists() or (path / "__init__.py").exists()


def imported_modules(path):
    """Every module `path` imports; `from package import module` counts as the module."""
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                name = f"{node.module}.{alias.name}"
                yield name if _is_module(name) else node.module


def module_path(name):
    path = PACKAGE.parent.joinpath(*name.split("."))
    return path.with_suffix(".py") if path.with_suffix(".py").exists() else path / "__init__.py"


@pytest.mark.parametrize("layer", FORBIDDEN)
def test_layer_imports(layer):
    files = list((PACKAGE / layer).rglob("*.py"))
    assert files
    for path in files:
        for module in imported_modules(path):
            for forbidden in FORBIDDEN[layer]:
                assert module != forbidden and not module.startswith(forbidden + "."), (
                    f"{path.relative_to(PACKAGE)} imports {module}"
                )


def test_the_ui_takes_only_the_domain_model_from_core():
    for path in (PACKAGE / "ui").rglob("*.py"):
        for module in imported_modules(path):
            if module == "lumina.core" or module.startswith("lumina.core."):
                assert module in DOMAIN, f"ui/{path.relative_to(PACKAGE / 'ui')} imports {module}"


@pytest.mark.parametrize("module", sorted(DOMAIN))
def test_the_domain_model_is_plain_data(module):
    """Importing it loads no numpy, OpenCV, Qt or database: only the standard library and
    the rest of the domain model."""
    for imported in imported_modules(module_path(module)):
        top = imported.split(".")[0]
        assert top in sys.stdlib_module_names or imported in DOMAIN, f"{module} imports {imported}"


def test_the_window_parts_do_not_import_the_window():
    """A controller is handed what it works with (#108): none imports the window."""
    files = list((PACKAGE / "ui" / "controllers").rglob("*.py"))
    assert files
    for path in files:
        for module in imported_modules(path):
            assert module != "lumina.ui.main_window", f"{path.relative_to(PACKAGE)} imports it"


def test_no_two_test_modules_share_a_name():
    """The test folders are not packages, so pytest imports each module by its name: two of
    the same name are a collection error, seen only when both are collected (the full run).
    The helpers beside them (photos.py, fakes.py) are imported by name too."""
    tests = Path(__file__).parent
    by_name = {}
    for path in sorted(tests.rglob("*.py")):
        if path.name != "conftest.py" and "samples" not in path.relative_to(tests).parts:
            by_name.setdefault(path.name, []).append(str(path.relative_to(tests)))
    shared = {name: paths for name, paths in by_name.items() if len(paths) > 1}
    assert not shared, f"modules of the same name in tests/: {shared}"
