"""Tests for the passive carrier claims-address scrape.

No real browser, no network, and no carrier-detail click: a synthetic legacy
insurance frame supplies only the inferred claims-specific inputs.
"""
from __future__ import annotations

import logging

import pytest

from amd_mcp_common.redact import Redactor
from portal.flows import claims_address
from portal.flows import insurance


class _Loc:
    def __init__(self, count=0, value=""):
        self._count = count
        self._value = value
        self.first = self

    async def count(self):
        return self._count

    async def input_value(self):
        return self._value


class _LegacyFrame:
    def __init__(self, values=None):
        self._values = values or {}

    def locator(self, selector):
        if selector in self._values:
            return _Loc(count=1, value=self._values[selector])
        return _Loc()


@pytest.mark.asyncio
async def test_data_present_returns_exact_whitelisted_claims_fields(caplog):
    street = "123 Synthetic Claims Road"
    frame = _LegacyFrame(
        {
            "#txtClaimsAddress1": street,
            "#txtClaimsAddress2": "Suite 400",
            "#txtClaimsCity": "Exampleville",
            "#txtClaimsState": "FL",
            "#txtClaimsZipCode": "32000",
            "#ellCarrier input": "Synthetic Carrier",
            "#txtPayerID": "PAYER000",
        }
    )

    with caplog.at_level(logging.INFO, logger="amd_portal_mcp"):
        out = await claims_address.scrape_claims_address(frame)

    assert out == {
        "claims_address_available": True,
        "claims_address_line1": street,
        "claims_address_line2": "Suite 400",
        "claims_city": "Exampleville",
        "claims_state": "FL",
        "claims_zip": "32000",
        "claims_carrier_name": "Synthetic Carrier",
        "claims_payer_id": "PAYER000",
        "claims_address_reason": "",
    }
    assert set(out) == set(claims_address.CLAIMS_ADDRESS_FIELDS)
    # Presence booleans may be logged; scraped values may not.
    assert street not in caplog.text
    assert "Synthetic Carrier" not in caplog.text
    for record in caplog.records:
        assert street not in str(record.msg)
        assert street not in str(record.args)


@pytest.mark.asyncio
async def test_absent_selectors_return_clean_carrier_detail_fallback(monkeypatch):
    async def _no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(claims_address.asyncio, "sleep", _no_sleep)
    out = await claims_address.scrape_claims_address(_LegacyFrame())

    assert out["claims_address_available"] is False
    assert out["claims_address_reason"] == "carrier_detail_not_opened"
    assert set(out) == set(claims_address.CLAIMS_ADDRESS_FIELDS)
    assert all(
        out[field] == ""
        for field in claims_address.CLAIMS_ADDRESS_FIELDS
        if field not in ("claims_address_available", "claims_address_reason")
    )


@pytest.mark.asyncio
async def test_incomplete_selector_set_returns_no_partial_address():
    out = await claims_address.scrape_claims_address(
        _LegacyFrame(
            {
                "#txtClaimAddress1": "456 Synthetic Remit Avenue",
                "#txtClaimCity": "Exampleville",
            }
        )
    )

    assert out["claims_address_available"] is False
    assert out["claims_address_reason"] == "element_not_found"
    assert out["claims_address_line1"] == ""
    assert out["claims_city"] == ""


def test_claims_address_whitelists_and_reasons_are_closed():
    assert claims_address.CLAIMS_ADDRESS_FIELDS == [
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
    assert claims_address.CLAIMS_ADDRESS_REASONS == {
        "selectors_not_verified",
        "element_not_found",
        "carrier_detail_not_opened",
    }


def test_batch_whitelist_keeps_claims_fields_and_drops_unknown_content():
    item = insurance._whitelist_item(
        {
            "claims_address_available": True,
            "claims_address_line1": "123 Synthetic Claims Road",
            "RAW_DOM_TEXT": "must not leave the flow",
        },
        insurance._BATCH_OK_KEYS,
    )
    assert item == {
        "claims_address_available": True,
        "claims_address_line1": "123 Synthetic Claims Road",
    }


def test_shared_redactor_fail_closes_claims_values_if_they_reach_it():
    payload = {
        "claims_address_available": True,
        "claims_address_line1": "123 Synthetic Claims Road",
        "claims_city": "Exampleville",
        "claims_state": "FL",
        "claims_zip": "32000",
        "claims_carrier_name": "Synthetic Carrier",
        "claims_payer_id": "PAYER000",
        "claims_address_reason": "",
    }
    out = Redactor().apply(
        payload,
        allow_phi=False,
        hash_key=b"synthetic-claims-test-key",
    )

    for field in (
        "claims_address_line1",
        "claims_city",
        "claims_state",
        "claims_zip",
        "claims_carrier_name",
        "claims_payer_id",
    ):
        assert out[field] == "<REDACTED>"
    assert out["claims_address_available"] is True
    assert out["claims_address_reason"] == ""
