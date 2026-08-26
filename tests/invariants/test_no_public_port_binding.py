"""SPEC 17.4: the gateway is never published on every interface.

/health and /metrics are deliberately unauthenticated, which is only safe
because the port is unreachable from outside the tailnet. A bare
"8820:8820" mapping in docker-compose.yml binds 0.0.0.0 on the host and
undoes that. This test makes the regression impossible to reintroduce
quietly.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"

#: "8820:8820" with no host address in front of it.
BARE_MAPPING = re.compile(r'^\s*-\s*"?(?!\d+\.\d+\.\d+\.\d+:)\d+:\d+"?\s*$', re.M)


def test_compose_publishes_no_bare_port_mapping():
    text = COMPOSE.read_text(encoding="utf-8")
    offenders = [m.group(0).strip() for m in BARE_MAPPING.finditer(text)]
    assert not offenders, (
        "docker-compose.yml publishes on all host interfaces: "
        f"{offenders}. Bind the mapping to the tailnet address or 127.0.0.1."
    )


def test_compose_port_mapping_names_a_host_address():
    text = COMPOSE.read_text(encoding="utf-8")
    assert re.search(r'-\s*"\d+\.\d+\.\d+\.\d+:\d+:\d+"', text), (
        "docker-compose.yml should publish 8820 on one explicit host address"
    )
