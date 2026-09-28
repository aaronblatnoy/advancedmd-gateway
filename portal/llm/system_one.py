"""System One (TypeSafe) judgments for the portal, via s1-server only.

Candidate rows, chart headers and search strings are PHI, so every question
asked from here goes to the on-box s1-server (Winnow/Laya over Ollaya on
black-sky). The hosted Jev API is never used from this module.

Contract mirrors TypeSafe /v1/systemone: state + questions (a dict keyed by
question id; choice/noul), answers carry probabilities we threshold in code.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("portal.system_one")

__all__ = [
    "ChoiceAnswer",
    "PortalSystemOne",
    "SystemOneError",
    "SystemOneUnavailable",
    "system_one_from_env",
]

phi_safe = True  # module invariant: on-box s1-server only

DEFAULT_S1_URL = "http://100.94.62.115:8003"
DEFAULT_S1_MODEL = "winnow:e4b"


class SystemOneError(RuntimeError):
    """s1-server returned an error or an unparseable answer."""


class SystemOneUnavailable(SystemOneError):
    """s1-server not configured (no key) or unreachable."""


@dataclass(slots=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float] = field(default_factory=dict)

    @property
    def probability(self) -> float:
        return float(self.probabilities.get(self.choice, 0.0))

    def ranked(self) -> list[tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: -kv[1])


class PortalSystemOne:
    """Thin async client for s1-server's /v1/systemone."""

    phi_safe = True

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("S1_SERVER_URL", DEFAULT_S1_URL)
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("S1_SERVER_API_KEY", "")
        self.model = model or os.environ.get("S1_MODEL", DEFAULT_S1_MODEL)
        self.timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise SystemOneUnavailable("S1_SERVER_API_KEY not set")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(
                    f"{self.base_url}/v1/systemone", json=body, headers=headers
                )
        except httpx.HTTPError as exc:
            raise SystemOneUnavailable(f"s1-server unreachable: {type(exc).__name__}") from exc
        if r.status_code >= 500:
            raise SystemOneUnavailable(f"s1-server HTTP {r.status_code}")
        if r.status_code >= 400:
            raise SystemOneError(f"s1-server HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise SystemOneError("s1-server returned non-JSON") from exc

    async def choice(
        self,
        *,
        state: dict[str, Any],
        instructions: str,
        criteria: dict[str, str],
    ) -> ChoiceAnswer:
        """Ask one Choice question; return the pick with its distribution."""
        body = {
            "state": state,
            "model": self.model,
            "questions": {
                "q": {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": criteria,
                }
            },
        }
        reply = await self._post(body)
        answer = _first_answer(reply)
        choice = answer.get("choice") or answer.get("answer")
        probs = answer.get("probabilities") or {}
        if not isinstance(choice, str) or choice not in criteria:
            raise SystemOneError("choice answer missing or outside criteria")
        clean = {
            str(k): float(v)
            for k, v in probs.items()
            if isinstance(v, (int, float))
        }
        return ChoiceAnswer(choice=choice, probabilities=clean)

    async def noul(self, *, state: dict[str, Any], instructions: str) -> float:
        """Ask one yes/no question; return P(yes)."""
        body = {
            "state": state,
            "model": self.model,
            "questions": {"q": {"type": "noul", "instructions": instructions}},
        }
        reply = await self._post(body)
        answer = _first_answer(reply)
        # s1-server answers {"type": "noul", "noul": 0.97}; the hosted API
        # spells it "probability".
        p = answer.get("noul", answer.get("probability", answer.get("p_yes")))
        if not isinstance(p, (int, float)):
            raise SystemOneError("noul answer missing probability")
        return float(p)


def _first_answer(reply: dict[str, Any]) -> dict[str, Any]:
    answers = reply.get("answers")
    if isinstance(answers, dict):
        answers = answers.get("q") or next(iter(answers.values()), None)
    elif isinstance(answers, list):
        answers = answers[0] if answers else None
    if not isinstance(answers, dict):
        raise SystemOneError("s1-server reply carried no answers")
    return answers


def system_one_from_env() -> PortalSystemOne:
    return PortalSystemOne()
