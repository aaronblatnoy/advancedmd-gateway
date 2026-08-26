"""SPEC 17.1: nothing a non-PHI caller receives carries plaintext PHI.

These run the REAL Redactor -- not a lambda double -- over the exact
result shapes the copied handlers build, because the leaks this file
locks shut were all shapes a key list of normalised field names does not
reach:

  * `query`, the search echo every lookup handler returns. For
    amd_patients_lookup_patient that echo IS a patient name.
  * AMD's own attribute spellings (`id`, `chart`, `memo`, `zipcode`)
    which survive verbatim under `_attrs` / `_child_text` in the `raw`
    echo, because only the handler's flattened copy gets renamed.
  * `_text`, AMD's free-text nodes.
  * `raw_xml`, AMD's XML string.

Every name here is synthetic (SPEC 23.3). The synthetic-fixture
invariant covers tests/fixtures/; these payloads are hand-written inline
and contain no real patient data either.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from amd_mcp_common import redact
from amd_mcp_common.redact import Redactor

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "knowledge" / "policies" / "phi-redaction-fields.data.json"

#: Not a secret and not persisted: the process key is random per run
#: (SPEC 17.1), so tests pin one to keep hashes comparable.
HASH_KEY = b"synthetic-test-hash-key-not-a-secret"

#: Synthetic identities. Names invented for this file; no real patient.
SYNTHETIC_LAST = "Quandex"
SYNTHETIC_FIRST = "Marlowe"
SYNTHETIC_QUERY = f"{SYNTHETIC_LAST}, {SYNTHETIC_FIRST}"
SYNTHETIC_DOB = "1/1/1900"
SYNTHETIC_CHART = "SYN-00042"
SYNTHETIC_MEMO = "prefers afternoon visits"


@pytest.fixture
def redactor() -> Redactor:
    """The real redactor, loading the real knowledge policy file."""
    return Redactor()


def apply(redactor: Redactor, payload: Any) -> Any:
    return redactor.apply(payload, allow_phi=False, hash_key=HASH_KEY)


def plaintext_of(payload: Any) -> str:
    """Every string in a payload, so a leak anywhere is one assert."""
    return json.dumps(payload, default=str)


# ------------------------------------------------- the policy file itself


def test_the_knowledge_policy_file_exists_and_parses():
    """Without it the redactor warns and silently falls back."""
    assert POLICY_PATH.exists(), f"missing {POLICY_PATH}"
    json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def test_the_loaded_policy_covers_every_fallback_key(redactor: Redactor):
    """A loaded file REPLACES the fallback, so it must be a superset.

    Without this, adding a key to _FALLBACK_PHI_KEYS while forgetting the
    knowledge file would make the file the weaker of the two -- and the
    file is what production loads.
    """
    assert redact._default_knowledge_path() == POLICY_PATH
    assert not redact._FALLBACK_PHI_KEYS - redactor.phi_keys
    assert not redact._FALLBACK_NON_PHI_KEEP - redactor.non_phi_keep
    assert redactor.patterns


def test_the_files_free_text_keys_match_the_module_set():
    """`free_text_keys` is declared in the file but OWNED by the module.

    redact.py decides `_text` value-aware, so it cannot read the list the
    way it reads `categories`. That makes the file's copy documentation
    only -- and a policy file whose PHI list has no effect is worse than
    one that omits it, because an operator who adds a key there would
    believe it was being redacted. Pin them equal so the drift is a
    failing test rather than a silent hole.
    """
    declared = json.loads(POLICY_PATH.read_text(encoding="utf-8"))["free_text_keys"]

    assert {str(k).lower() for k in declared} == set(redact._FREE_TEXT_KEYS)


@pytest.mark.parametrize(
    "key",
    ["query", "id", "chart", "chart_number", "memo", "zipcode", "raw_xml"],
)
def test_each_closed_hole_is_phi_in_both_the_file_and_the_fallback(
    key: str, redactor: Redactor
):
    assert key in redact._FALLBACK_PHI_KEYS
    assert redactor.is_phi_key(key, strict=False)


# ------------------------------------------------------- lookup_patient


def lookup_patient_result() -> dict[str, Any]:
    """The shape amd_patients_lookup_patient returns (handler verbatim)."""
    return {
        "query": SYNTHETIC_QUERY,
        "page": 1,
        "count": 2,
        "matches": [
            {
                "patient_id": "900001",
                "chart_number": SYNTHETIC_CHART,
                "first_name": SYNTHETIC_FIRST,
                "last_name": SYNTHETIC_LAST,
                "dob": SYNTHETIC_DOB,
            },
            {
                "patient_id": "900002",
                "chart_number": "SYN-00043",
                "first_name": "Verity",
                "last_name": SYNTHETIC_LAST,
                "dob": "1/1/1970",
            },
        ],
        "narrow_query": False,
    }


def test_lookup_patient_query_echo_is_not_returned_in_plaintext(redactor: Redactor):
    """The hole: `query` on a patient lookup is the patient's name."""
    out = apply(redactor, lookup_patient_result())

    assert out["query"] != SYNTHETIC_QUERY
    assert out["query"] == "<REDACTED>"
    assert out["query_hash"]
    assert SYNTHETIC_QUERY not in plaintext_of(out)


def test_lookup_patient_matches_carry_no_plaintext_identity(redactor: Redactor):
    out = apply(redactor, lookup_patient_result())
    text = plaintext_of(out)

    for value in (SYNTHETIC_FIRST, SYNTHETIC_LAST, SYNTHETIC_DOB, SYNTHETIC_CHART):
        assert value not in text
    for match in out["matches"]:
        for key in ("first_name", "last_name", "dob", "chart_number", "patient_id"):
            assert match[key] == "<REDACTED>"


