"""Local Ollama adapter for portal recovery (phi_safe only)."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("portal.llm")

__all__ = ["OllamaRecoveryLLM", "RecoveryLLMError"]

phi_safe = True  # module invariant: never call hosted models from here


class RecoveryLLMError(RuntimeError):
    """Recovery LLM unavailable or misconfigured."""


class OllamaRecoveryLLM:
    """Minimal tool-calling client for bounded portal recovery."""

    phi_safe = True

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get(
                "PORTAL_LLM_BASE_URL", "http://100.94.62.115:8000"
            )
        ).rstrip("/")
        self.model = model or os.environ.get(
            "PORTAL_LLM_MODEL", "llama3.2-vision"
        )
        self.timeout_s = timeout_s

    async def available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                for path in ("/v1/models", "/api/tags"):
                    r = await client.get(f"{self.base_url}{path}")
                    if r.status_code == 200:
                        return True
        except Exception:
            pass
        return False

    async def choose_action(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        image_b64: str | None = None,
    ) -> dict[str, Any]:
        """Return parsed tool-call arguments from the model."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
        ]
        user_msg: dict[str, Any] = {"role": "user", "content": user}
        if image_b64:
            user_msg["images"] = [image_b64]
        messages.append(user_msg)

        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {"temperature": 0},
        }
        url = f"{self.base_url}/v1/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            raise RecoveryLLMError("ollama request failed") from exc

        choices = data.get("choices") or []
        if not choices:
            raise RecoveryLLMError("ollama returned no choices")
        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            # Plain-text fallback: treat as abort
            return {"action": "abort", "reason": "model returned no tool call"}
        fn = tool_calls[0].get("function") or {}
        name = fn.get("name") or "abort"
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {}
        args["action"] = name
        return args
