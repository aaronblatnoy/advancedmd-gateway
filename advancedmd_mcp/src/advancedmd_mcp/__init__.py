"""advancedmd-mcp: the local stdio shim. SPEC 12.3.

It speaks MCP over stdio to an agent on a workstation and turns every
call into an HTTP request to the gateway. It holds no AdvancedMD
credentials, no tool logic and no AdvancedMD knowledge: the tool list
comes from GET /v1/tools at start-up and every tools/call becomes POST
/v1/tools. Swapping an agent between this shim and the remote surface
(SPEC 12.2) changes no tool name, no schema and no result shape.
"""
from __future__ import annotations

from advancedmd_mcp.__main__ import (
    MCP_PROTOCOL_VERSION,
    DOMAINS,
    ConfigMissing,
    Shim,
    main,
    mcp_tool_from_row,
)

__all__ = [
    "DOMAINS",
    "MCP_PROTOCOL_VERSION",
    "ConfigMissing",
    "Shim",
    "main",
    "mcp_tool_from_row",
]
__version__ = "1.0.0"
