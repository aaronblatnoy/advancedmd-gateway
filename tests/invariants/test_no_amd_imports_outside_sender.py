"""SPEC 6.2 / 23.6: handlers never reach AdvancedMD directly.

No module under domains/ or gateway/ may import an HTTP client or name
an AdvancedMD URL, except gateway/sender.py and gateway/session.py.

This is a real test over the real tree, not a placeholder: it parses every
Python file with ast, so it sees actual imports and actual string
literals and is not fooled by -- nor tripped up by -- prose in a comment
or a docstring that merely discusses httpx.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRS = ("domains", "gateway")

#: The only two modules permitted to speak HTTP to AdvancedMD (SPEC 23.6).
ALLOWED = {
    REPO_ROOT / "gateway" / "sender.py",
    REPO_ROOT / "gateway" / "session.py",
}

FORBIDDEN_IMPORTS = {"httpx", "requests", "urllib3", "aiohttp", "http.client"}

#: Any AMD host. The partner-login host is the one that actually appears
#: in the vendored clients; the bare domain catches the regional
#: endpoints too.
FORBIDDEN_URL_MARKERS = ("advancedmd.com", "partnerlogin")


def python_files() -> list[Path]:
    out: list[Path] = []
    for directory in SCANNED_DIRS:
        root = REPO_ROOT / directory
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            if path in ALLOWED:
                continue
            out.append(path)
    return out


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() of every Constant node that is a module/class/function docstring."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(
                body[0].value, ast.Constant
            ) and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _root_module(name: str) -> str:
    return name.split(".")[0]


ALL_FILES = python_files()


def test_the_scan_actually_covers_the_tree():
    """Guard against a silently empty walk (a passing test that tests nothing)."""
    assert len(ALL_FILES) > 50, "domains/ and gateway/ should be populated"
    assert any(p.parts[-3:-1] == ("amd_patients_mcp", "handlers") for p in ALL_FILES)


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_http_client_import_and_no_amd_url(path: Path):
    source = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source, filename=str(path))
    rel = path.relative_to(REPO_ROOT)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert _root_module(alias.name) not in FORBIDDEN_IMPORTS, (
                    f"{rel} imports {alias.name}; only gateway/sender.py and "
                    "gateway/session.py may reach AdvancedMD (SPEC 6.2)"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert _root_module(module) not in FORBIDDEN_IMPORTS, (
                f"{rel} imports from {module}; only gateway/sender.py and "
                "gateway/session.py may reach AdvancedMD (SPEC 6.2)"
            )

    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            lowered = node.value.lower()
            for marker in FORBIDDEN_URL_MARKERS:
                assert marker not in lowered, (
                    f"{rel} contains an AdvancedMD URL; it belongs in "
                    "gateway/session.py (SPEC 6.2, 23.6)"
                )