def test_lookup_patient_keeps_the_non_phi_envelope(redactor: Redactor):
    """Redaction must not cost the caller the structure it reasons over."""
    out = apply(redactor, lookup_patient_result())

    assert out["count"] == 2
    assert out["page"] == 1
    assert out["narrow_query"] is False
    assert len(out["matches"]) == 2


# -------------------------------------------------------- getdemographic


def getdemographic_result() -> dict[str, Any]:
    """A raw_to_dict echo: AMD's attribute spellings, not ours.

    `_attrs` / `_text` / `_child_text` are what raw_to_dict and
    extract_rows_by_tag build, so this is the tree a handler's `raw` key
    actually holds.
    """
    return {
        "patient_id": "900001",
        "raw": {
            "_tag": "PPMDResults",
            "_attrs": {},
            "_children": [
                {
                    "_tag": "patient",
                    "_attrs": {
                        "id": "900001",
                        "chart": SYNTHETIC_CHART,
                        "memo": SYNTHETIC_MEMO,
                        "zipcode": "00000",
                    },
                    "_children": [
                        {
                            "_tag": "lastname",
                            "_attrs": {},
                            "_text": SYNTHETIC_LAST,
                        },
                        {
                            "_tag": "note",
                            "_attrs": {},
                            "_text": SYNTHETIC_MEMO,
                        },
                        {
                            "_tag": "empty",
                            "_attrs": {},
                            "_text": "",
                        },
                    ],
                }
            ],
        },
        "rows": [
            {
                "id": "900001",
                "chart": SYNTHETIC_CHART,
                "_child_text": {"lastname": SYNTHETIC_LAST, "dob": SYNTHETIC_DOB},
                "_child_attrs": {"insurance": {"memberid": "SYN-MEM-1"}},
            }
        ],
    }


def test_getdemographic_amd_attribute_spellings_do_not_leak(redactor: Redactor):
    """The hole: `id` / `chart` / `memo` / `zipcode` under `_attrs`."""
    out = apply(redactor, getdemographic_result())
    attrs = out["raw"]["_children"][0]["_attrs"]

    for key in ("id", "chart", "memo", "zipcode"):
        assert attrs[key] == "<REDACTED>", key
        assert attrs[f"{key}_hash"], key
    assert SYNTHETIC_CHART not in plaintext_of(out)
    assert SYNTHETIC_MEMO not in plaintext_of(out)


def test_getdemographic_free_text_nodes_do_not_leak(redactor: Redactor):
    """The hole: `_text`. Value-aware -- empty text stays empty."""
    out = apply(redactor, getdemographic_result())
    children = out["raw"]["_children"][0]["_children"]
    by_tag = {child["_tag"]: child for child in children}

    assert by_tag["lastname"]["_text"] == "<REDACTED>"
    assert by_tag["note"]["_text"] == "<REDACTED>"
    assert by_tag["empty"]["_text"] == ""
    assert "_text_hash" not in by_tag["empty"]


def test_getdemographic_flattened_rows_do_not_leak(redactor: Redactor):
    """extract_rows_by_tag output: attrs plus _child_text / _child_attrs."""
    out = apply(redactor, getdemographic_result())
    row = out["rows"][0]

    assert row["id"] == "<REDACTED>"
    assert row["chart"] == "<REDACTED>"
    assert row["_child_text"]["lastname"] == "<REDACTED>"
    assert row["_child_text"]["dob"] == "<REDACTED>"
    assert row["_child_attrs"]["insurance"]["memberid"] == "<REDACTED>"


def test_no_synthetic_identity_survives_anywhere_in_the_echo(redactor: Redactor):
    """One assert over the whole tree: the leak could be at any depth."""
    text = plaintext_of(apply(redactor, getdemographic_result()))

    for value in (SYNTHETIC_LAST, SYNTHETIC_DOB, SYNTHETIC_CHART,
                  SYNTHETIC_MEMO, "SYN-MEM-1"):
        assert value not in text, value


# --------------------------------------------------------------- raw_xml


def test_raw_xml_string_is_not_returned_in_plaintext(redactor: Redactor):
    """SPEC 17.1: unredactable, so a non-PHI caller never sees it.

    gateway/worker.py strips the key outright; the redactor blanks it
    so a result that reaches a non-PHI caller by any other path is
    covered too.
    """
    xml = (
        f'<patient chart="{SYNTHETIC_CHART}">'
        f"<lastname>{SYNTHETIC_LAST}</lastname></patient>"
    )
    out = apply(redactor, {"note_id": "SYN-1", "raw_xml": xml})

    assert out["raw_xml"] == "<REDACTED>"
    assert SYNTHETIC_LAST not in plaintext_of(out)
    assert out["note_id"] == "SYN-1"


# -------------------------------------------------------- the phi=true side


def test_a_phi_caller_sees_the_payload_untouched(redactor: Redactor):
    """allow_phi=True is identity. Workflows depend on the echo."""
    payload = lookup_patient_result()

    assert redactor.apply(payload, allow_phi=True, hash_key=HASH_KEY) is payload


def test_revealed_fields_names_the_holes_it_would_redact(redactor: Redactor):
    """The audit path must be able to say a free-text node was revealed."""
    revealed = redactor.revealed_fields(getdemographic_result())

    assert "raw._children[0]._attrs.id" in revealed
    assert "raw._children[0]._attrs.chart" in revealed
    assert "raw._children[0]._children[0]._text" in revealed
    # Empty text is not a revealed field: there is nothing in it.
    assert "raw._children[0]._children[2]._text" not in revealed
