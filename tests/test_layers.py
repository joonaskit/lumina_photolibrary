"""The layer rules from PLAN.md, checked on the imports of every module."""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parent.parent / "src" / "lumina"

# layer -> top-level modules it must not import
FORBIDDEN = {
    "core": {"PySide6", "shiboken6", "lumina.qt_models", "lumina.integration", "lumina.ui"},
    "ui": {"rawpy", "PIL", "sqlite3", "onnxruntime", "subprocess", "concurrent", "lumina.core"},
    "qt_models": {"lumina.ui"},
    "integration": {"lumina.ui", "lumina.qt_models"},
}


def imported_modules(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


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


def test_the_window_parts_do_not_import_the_window():
    """A controller is handed what it works with (#108): none imports the window."""
    files = list((PACKAGE / "ui" / "controllers").rglob("*.py"))
    assert files
    for path in files:
        for module in imported_modules(path):
            assert module != "lumina.ui.main_window", f"{path.relative_to(PACKAGE)} imports it"
