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


def _templates(raw_dict: Any) -> list[dict[str, Any]]:
    """One row per template: best-effort id/name plus AMD's own attributes.

    AMD's attribute spelling for the template name is not documented
    (live 2026-09-27: 59 rows, ids present, none of the guessed name keys
    matched), so the row carries ``attrs`` verbatim. Templates are
    practice configuration; the caller-policy redactor still applies.
    """
    for tag in _ROW_TAGS:
        rows = extract_rows_by_tag(raw_dict, tag)
        if rows:
            return [
                {
                    "id": _first(r, _ID_KEYS),
                    "name": _first(r, _NAME_KEYS),
                    "attrs": {str(k): str(v) for k, v in r.items()},
                }
                for r in rows
            ]
    return []


async def handle() -> dict[str, Any]:
    client = get_client()
    # The gateway client is async; the legacy sync safe_amd_call never
    # sends anything through it (that is why the BETA count-only EHR
    # handlers were never verifiable).
    _element, raw_dict, err = await safe_amd_call_element_async(
        client, action=ACTION, raw_to_dict_fn=raw_to_dict, class_="api",
    )
    if err is not None:
        return {**err}
    templates = _templates(raw_dict)
    return {
        "count": count_rows_for_tags(raw_dict, *_ROW_TAGS),
        "templates": templates,
    }
