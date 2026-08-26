#!/usr/bin/env python3
"""
================================================================================
OPERATOR-ONLY. THIS SCRIPT TALKS TO ADVANCEDMD.

  * It requires REAL AdvancedMD credentials.
  * It is the ONLY file in this repository permitted to contact AMD.
  * Run it ONLY on black-sky, ONLY by the operator, ONLY by hand.
  * NO agent, workflow, test, or CI job may run it. Agents read committed
    fixtures and nothing else; an agent task that needs a NEW recording
    STOPS and asks the operator (SPEC 23.3 step 4).
  * Use a synthetic or consented test patient. Never a real patient of
    convenience.
  * The scrubbed output is reviewed BY A HUMAN on the box before it is
    committed (SPEC 23.3 step 3). The scrubber is a safety net, not a
    guarantee: read the file.
================================================================================

Procedure (SPEC 23.3):

  1. Post one request for one tool.
  2. Save the request XML as-is (it contains no PHI beyond the id you
     passed, which is the id of a test patient).
  3. Pass the reply through the scrubber below, which replaces EVERY
     attribute value and EVERY text node with a deterministic synthetic
     value, exempting only the structural allowlist (STRUCTURAL_ATTRS),
     and preserving structure and id formats.
  4. Operator reviews and commits.

Usage:

    export AMD_USERNAME=... AMD_PASSWORD=... AMD_OFFICE_KEY=...
    python scripts/record_fixture.py \
        --action getdemographic --class demographics \
        --attr patientid=<test patient id> \
        --out tests/fixtures

Add --dry-run to build and print the request XML without contacting AMD.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

BANNER = """
  advancedmd-gateway fixture recorder
  THIS CONTACTS ADVANCEDMD WITH REAL CREDENTIALS.
  Operator-only, on the box, by hand. Ctrl-C now if that is not you.
