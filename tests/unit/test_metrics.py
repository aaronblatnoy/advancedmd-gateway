"""SPEC 18.1: the metric names, and what a label may carry.

/metrics is a public, unauthenticated endpoint, so a label value is the
easiest place in the whole gateway for PHI to escape. These tests pin
the name list to the spec and prove that anything that is not a short
identifier is dropped before it reaches the exposition.
"""
from __future__ import annotations

import pytest

from gateway.metrics import (
    LABEL_VALUE_MAX,
    METRIC_NAMES,
    OTHER,
    Metrics,
    safe_label,
)

#: SPEC 18.1, transcribed here independently of gateway/metrics.py so
#: the two have to agree.
SPEC_18_1_NAMES = (
    "connector_tool_calls_total",
    "connector_tool_wait_seconds",
    "connector_tool_elapsed_seconds",
    "connector_amd_requests_total",
    "connector_amd_post_seconds",
    "connector_clock_used",
    "connector_clock_limit",
    "connector_clock_sleep_seconds_total",
    "connector_session_relogins_total",
    "connector_session_login_refused_total",
    "connector_entry_queue_depth",
    "connector_request_queue_depth",
    "connector_up",
)


def test_metric_names_are_exactly_the_spec_list():
    assert METRIC_NAMES == SPEC_18_1_NAMES


@pytest.mark.parametrize(
    "value",
    [
        "TESTPATIENT ALPHA",           # a name: has a space
        "01/02/1980",                  # a date of birth: has slashes
        "<demographic id='900001'/>",  # an XML fragment
        "a" * (LABEL_VALUE_MAX + 1),   # too long to be an identifier
        "chart TEST900001",
        "",
        None,
    ],
)
def test_a_phi_shaped_label_value_never_reaches_the_exposition(value):
    assert safe_label(value) == OTHER


@pytest.mark.parametrize(
    "value", ["getdemographic", "billing-agent", "tier_2", "ok", "1", "amd:post"]
)
def test_identifier_shaped_labels_are_kept(value):
    assert safe_label(value) == value


def test_a_phi_shaped_caller_or_tool_is_scrubbed_in_the_rendered_text():
    metrics = Metrics(instance_id="test")
    metrics.tool_call("TESTPATIENT ALPHA", "01/02/1980", "ok")
    rendered = metrics.render()
    assert "TESTPATIENT" not in rendered
    assert "01/02/1980" not in rendered
    assert f'caller="{OTHER}"' in rendered


def test_every_declared_name_appears_in_the_rendered_text():
    metrics = Metrics(instance_id="test")
    metrics.set_up(True)
    metrics.tool_call("agent", "getdemographic", "ok")
    metrics.tool_wait("agent", "interactive", 0.2)
    metrics.tool_elapsed("getdemographic", 0.4)
    metrics.amd_request("getdemographic", 2, "ok")
    metrics.amd_post(2, 0.3)
    metrics.clock_window(2, 3, 10)
    metrics.clock_slept(2, 1.5)
    metrics.relogin("session_timeout")
    metrics.login_refused(401)
    metrics.entry_queue_depth(4)
    metrics.request_queue_depth(1)
    rendered = metrics.render()
    for name in SPEC_18_1_NAMES:
        assert name in rendered, f"{name} is declared but never rendered"
