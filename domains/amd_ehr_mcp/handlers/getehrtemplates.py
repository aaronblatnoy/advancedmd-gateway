"""amd_ehr_get_ehr_templates — the practice's EHR note templates.

Returns the template rows (id + name), not just a count: a caller that
needs ``getehrnotes`` must pass a ``templateid`` (AMD faults "Template Id
is not valid" without one) and this is the only way to learn the ids.
Templates are practice configuration, not patient data; every other
attribute AMD sends is dropped.
"""
from __future__ import annotations
from typing import Any
from ._common import (
    count_rows_for_tags,
    extract_rows_by_tag,
    get_client,
    raw_to_dict,
    safe_amd_call_element_async,
)

ACTION = "getehrtemplates"
WRITE_ACTION = False
TIER = 3
PERMITTED_ACTIONS = ("getehrtemplates",)
_ROW_TAGS = ("template", "ehrtemplate")
_ID_KEYS = ("id", "templateid", "template_id")
_NAME_KEYS = ("name", "templatename", "template_name", "description")


def _first(attrs: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = attrs.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _template_nodes(raw_dict: Any) -> list[dict]:
    """All raw nodes whose tag is a template row, in document order."""
    out: list[dict] = []

    def _walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("_tag") in _ROW_TAGS:
            out.append(node)
            return
        for child in node.get("_children") or []:
            _walk(child)

    _walk(raw_dict)
    return out


def _templates(raw_dict: Any) -> list[dict[str, Any]]:
    """One row per template: best-effort id/name plus everything AMD sent.

    Live 2026-09-27 (dermacare): 59 rows whose ONLY attribute is ``id``;
    the name is not an attribute, so the row also carries the element
    text and each child element's tag, attributes and text. Templates
    are practice configuration; the caller-policy redactor still applies.
    """
    rows: list[dict[str, Any]] = []
    for node in _template_nodes(raw_dict):
        attrs = {str(k): str(v) for k, v in (node.get("_attrs") or {}).items()}
        text = str(node.get("_text") or "").strip()
        children: dict[str, Any] = {}
        for child in node.get("_children") or []:
            if not isinstance(child, dict):
                continue
            tag = str(child.get("_tag") or "")
            if not tag:
                continue
            child_text = str(child.get("_text") or "").strip()
            child_attrs = {str(k): str(v) for k, v in (child.get("_attrs") or {}).items()}
            children[tag] = child_text if child_text and not child_attrs else (
                {**child_attrs, **({"text": child_text} if child_text else {})}
            )
        name = _first(attrs, _NAME_KEYS) or text
        if not name:
            for key in _NAME_KEYS:
                v = children.get(key)
                if isinstance(v, str) and v:
                    name = v
                    break
        rows.append({
            "id": _first(attrs, _ID_KEYS),
            "name": name,
            "attrs": attrs,
            "text": text,
            "children": children,
        })
    return rows


async def handle() -> dict[str, Any]:
    client = get_client()
    # The gateway client is async; the legacy sync safe_amd_call never
    # sends anything through it (that is why the BETA count-only EHR
    # handlers were never verifiable).
    # AMD replies only with the columns the request names (same pattern
    # as getdatevisits' <visit .../> children). Without this child the
    # reply rows carry nothing but @id (live 2026-09-27).
    from lxml import etree
    children = [etree.Element("template", id="Id", name="Name", description="Description")]
    _element, raw_dict, err = await safe_amd_call_element_async(
        client, action=ACTION, raw_to_dict_fn=raw_to_dict, class_="api",
        children=children,
    )
    if err is not None:
        return {**err}
    templates = _templates(raw_dict)
    return {
        "count": len(templates) or count_rows_for_tags(raw_dict, *_ROW_TAGS),
        "templates": templates,
    }