"""

PROVENANCE = (
    "recorded by scripts/record_fixture.py and scrubbed against the "
    "structural allowlist; reviewed by the operator before commit; "
    "contains no real patient data"
)

# SPEC 23.3 step 2: the STRUCTURAL ALLOWLIST. This is the ONLY set of
# attribute names whose value is allowed to survive a recording. Every
# other attribute value, and every non-empty text node, is replaced with
# a deterministic synthetic value -- whether or not this file anticipated
# the name. A denylist cannot be safe here: AMD adds fields we have never
# seen, and any one of them may be free prose about a patient.
#
# Membership rule: a name belongs here only if the PARSER needs its real
# value and the value cannot describe a person.
STRUCTURAL_ATTRS = frozenset({
    "success",
    "class",
    "action",
    "count",
    "recordcount",
    "totalcount",
    "pagecount",
    "page",
    "index",
    "code",
    "faultcode",
    "type",
    "status",
    "version",
    "encoding",
})

# Attribute names whose value must keep its FORMAT (the parser asserts on
# the shape) but must not keep its content. These get a shape-preserving
# synthetic id, not the real one.
ID_ATTRS_KEEP_FORMAT = frozenset({
    "id",
    "patientid",
    "visitid",
    "chartnumber",
    "carrierid",
    "providerid",
    "facilityid",
    "profileid",
    "insuranceid",
    "chargeid",
    "noteid",
    "appointmentid",
})

# Deterministic synthetic replacements, chosen to preserve shape.
SYNTHETIC_TOKENS = [
    "SYNTHETIC-ALPHA", "SYNTHETIC-BRAVO", "SYNTHETIC-CHARLIE",
    "SYNTHETIC-DELTA", "SYNTHETIC-ECHO", "SYNTHETIC-FOXTROT",
]

SYNTHETIC_TEXT = "SYNTHETIC TEXT REMOVED BY record_fixture.py"


def _shape_preserving(value: str, digest: str) -> str:
    """Same length, same digit/letter/punctuation positions, new content."""
    out = []
    for i, ch in enumerate(value):
        if ch.isdigit():
            d = digest[i % len(digest)]
            out.append(d if d.isdigit() else "0")
        elif ch.isalpha():
            out.append("X" if ch.isupper() else "x")
        else:
            out.append(ch)
    return "".join(out)


def _synthetic_for(attr: str, value: str) -> str:
    """A stable synthetic stand-in that preserves the value's shape.

    Same input -> same output within a run and across runs, so a fixture
    stays diff-stable, but the mapping is one-way (sha256, no table kept).
    """
    if not value:
        return value
    digest = hashlib.sha256(f"{attr}|{value}".encode("utf-8")).hexdigest()
    lowered = attr.lower()
    if "dob" in lowered or "birth" in lowered:
        return "1/1/1970"
    if "phone" in lowered or "fax" in lowered or "cell" in lowered:
        return "555-0100"
    if "ssn" in lowered:
        return "000-00-0000"
    if "email" in lowered:
        return f"synthetic{digest[:6]}@example.invalid"
    if "zip" in lowered:
        return "00000"
    if lowered in ID_ATTRS_KEEP_FORMAT or "chart" in lowered or lowered.endswith("id"):
        return _shape_preserving(value, digest)
    return SYNTHETIC_TOKENS[int(digest[:4], 16) % len(SYNTHETIC_TOKENS)]


def scrub(tree: etree._Element) -> etree._Element:
    """Replace EVERY attribute value and EVERY non-empty text node.

    This is an allowlist, not a denylist: a value survives only if its
    attribute name is in STRUCTURAL_ATTRS. Anything else -- a name we
    have seen, a name we have not, a memo, a carrier field, free prose --
    is replaced. Tag names, attribute names and structure survive, so the
    fixture still exercises the parser.
    """
    for element in tree.iter():
        for attr, value in list(element.attrib.items()):
            if str(attr).lower() in STRUCTURAL_ATTRS:
                continue
            element.set(attr, _synthetic_for(attr, value))
        if element.text and element.text.strip():
            element.text = SYNTHETIC_TEXT
        if element.tail and element.tail.strip():
            element.tail = SYNTHETIC_TEXT
    return tree


def build_request(action: str, class_: str, attrs: dict[str, str],
                  usercontext: str | None) -> bytes:
    root = etree.Element("ppmdmsg", action=action, **{"class": class_})
    root.set("msgtime", datetime.now(timezone.utc).strftime("%m/%d/%Y %H:%M:%S"))
    root.set("nocookie", "1")
    for key, value in attrs.items():
        root.set(key, value)
    if usercontext:
        # AMD requires the token INSIDE a <usercontext> child element, not
        # as an attribute (HTTP 400 "Improperly Formatted Token").
        child = etree.SubElement(root, "usercontext")
        child.text = usercontext
    return etree.tostring(root, encoding="ISO-8859-1", xml_declaration=True)


def main(argv: list[str] | None = None) -> int:
    print(BANNER, file=sys.stderr)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", required=True)
    parser.add_argument("--class", dest="class_", required=True)
    parser.add_argument("--attr", action="append", default=[],
                        metavar="name=value", help="repeatable AMD attribute")
    parser.add_argument("--out", default="tests/fixtures", type=Path)
    parser.add_argument("--name", default=None,
                        help="fixture basename; defaults to the action")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and print the request; contact nobody")
    parser.add_argument("--yes", action="store_true",
                        help="skip the interactive confirmation")
    args = parser.parse_args(argv)

    attrs: dict[str, str] = {}
    for item in args.attr:
        if "=" not in item:
            parser.error(f"--attr expects name=value, got {item!r}")
        key, _, value = item.partition("=")
        attrs[key] = value

    basename = args.name or args.action
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        body = build_request(args.action, args.class_, attrs, usercontext=None)
        print(body.decode("ISO-8859-1"))
        return 0

    for name in ("AMD_USERNAME", "AMD_PASSWORD", "AMD_OFFICE_KEY"):
        if not os.environ.get(name):
            print(f"error: {name} is not set. This script needs real "
                  "credentials and must be run by the operator on the box.",
                  file=sys.stderr)
            return 2

    if not args.yes:
        answer = input("Type RECORD to contact AdvancedMD: ").strip()
        if answer != "RECORD":
            print("aborted", file=sys.stderr)
            return 1

    # ---------------------------------------------------------------
    # The live call. Deliberately not implemented against a vendored
    # client: the operator runs this inside the gateway image, where
    # gateway.session and gateway.sender already hold the one login
    # and the one clock. Wiring it is the verification lane's job
    # (SPEC 9.3 step 2), so that a recording consumes exactly one clocked
    # AMD call and appears in the audit like any other.
    # ---------------------------------------------------------------
    try:
        import asyncio

        from gateway.config import load_config
        from gateway.queues import XmlRequest
        from gateway.sender import send  # type: ignore[attr-defined]
        from gateway.session import AmdSession  # type: ignore[attr-defined]
    except ImportError as exc:
        print(f"error: the gateway runtime is not available here ({exc.name}). "
              "Run this inside the gateway image, on black-sky.",
              file=sys.stderr)
        return 3

    async def _run() -> bytes:
        config = load_config()
        session = AmdSession(config)
        await session.login()
        request = XmlRequest(action=args.action, class_=args.class_,
                             record_id="fixture-recording", priority=0,
                             attrs=attrs)
        reply = await send(request)
        return etree.tostring(reply)

    raw = asyncio.run(_run())

    request_path = out_dir / f"{basename}.request.xml"
    reply_path = out_dir / f"{basename}.reply.xml"

    request_path.write_bytes(
        build_request(args.action, args.class_, attrs, usercontext=None)
    )
    scrubbed = scrub(etree.fromstring(raw))
    document = etree.ElementTree(scrubbed)
    document.getroot().addprevious(etree.Comment(f" {PROVENANCE} "))
    document.write(str(reply_path), encoding="utf-8", xml_declaration=True,
                   pretty_print=True)

    print(f"wrote {request_path}")
    print(f"wrote {reply_path}")
    print("\nNOW: read the scrubbed reply yourself before committing it. "
          "The scrubber is a safety net, not a guarantee.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
