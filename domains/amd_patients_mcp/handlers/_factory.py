"""Build ToolSpec list from handler modules + loaded policies.

At boot, server.py calls ``build_specs(policies, schemas, client)``.
The factory:

  1. Iterates the handler modules in `_HANDLER_MODULES`.
  2. For each, reads ACTION / WRITE_ACTION / TIER / PERMITTED_ACTIONS.
  3. Matches against the loaded policy (keyed by ACTION).
  4. Builds an mcp.types.Tool with name from policy.tool_name +
     inputSchema from the loaded JSON Schema.
  5. Returns a list of ToolSpec.
"""
from __future__ import annotations

from typing import Any

import mcp.types as mcp_types

from amd_mcp_common.base_server import ToolSpec
from amd_mcp_common.knowledge_loader import ActionPolicy

from . import (
    getdemographic,
    getmaster_patient,
    getpatientvisits,
    getreminderappts,
    getupdatedpatients,
    lookup_patient,
    is_patient,
    savedemographic,
    upddemographic,
    # E1 additions per AMD_TOOLS_FULL_BUILDOUT_PLAN-final.txt
    getcustomdata,
    getreminderpatientbirthdays,
    lookuprespparty,
    addpatient,
    updatepatient,
    addinsurance,
    updateinsurance,
    savepatientnotes,
    addrespparty,
    addreferral,
    updatereferral,
    uploadfile,
)


# Each (module, action_key_used_to_match_policy).
_HANDLER_MODULES: tuple = (
    getdemographic,
    getupdatedpatients,
    lookup_patient,
    is_patient,
    getmaster_patient,
    getpatientvisits,
    getreminderappts,
    savedemographic,
    upddemographic,
    # E1 additions:
    getcustomdata,
    getreminderpatientbirthdays,
    lookuprespparty,
    addpatient,
    updatepatient,
    addinsurance,
    updateinsurance,
    savepatientnotes,
    addrespparty,
    addreferral,
    updatereferral,
    uploadfile,
)


def build_specs(
    *,
    policies: dict[str, ActionPolicy],
    schemas: dict[str, dict[str, Any]],
) -> list[ToolSpec]:
    """Assemble ToolSpec list from handler modules + policies + schemas.

    Skips any handler whose policy is missing (logs a warning).
    """
    specs: list[ToolSpec] = []
    for mod in _HANDLER_MODULES:
        action_key = mod.ACTION
        policy = policies.get(action_key)
        if policy is None:
            # No policy means no MCP tool exposure. Aaron-curated layer.
            import logging
            logging.getLogger("amd_patients_mcp.factory").warning(
                "handler %r has ACTION=%r but no matching policy file; "
                "skipping.",
                mod.__name__, action_key,
            )
            continue
        # DUO-1 / AUDIT-2 FC-4: filter meta-domain or null tool_name policies.
        # Catalog-only helper actions are suppressed from list_tools by skipping
        # spec construction here.
        if policy.domain == "meta" or policy.tool_name is None:
            continue
        schema = schemas.get(action_key, {"type": "object"})
        # Strip top-level metadata from the input schema (the LLM-facing
        # tool schema is the raw structural shape).
        input_schema = {k: v for k, v in schema.items()
                        if not k.startswith("x-") and k not in ("$schema", "$id")}
        # `description` lives in the Tool, not the inputSchema.
        description = input_schema.pop("description", None) or policy.action

        tool = mcp_types.Tool(
            name=policy.tool_name,
            description=description,
            inputSchema=input_schema,
        )
        specs.append(ToolSpec(
            tool=tool,
            handler=mod.handle,
            tier=policy.tier,
            permitted_actions=tuple(policy.permitted_actions),
            action=action_key,
            domain=policy.domain,
            write_action=getattr(mod, "WRITE_ACTION", False) or policy.write_action,
            policy=policy,
            schema_id=schema.get("$id"),
            policy_id=f"amd-mcp-policy/v1#{action_key}",
        ))
    return specs
