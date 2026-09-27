"""amd_ehr_get_ehr_templates — the practice's EHR note templates.

Returns the template rows (id + name), not just a count: a caller that
needs ``getehrnotes`` must pass a ``templateid`` (AMD faults "Template Id
is not valid" without one) and this is the only way to learn the ids.
Templates are practice configuration, not patient data; every other
attribute AMD sends is dropped.
"""
from __future__ import annotations
from typing import Any
from amd_mcp_common.errors import safe_amd_call
from ._common import count_rows_for_tags, extract_rows_by_tag, get_client, raw_to_dict

ACTION = "getehrtemplates"
WRITE_ACTION = False
TIER = 1
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


def _templates(raw_dict: Any) -> list[dict[str, str]]:
    for tag in _ROW_TAGS:
        rows = extract_rows_by_tag(raw_dict, tag)
        if rows:
            return [
                {"id": _first(r, _ID_KEYS), "name": _first(r, _NAME_KEYS)}
                for r in rows
            ]
    return []


async def handle() -> dict[str, Any]:
    client = get_client()
    raw_dict, err = safe_amd_call(client, action="getehrtemplates", raw_to_dict_fn=raw_to_dict)
    if err is not None:
        return {**err}
    templates = _templates(raw_dict)
    return {
        "count": count_rows_for_tags(raw_dict, *_ROW_TAGS),
        "templates": templates,
    }
