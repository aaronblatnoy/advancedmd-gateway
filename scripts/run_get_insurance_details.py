#!/usr/bin/env python3
"""One-shot: run get_insurance_details for a test patient (operator use)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portal import browser
from portal.flows import insurance
from portal.flows._runner import run_flow


async def main() -> int:
    patient = os.environ.get("PORTAL_TEST_PATIENT", "Blatnoy, Aaron")
    index = int(os.environ.get("PORTAL_TEST_INSURANCE_INDEX", "1"))

    for key in ("AMD_USERNAME", "AMD_PASSWORD", "AMD_OFFICE_KEY"):
        if not os.environ.get(key):
            print(f"missing env {key}", file=sys.stderr)
            return 2

    os.environ.setdefault("PORTAL_RECOVERY_ENABLED", "1")
    os.environ.setdefault(
        "PORTAL_LLM_BASE_URL", "http://100.94.62.115:8000"
    )
    # Headed helps first-run login debugging; set AMD_PORTAL_HEADLESS=0 to watch.
    os.environ.setdefault("AMD_PORTAL_HEADLESS", "1")

    page = await browser.get_page()
    result = await run_flow(
        "get_insurance_details",
        insurance.get_insurance_details,
        page,
        patient=patient,
        insurance_index=index,
    )
    print(json.dumps(result, indent=2))
    await browser.shutdown()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
