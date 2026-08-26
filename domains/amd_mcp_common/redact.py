"""PHI redactor (lifted from legacy amd-mcp-server, action-policy aware).

Differences from the legacy module:

1. `Redactor` class wraps the global state so per-action policy can
   override / extend the base PHI key set.
2. The knowledge-path workspace marker is replaced by a configurable
   path passed to the constructor (or via env var
   `AMD_MCP_REDACTION_POLICY_PATH`). Different domain servers live at
   different parent depths from `/knowledge/` so the walk-parents
   heuristic is fallback-only.

The two top-level functions (`apply`, `revealed_fields`) remain for
backwards compat with code that doesn't yet use the per-action class.

See `2026-06-03-knowledge-driven-policy.md` for the action-policy
schema this redactor consumes.

The key set is deliberately fail-closed for `allow_phi=False`. AMD's own
attribute spellings (`id`, `chart`, `memo`, `zipcode`), the search echo
(`query`), free text nodes (`_text`) and the raw XML string (`raw_xml`)
are all PHI, because nothing in a payload distinguishes a CPT lookup's
`query`/`id` from a patient lookup's. The cost is that a non-PHI caller
sees `<REDACTED>` for a code-lookup echo and a masterfile row's internal
id; the alternative is plaintext patient names reaching an AI caller.
Callers that need those values carry a phi=true token.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("amd_mcp_common.redact")

_REDACTED = "<REDACTED>"
_HASH_LEN = 16


def _default_knowledge_path() -> Path | None:
    """Locate the global PHI policy file.

    Tries (in order):
      1. AMD_MCP_REDACTION_POLICY_PATH env var.
      2. The promoted file phi-redaction-fields.data.json.
      3. The draft file phi-redaction-fields.draft.data.json.

    The lookup walks parents from this file's directory looking for a
    sibling `knowledge/policies/` subdirectory.
    """
    override = os.environ.get("AMD_MCP_REDACTION_POLICY_PATH")
    if override:
        path = Path(override)
        return path if path.exists() else None
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "knowledge").is_dir():
            kdir = parent / "knowledge" / "policies"
            promoted = kdir / "phi-redaction-fields.data.json"
            if promoted.exists():
                return promoted
            draft = kdir / "phi-redaction-fields.draft.data.json"
            if draft.exists():
                return draft
            return None
    return None


# Fallback field list. Mirrors knowledge/policies/phi-redaction-fields.data.json;
# a test asserts the file is a superset of this set so the two cannot drift.
_FALLBACK_PHI_KEYS = frozenset({
    "first_name", "firstname", "last_name", "lastname",
    "middle_name", "middlename",
    "dob", "date_of_birth", "birthdate",
    "ssn", "social_security_number",
    "chart_number", "chartnumber",
    "mrn", "medical_record_number",
    "patient_id", "patientid",
    "address", "address1", "address2",
    "city", "zip", "postal_code",
    "phone", "phone_home", "phone_cell", "phone_work",
    "homephone", "cellphone", "workphone",
    "fax", "email",
    "member_id", "memberid",
    "subscriber_number",
    "subscriber_firstname", "subscriber_lastname", "subscriber_dob",
    "group_number", "policy_number",
    "guarantor_firstname", "guarantor_lastname",
    "guarantor_dob", "guarantor_address",
    "guarantor_phone", "guarantor_ssn",
    # The search echo. Every lookup handler repeats the caller's search
    # string back in its result, and for a patient lookup that string IS
    # a patient name. Nothing in the payload distinguishes a name search
    # from a CPT search, so `query` is PHI for every tool: a non-PHI
    # caller gets `matches` and `count`, not the echo. A PHI caller
    # (phi=true token) sees it untouched.
    "query", "search", "searchstring", "name",
    # AMD's own attribute spellings, which survive verbatim in the
    # `raw` / `_attrs` / `_child_text` echo every handler returns. The
    # normalised aliases above only cover what a handler renamed.
    "id", "chart", "memo", "zipcode", "zip4",
    # AMD's raw XML string. The worker also strips this key outright
    # unless the token carries BOTH phi and raw_xml (SPEC 17.1); the
    # redactor covers the case where a result reaches a non-PHI caller
    # through some other path.
    "raw_xml", "rawxml",
})

#: Keys whose value is free text lifted straight out of AMD's XML. The
#: redactor cannot tell a code description from a patient note, so for a
#: non-PHI caller any non-empty string under one of these keys is
#: redacted. Empty text is left alone -- there is nothing to leak and a
#: hash of "" is noise.
_FREE_TEXT_KEYS = frozenset({"_text"})

_FALLBACK_STRICT_PATTERNS: tuple[str, ...] = (
    r"^.*_name$",
    r"^.*name$",
    r"^.*_dob$",
    r"^.*ssn.*$",
    r"^.*_phone$",
    r"^.*phone$",
    r"^.*email.*$",
)

_FALLBACK_NON_PHI_KEEP = frozenset({
    "appointment_id", "appointmentid", "appt_status", "appt_id",
    "visit_id", "visit_status",
    "provider_id", "providerid", "provider_name",
    "provider_lastname", "provider_firstname",
    "rendering_provider_id", "rendering_provider_name",
    "referring_provider_id", "referring_provider_name",
    "location", "location_id", "appointment_location",
    "appointment_type", "appointment_type_id", "appointment_type_name",
    "duration_minutes", "scheduled_at", "scheduled_date",
    "appointment_datetime",
    "cpt_code", "diagnosis_code", "icd10",
    "carrier_id", "carrier_slug", "carrier_code", "carrier_name",
    "primary_insurance_carrier_code", "primary_insurance_carrier_name",
    "amd_carrier_id", "group_name",
    "copay", "copay_type", "deductible", "deductible_met",
    "coinsurance", "plan_type", "place_of_service",
})


def _load_policy(
    path: Path | None,
) -> tuple[frozenset[str], tuple[re.Pattern[str], ...], frozenset[str]]:
    if path is None:
        print(
            "[amd_mcp_common.redact] WARN: knowledge policy file not found; "
            "falling back to built-in PHI key list.",
            file=sys.stderr,
        )
        return (
            _FALLBACK_PHI_KEYS,
            tuple(re.compile(p, re.IGNORECASE) for p in _FALLBACK_STRICT_PATTERNS),
            _FALLBACK_NON_PHI_KEEP,
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        phi_keys: set[str] = set()
        for cat in data.get("categories", {}).values():
            for field in cat.get("fields", []):
                name = field.get("name")
                if isinstance(name, str):
                    phi_keys.add(name.lower())
        patterns = tuple(
            re.compile(p, re.IGNORECASE)
            for p in data.get("strict_mode_patterns", [])
        )
        keep = frozenset(
            str(k).lower() for k in data.get("non_phi_keep", [])
        )
        return frozenset(phi_keys), patterns, keep
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(
            f"[amd_mcp_common.redact] WARN: cannot parse {path}: {e}; "
            "falling back to built-in PHI key list.",
            file=sys.stderr,
        )
        return (
            _FALLBACK_PHI_KEYS,
            tuple(re.compile(p, re.IGNORECASE) for p in _FALLBACK_STRICT_PATTERNS),
            _FALLBACK_NON_PHI_KEEP,
        )


def _hmac_hash(value: Any, hash_key: bytes) -> str:
    payload = "" if value in (None, "") else str(value)
    digest = hmac.new(hash_key, payload.encode("utf-8"), sha256).hexdigest()
    return digest[:_HASH_LEN]


class Redactor:
    """Action-policy-aware PHI redactor.

    Construct with the base policy (loaded from the workspace's
    knowledge file); ``for_action(policy_dict)`` returns a child
    Redactor with the action's specific ``phi_fields`` /
    ``strict_mode_patterns`` / ``non_phi_keep`` merged in.
    """

    def __init__(
        self,
        *,
        base_phi_keys: frozenset[str] | None = None,
        base_patterns: tuple[re.Pattern[str], ...] | None = None,
        base_non_phi_keep: frozenset[str] | None = None,
        policy_path: Path | None = None,
    ) -> None:
        if base_phi_keys is None or base_patterns is None or base_non_phi_keep is None:
            loaded_path = policy_path or _default_knowledge_path()
            loaded_keys, loaded_patterns, loaded_keep = _load_policy(loaded_path)
            self.phi_keys = base_phi_keys if base_phi_keys is not None else loaded_keys
            self.patterns = base_patterns if base_patterns is not None else loaded_patterns
            self.non_phi_keep = (
                base_non_phi_keep if base_non_phi_keep is not None else loaded_keep
            )
        else:
            self.phi_keys = base_phi_keys
            self.patterns = base_patterns
            self.non_phi_keep = base_non_phi_keep

    def for_action(self, action_policy: dict | None) -> "Redactor":
        """Return a Redactor whose state is merged with the action policy.

        ``action_policy`` is the loaded JSON content of one
        ``<action>.policy.data.json`` file (under ``knowledge/integrations/amd/``).
        Recognized keys under ``redact``:

          * phi_fields            -> added to phi_keys
          * non_phi_keep          -> added to non_phi_keep
          * strict_mode_patterns  -> compiled and added to patterns
        """
        if not action_policy:
            return self
        redact_block = action_policy.get("redact", {})
        new_phi = set(self.phi_keys)
        new_phi.update(str(k).lower() for k in redact_block.get("phi_fields", []))
        new_keep = set(self.non_phi_keep)
        new_keep.update(str(k).lower() for k in redact_block.get("non_phi_keep", []))
        new_patterns = list(self.patterns)
        for p in redact_block.get("strict_mode_patterns", []):
            new_patterns.append(re.compile(p, re.IGNORECASE))
        return Redactor(
            base_phi_keys=frozenset(new_phi),
            base_patterns=tuple(new_patterns),
            base_non_phi_keep=frozenset(new_keep),
        )

    def is_phi_key(self, key: str, strict: bool) -> bool:
        k = key.lower()
        if k in self.non_phi_keep:
            return False
        if k in self.phi_keys:
            return True
        if strict:
            for pat in self.patterns:
                if pat.match(k):
                    return True
        return False

    def is_free_text_phi(self, key: str, value: Any) -> bool:
        """True for an AMD free-text node carrying something to leak.

        Key-matching alone cannot cover `_text`: the same key holds a CPT
        description on one node and a patient note on the next, so the
        decision has to see the value. Empty text is not redacted.
        """
        k = key.lower()
        if k in self.non_phi_keep or k not in _FREE_TEXT_KEYS:
            return False
        return isinstance(value, str) and bool(value.strip())

    def apply(
        self,
        payload: Any,
        *,
        allow_phi: bool,
        hash_key: bytes,
        strict: bool = True,
    ) -> Any:
        if allow_phi:
            return payload
        return self._walk(payload, hash_key, strict)

    def revealed_fields(self, payload: Any, *, strict: bool = True) -> list[str]:
        seen: set[str] = set()

        def visit(node: Any, prefix: str = "") -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    path = f"{prefix}.{k}" if prefix else str(k)
                    if isinstance(k, str) and (
                        self.is_phi_key(k, strict) or self.is_free_text_phi(k, v)
                    ):
                        seen.add(path)
                    visit(v, path)
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    visit(item, f"{prefix}[{i}]")

        visit(payload)
        return sorted(seen)

    def _walk(self, node: Any, hash_key: bytes, strict: bool) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                if isinstance(k, str) and (
                    self.is_phi_key(k, strict) or self.is_free_text_phi(k, v)
                ):
                    out[k] = _REDACTED
                    out[f"{k}_hash"] = _hmac_hash(v, hash_key)
                else:
                    out[k] = self._walk(v, hash_key, strict)
            return out
        if isinstance(node, list):
            return [self._walk(item, hash_key, strict) for item in node]
        return node


# ---------- module-level singleton (back-compat with legacy callers) -----

_module_redactor: Redactor | None = None


def _get() -> Redactor:
    global _module_redactor
    if _module_redactor is None:
        _module_redactor = Redactor()
    return _module_redactor


def apply(
    payload: Any,
    *,
    allow_phi: bool,
    hash_key: bytes,
    strict: bool = True,
) -> Any:
    """Module-level shim for code that doesn't yet use ``Redactor``."""
    return _get().apply(payload, allow_phi=allow_phi, hash_key=hash_key, strict=strict)


def revealed_fields(payload: Any, *, strict: bool = True) -> list[str]:
    return _get().revealed_fields(payload, strict=strict)


# Backwards-compat module attributes (some legacy tests reference these).
def _phi_keys_view() -> frozenset[str]:
    return _get().phi_keys


# Re-exported for symmetry with the legacy module.
PHI_KEYS = property(lambda _: _get().phi_keys)
