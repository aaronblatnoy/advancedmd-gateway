"""Deterministic human-readable traces for computer-use tools.

Every ``run_flow`` result carries a top-level ``trace`` array: short,
PHI-free, fixed sentences describing what the automation did. Machine
stage ids stay in ``checkpoints``; ``trace`` is the operator/agent story.

Rules:
- Same stage/event sequence => same sentences (no wall-clock, no recovery
  step counts in the text — those live in checkpoints / meta).
- Never interpolate patient name, DOB, chart, member id, or page text.
- New checkpoint stage ids MUST add a label here in the same change.
"""
from __future__ import annotations

from typing import Mapping

# Stage id -> pass-line sentence. Fail / recovery wrappers are formatters below.
STAGE_LABELS: Mapping[str, str] = {
    "logged_in": "Established portal session",
    "app_ready": "App window ready",
    "scheduler_open": "Opened Scheduler",
    "patient_found": "Matched patient in scheduler search",
    "patient_info_open": "Opened patient info",
    "insurance_card_open": "Opened insurance coverage card",
    "fields_scraped": "Scraped insurance card fields",
    "claims_address_scraped": "Read claims address fields",
    "eligibility_details_open": "Opened eligibility Details (on-file 271)",
    "eligibility_check_open": "Opened Check Eligibility surface",
    "eligibility_check_fired": "Fired Check Eligibility",
    "session_probed": "Probed portal session",
}


def label_for(stage: str) -> str:
    return STAGE_LABELS.get(stage, f"Stage '{stage}'")


def format_stage_pass(stage: str) -> str:
    return label_for(stage)


def format_stage_fail(stage: str) -> str:
    return f"Failed while: {label_for(stage)}"


def format_started(flow: str) -> str:
    return f"Started {flow}"


def format_completed(flow: str) -> str:
    return f"Completed {flow}"


def format_failed(flow: str, diagnosis: str | None = None) -> str:
    if diagnosis:
        return f"Failed {flow} ({diagnosis})"
    return f"Failed {flow}"


def format_recovery(stage: str, *, cleared: bool) -> str:
    label = label_for(stage)
    if cleared:
        return f"Local LLM cleared blocker at '{label}'"
    return f"Local LLM could not clear blocker at '{label}'"


def format_session_relogin() -> str:
    return "Re-established portal session after expiry"
