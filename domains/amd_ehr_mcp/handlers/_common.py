"""Shared helpers for amd-ehr-mcp handlers."""
from __future__ import annotations

import dataclasses
from datetime import date, datetime
from typing import Any, Callable

from amd_mcp_common.action_guard import maybe_guarded


_client_factory: Callable[[], Any] | None = None


def set_client_factory(factory: Callable[[], Any]) -> None:
    global _client_factory
    _client_factory = factory


def get_client():
    if _client_factory is None:
        raise RuntimeError(
            "amd-ehr-mcp: handlers._common has no AMDClient factory. "
            "server.build_server(...) must call set_client_factory()."
        )
    return maybe_guarded(_client_factory())


def serialize(obj) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return serialize(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {k: serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "tag") and hasattr(obj, "attrib"):
        return raw_to_dict(obj)
    return obj


def raw_to_dict(node) -> Any:
    if node is None:
        return None
    if not hasattr(node, "tag"):
        return str(node)
    out: dict[str, Any] = {"_tag": node.tag, "_attrs": dict(node.attrib)}
    children = list(node)
    if children:
        out["_children"] = [raw_to_dict(c) for c in children]
    text = (node.text or "").strip() if node.text else ""
    if text:
        out["_text"] = text
    return out


def extract_rows_by_tag(raw_dict: Any, tag: str) -> list[dict]:
    """Walk the raw_to_dict tree and pull all <tag> elements out."""
    out: list[dict] = []
    if not isinstance(raw_dict, dict):
        return out

    def _walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("_tag") == tag:
            attrs = dict(node.get("_attrs") or {})
            out.append(attrs)
            return
        for child in node.get("_children") or []:
            _walk(child)

    _walk(raw_dict)
    return out


def count_rows_for_tags(raw_dict: Any, *tags: str) -> int:
    """Sum counts across multiple candidate row tags.

    EHR endpoints vary in tag naming (allergy/medication/problem/etc.);
    we pass a tuple and take the FIRST non-zero match, mirroring the
    fallback pattern used in patients-mcp.getreminderappts.
    """
    for tag in tags:
        rows = extract_rows_by_tag(raw_dict, tag)
        if rows:
            return len(rows)
    return 0


# --------------------------------------------------------------------
# Async bridge to the gateway's client shim (SPEC 4.4, Amendment D-2).
#
# amd_mcp_common.errors.safe_amd_call is synchronous: it was written for
# the vendored blocking AMDClient. In the gateway the client is
# gateway/client_shim.py, whose call() is a coroutine that awaits
# send(). Calling the sync helper here would hand the handler an
# un-awaited coroutine instead of a reply tree.
#
# This is the same function with one difference: it awaits an awaitable
# result. Fault translation is unchanged (it reuses translate_amd_error),
# so handler result shapes are identical. Connector errors pass straight
# through -- the worker maps them (SPEC 5.4), and swallowing one into an
# {"error": ...} envelope would hide a refusal behind a 200.
# --------------------------------------------------------------------


async def safe_amd_call_element_async(client, *, action: str, raw_to_dict_fn, **kwargs):
    """As ``safe_amd_call_async``, but also hands back the parsed element.

    Returns ``(element_or_None, raw_dict_or_None, error_envelope_or_None)``.

    The element is the tree ``send()`` already parsed and returned -- it
    is not a second read of the wire and nothing here retains it. Only
    ``getehrnotes`` needs it, to re-serialize AMD's note list into
    ``result["raw_xml"]`` (D-R4-1/D-R4-2, docs/GATEWAY_DECISIONS.md D27).
    Every other handler keeps the 2-tuple wrapper below.
    """
    import inspect as _inspect

    from amd_mcp_common.errors import translate_amd_error
    from gateway.errors import ConnectorError

    try:
        raw = client.call(action=action, **kwargs)
        if _inspect.isawaitable(raw):
            raw = await raw
    except ConnectorError:
        raise
    except BaseException as exc:  # noqa: BLE001 - surface the envelope
        return None, None, translate_amd_error(exc)
    raw_dict = raw_to_dict_fn(raw)
    envelope = translate_amd_error(raw_dict)
    if envelope.get("error") and envelope["error"] != "ok":
        return raw, raw_dict, envelope
    return raw, raw_dict, None


async def safe_amd_call_async(client, *, action: str, raw_to_dict_fn, **kwargs):
    """Await ``client.call(...)`` and inspect the result for AMD faults.

    Returns the same ``(raw_dict_or_None, error_envelope_or_None)``
    2-tuple as ``amd_mcp_common.errors.safe_amd_call``.
    """
    _element, raw_dict, err = await safe_amd_call_element_async(
        client, action=action, raw_to_dict_fn=raw_to_dict_fn, **kwargs
    )
    return raw_dict, err
