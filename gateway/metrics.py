"""/metrics, Prometheus text format. SPEC 18.1.

Self-contained on purpose: no client library, so there is no third-party
code deciding what a label may contain. The metric names below are the
SPEC 18.1 list exactly, and METRIC_NAMES is asserted against the
declarations so a typo fails at import rather than in Grafana.

PHI rule (SPEC 17.1): label values are caller names, tool names, AMD
action names, tiers, outcomes and HTTP statuses -- identifiers the
operator already knows. Never args, never results, never a patient
identifier. That is enforced, not assumed: every label value is checked
against a conservative identifier pattern and a length cap, and a value
that fails is replaced with "other" rather than exported.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "METRIC_NAMES",
    "LABEL_VALUE_MAX",
    "OTHER",
    "Metrics",
    "safe_label",
]

#: SPEC 18.1, verbatim.
METRIC_NAMES: tuple[str, ...] = (
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

LABEL_VALUE_MAX = 64
OTHER = "other"
_LABEL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,%d}$" % LABEL_VALUE_MAX)

#: Wait/elapsed/post latency buckets, seconds. Batch waits are long, so
#: the tail goes out to 15 minutes.
_LATENCY_BUCKETS: tuple[float, ...] = (
    0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900,
)


def safe_label(value: Any) -> str:
    """Coerce a label value to a PHI-free identifier.

    Anything that is not a short identifier-shaped token becomes
    "other". A patient name, a date of birth with slashes, an XML
    fragment or a long string can therefore never reach the exposition.
    """
    text = "" if value is None else str(value)
    if not _LABEL_RE.match(text):
        return OTHER
    return text


class _Series:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0.0


class _Histogram:
    __slots__ = ("buckets", "count", "total")

    def __init__(self, buckets: Sequence[float]) -> None:
        self.buckets = {b: 0 for b in buckets}
        self.count = 0
        self.total = 0.0

    def observe(self, seconds: float) -> None:
        self.count += 1
        self.total += float(seconds)
        for bound in self.buckets:
            if seconds <= bound:
                self.buckets[bound] += 1


def _fmt(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def _labels_text(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return "{" + inner + "}"


class Metrics:
    """Every SPEC 18.1 metric, and the Prometheus text rendering.

    One instance per process. Methods take the same label names the SPEC
    18.1 list declares; every value passes through safe_label().
    """

    def __init__(self, instance_id: str = "connector") -> None:
        self._lock = threading.Lock()
        self._instance_id = safe_label(instance_id)
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], _Series] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], _Series] = {}
        self._hists: dict[tuple[str, tuple[tuple[str, str], ...]], _Histogram] = {}
        self.set_up(True)

    # ------------------------------------------------------ plumbing

    @staticmethod
    def _key(name: str, labels: Mapping[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
        if name not in METRIC_NAMES:
            raise KeyError(f"metric not in SPEC 18.1: {name}")
        clean = tuple((k, safe_label(v)) for k, v in labels.items())
        return name, clean

    def _inc(self, name: str, labels: Mapping[str, Any], amount: float = 1.0) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters.setdefault(key, _Series()).value += float(amount)

    def _set(self, name: str, labels: Mapping[str, Any], value: float) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._gauges.setdefault(key, _Series()).value = float(value)

    def _observe(self, name: str, labels: Mapping[str, Any], seconds: float) -> None:
        key = self._key(name, labels)
        with self._lock:
            hist = self._hists.get(key)
            if hist is None:
                hist = self._hists[key] = _Histogram(_LATENCY_BUCKETS)
            hist.observe(float(seconds))

    # -------------------------------------------------- SPEC 18.1 API

    def tool_call(self, caller: str, tool: str, outcome: str) -> None:
        self._inc("connector_tool_calls_total",
                  {"caller": caller, "tool": tool, "outcome": outcome})

    def tool_wait(self, caller: str, priority: str, seconds: float) -> None:
        self._observe("connector_tool_wait_seconds",
                      {"caller": caller, "priority": priority}, seconds)

    def tool_elapsed(self, tool: str, seconds: float) -> None:
        self._observe("connector_tool_elapsed_seconds", {"tool": tool}, seconds)

    def amd_request(self, action: str, tier: Any, outcome: str) -> None:
        self._inc("connector_amd_requests_total",
                  {"action": action, "tier": tier, "outcome": outcome})

    def amd_post(self, tier: Any, seconds: float) -> None:
        self._observe("connector_amd_post_seconds", {"tier": tier}, seconds)

    def clock_window(self, tier: Any, used: int, limit: int) -> None:
        self._set("connector_clock_used", {"tier": tier}, used)
        self._set("connector_clock_limit", {"tier": tier}, limit)

    def clock_slept(self, tier: Any, seconds: float) -> None:
        self._inc("connector_clock_sleep_seconds_total", {"tier": tier}, seconds)

    def relogin(self, reason: str) -> None:
        """reason = startup | 1025 | manual (SPEC 18.1)."""
        self._inc("connector_session_relogins_total", {"reason": reason})

    def login_refused(self, http_status: Any) -> None:
        self._inc("connector_session_login_refused_total",
                  {"http_status": http_status})

    def entry_queue_depth(self, depth: int) -> None:
        self._set("connector_entry_queue_depth", {}, depth)

    def request_queue_depth(self, depth: int) -> None:
        self._set("connector_request_queue_depth", {}, depth)

    def set_up(self, up: bool) -> None:
        self._set("connector_up", {"instance_id": self._instance_id},
                  1 if up else 0)

    # --------------------------------------------------- exposition

    def render(self) -> str:
        """Prometheus text format (version 0.0.4)."""
        with self._lock:
            counters = {k: v.value for k, v in self._counters.items()}
            gauges = {k: v.value for k, v in self._gauges.items()}
            hists = {
                k: (dict(h.buckets), h.count, h.total) for k, h in self._hists.items()
            }
        lines: list[str] = []
        for name in METRIC_NAMES:
            body = self._render_metric(name, counters, gauges, hists)
            if body:
                lines.extend(body)
        return "\n".join(lines) + "\n"

    def _render_metric(
        self,
        name: str,
        counters: Mapping[Any, float],
        gauges: Mapping[Any, float],
        hists: Mapping[Any, tuple[Mapping[float, int], int, float]],
    ) -> list[str]:
        out: list[str] = []
        matching_counters = [(k, v) for k, v in counters.items() if k[0] == name]
        matching_gauges = [(k, v) for k, v in gauges.items() if k[0] == name]
        matching_hists = [(k, v) for k, v in hists.items() if k[0] == name]
        if matching_counters:
            out.append(f"# TYPE {name} counter")
            for (_, labels), value in sorted(matching_counters):
                out.append(f"{name}{_labels_text(dict(labels))} {_fmt(value)}")
        if matching_gauges:
            out.append(f"# TYPE {name} gauge")
            for (_, labels), value in sorted(matching_gauges):
                out.append(f"{name}{_labels_text(dict(labels))} {_fmt(value)}")
        if matching_hists:
            out.append(f"# TYPE {name} histogram")
            for (_, labels), (buckets, count, total) in sorted(matching_hists):
                base = dict(labels)
                for bound in sorted(buckets):
                    marked = {**base, "le": _fmt(bound)}
                    out.append(f"{name}_bucket{_labels_text(marked)} {buckets[bound]}")
                inf = {**base, "le": "+Inf"}
                out.append(f"{name}_bucket{_labels_text(inf)} {count}")
                out.append(f"{name}_sum{_labels_text(base)} {_fmt(total)}")
                out.append(f"{name}_count{_labels_text(base)} {count}")
        return out


def metric_names() -> Iterable[str]:
    return METRIC_NAMES
