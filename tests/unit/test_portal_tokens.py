"""portal_tools allowlist on Caller."""
from __future__ import annotations

from gateway.interfaces import Caller
from gateway.tokens import TokenTable


def test_portal_tools_default_deny():
    table = TokenTable.parse(
        {
            "callers": [
                {
                    "name": "test",
                    "hash": "sha256:" + "a" * 64,
                    "priority": "interactive",
                    "tools": ["getdemographic"],
                }
            ]
        }
    )
    caller = next(iter(table.values()))
    assert caller.portal_tools == ()


def test_portal_tools_explicit_list():
    table = TokenTable.parse(
        {
            "callers": [
                {
                    "name": "portal-batch",
                    "hash": "sha256:" + "b" * 64,
                    "priority": "batch",
                    "tools": [],
                    "portal_tools": ["get_insurance_details"],
                }
            ]
        }
    )
    caller = next(iter(table.values()))
    assert caller.portal_tools == ("get_insurance_details",)


def test_portal_tools_wildcard():
    table = TokenTable.parse(
        {
            "callers": [
                {
                    "name": "portal-all",
                    "hash": "sha256:" + "c" * 64,
                    "priority": "interactive",
                    "portal_tools": "*",
                }
            ]
        }
    )
    caller = next(iter(table.values()))
    assert caller.portal_tools == "*"
