"""Portal must not speak HTTP to AdvancedMD hosts (browser-only)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_FORBIDDEN_IMPORTS = frozenset({"httpx", "requests"})
_AMD_HOST_FRAGMENTS = frozenset(
    {"advancedmd.com", "login.advancedmd.com", "static-100.advancedmd.com"}
)
_ROOT = Path(__file__).resolve().parents[2] / "portal"
# httpx is allowed under portal/llm/ (local Ollama only).
_HTTpx_ALLOWED = _ROOT / "llm"
# Navigation URL literals are allowed in browser/login and flow docs.
_URL_LITERAL_ALLOWED = frozenset({"browser.py", "login.py", "eligibility.py"})


def _iter_py_files():
    for path in _ROOT.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        if _HTTpx_ALLOWED in path.parents:
            continue
        yield path


def _find_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _FORBIDDEN_IMPORTS:
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _FORBIDDEN_IMPORTS:
                hits.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if path.name in _URL_LITERAL_ALLOWED:
                continue
            val = node.value.lower()
            for frag in _AMD_HOST_FRAGMENTS:
                if frag in val:
                    hits.append(f"AMD URL literal in {path.name}")
                    break
    return hits


@pytest.mark.parametrize("path", list(_iter_py_files()), ids=lambda p: p.name)
def test_portal_no_direct_amd_http(path: Path):
    violations = _find_violations(path)
    assert not violations, f"{path}: {violations}"
