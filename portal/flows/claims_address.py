"""Carrier claims-address scrape from the open legacy insurance card.

The downstream workflow needs the carrier's ``send claims to`` mailing
address for this coverage. This is NOT the patient's home address. The
legacy insurance card is already open when this module runs, so the scrape
is deliberately passive: it reads only claims-specific inputs if they are
already present in that frame.

READ-ONLY boundary (HARD)
-------------------------
This module NEVER clicks ``#ellCarrier`` or any other control. In particular,
it never clicks Check Eligibility, Save Order, Save, Submit, Bypass, or a
carrier lookup control. A carrier ellipsis may open a Carrier Master/detail
view, but that control has not been established as read-only-safe. Until a
live PHI-free structural capture and Aaron's review establish that boundary,
the carrier-detail view is not opened by automation.

Panel shape (BEST-INFERENCE, UNVERIFIED -- needs a live PHI-free structural
capture session before production use)
--------------------------------------------------------------------------
No committed knowledge or documentation names a verified claims/remit-address
selector. The candidates below follow the same legacy ASPX naming convention
as the confirmed insurance-card fields (for example ``#txtPayerID`` and
``#txtCarrierCode``), but they have NOT been mapped live:

  #txtClaimsAddress1 / #txtClaimAddress1       (claims street line 1)
  #txtClaimsAddress2 / #txtClaimAddress2       (claims street line 2)
  #txtClaimsCity / #txtClaimCity               (claims city)
  #txtClaimsState / #txtClaimState             (claims state)
  #txtClaimsZip[Code] / #txtClaimZip[Code]     (claims postal code)

Generic selectors such as ``#txtAddress1`` are intentionally excluded: on a
patient insurance form they could identify the subscriber/patient address,
which is explicitly out of scope. If the claims-specific nodes are absent
after a bounded wait, the result is a clean unavailable shape. Only the fixed
``CLAIMS_ADDRESS_FIELDS`` whitelist leaves this module; no raw DOM text does.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("amd_portal_mcp")

# Architecture decision: this is an additive field group on the primary
# get_insurance_details tool, parallel to ELIGIBILITY_FIELDS. The carrier
# address belongs to the same selected coverage and reuses the already-open
# legacy frame without adding another browser entry point or permission.
CLAIMS_ADDRESS_FIELDS = [
    "claims_address_available",
    "claims_address_line1",
    "claims_address_line2",
    "claims_city",
    "claims_state",
    "claims_zip",
    "claims_carrier_name",
    "claims_payer_id",
    "claims_address_reason",
]

# Closed reasons for an unavailable address. The success shape uses "".
CLAIMS_ADDRESS_REASONS = frozenset(
    {
        "selectors_not_verified",
        "element_not_found",
        "carrier_detail_not_opened",
    }
)

# BEST-INFERENCE, UNVERIFIED -- needs a live PHI-free structural capture
# session before production use. Claims-specific names only; never generic
# address inputs that could be the patient/subscriber home address.
_SELECTORS = {
    "claims_address_line1": (
        "#txtClaimsAddress1",
        "#txtClaimAddress1",
    ),
    "claims_address_line2": (
        "#txtClaimsAddress2",
        "#txtClaimAddress2",
    ),
    "claims_city": (
        "#txtClaimsCity",
        "#txtClaimCity",
    ),
    "claims_state": (
        "#txtClaimsState",
        "#txtClaimState",
    ),
    "claims_zip": (
        "#txtClaimsZipCode",
        "#txtClaimZipCode",
        "#txtClaimsZip",
        "#txtClaimZip",
    ),
}

_REQUIRED_ADDRESS_FIELDS = (
    "claims_address_line1",
    "claims_city",
    "claims_state",
    "claims_zip",
)


def _empty_claims_address(reason: str) -> dict:
    """Return the exact unavailable result shape for a closed reason."""
    if reason not in CLAIMS_ADDRESS_REASONS:
        raise ValueError("unknown claims-address reason")
    result = {field: "" for field in CLAIMS_ADDRESS_FIELDS}
    result["claims_address_available"] = False
    result["claims_address_reason"] = reason
    return result


async def _selector_count(ins, selector: str) -> int:
    try:
        return int(await ins.locator(selector).count())
    except Exception:
        return 0


async def _first_present_selector(ins, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        if await _selector_count(ins, selector) > 0:
            return selector
    return None


async def _wait_for_any_address_selector(
    ins, *, attempts: int = 6, interval_s: float = 0.5
) -> bool:
    """Boundedly wait for any claims-specific address node to attach."""
    candidates = tuple(
        selector
        for field in _REQUIRED_ADDRESS_FIELDS
        for selector in _SELECTORS[field]
    )
    for attempt in range(attempts):
        for selector in candidates:
            if await _selector_count(ins, selector) > 0:
                return True
        if attempt + 1 < attempts:
            await asyncio.sleep(interval_s)
    return False


async def _input_value(ins, selector: str) -> str:
    try:
        return (await ins.locator(selector).first.input_value()).strip()
    except Exception:
        return ""


async def scrape_claims_address(ins) -> dict:
    """Passively read the bounded carrier claims-address whitelist.

    No clicks are performed. An unavailable or incomplete inferred selector
    set produces a clean all-empty address shape with a closed reason rather
    than partial values or an exception.
    """
    if not await _wait_for_any_address_selector(ins):
        result = _empty_claims_address("carrier_detail_not_opened")
        log.info(
            "flow=claims_address available=%s reason=%s",
            result["claims_address_available"],
            result["claims_address_reason"],
        )
        return result

    selected: dict[str, str] = {}
    for field, selectors in _SELECTORS.items():
        selector = await _first_present_selector(ins, selectors)
        if selector:
            selected[field] = selector

    if any(field not in selected for field in _REQUIRED_ADDRESS_FIELDS):
        result = _empty_claims_address("element_not_found")
        log.info(
            "flow=claims_address available=%s reason=%s",
            result["claims_address_available"],
            result["claims_address_reason"],
        )
        return result

    values = {
        field: await _input_value(ins, selector)
        for field, selector in selected.items()
    }
    if any(not values.get(field) for field in _REQUIRED_ADDRESS_FIELDS):
        result = _empty_claims_address("selectors_not_verified")
        log.info(
            "flow=claims_address available=%s reason=%s",
            result["claims_address_available"],
            result["claims_address_reason"],
        )
        return result

    # These two selectors are already confirmed on the selected legacy card.
    carrier_name = await _input_value(ins, "#ellCarrier input")
    payer_id = await _input_value(ins, "#txtPayerID")
    result = {field: "" for field in CLAIMS_ADDRESS_FIELDS}
    result.update(values)
    result["claims_address_available"] = True
    result["claims_carrier_name"] = carrier_name
    result["claims_payer_id"] = payer_id

    log.info(
        "flow=claims_address field presence: %s",
        {
            field: bool(result[field])
            for field in CLAIMS_ADDRESS_FIELDS
            if field not in ("claims_address_available", "claims_address_reason")
        },
    )
    return result
