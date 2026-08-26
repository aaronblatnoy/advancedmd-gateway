"""P0 scaffold: SPEC 19 config, SPEC 5.2/5.3/6.1/6.3 queues, the D-2 shim.

Later lanes deepen these in test_queues.py / test_clock.py / etc.; this
file pins the interfaces P0 freezes.
"""
from __future__ import annotations

import asyncio

import pytest

from gateway.client_shim import AMDClient, amd_date
from gateway.config import ConfigError, DEFAULTS, REQUIRED, load_config
from gateway.errors import ToolArgsInvalid
from gateway.interfaces import AUDIT_KEYS, Caller, RegistryEntry
from gateway.queues import (
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    EntryQueue,
    RequestQueue,
    XmlRequest,
)
from tests.conftest import sync_slot

# ------------------------------------------------------------- config


def test_defaults_match_spec_19(base_env):
    cfg = load_config(base_env)
    assert cfg.gateway_port == 8820
    assert cfg.clock_state_path == "/data/clock.json"
    assert cfg.clock_margin == 0.90
    assert cfg.execution_allowance_ms == 120000
    assert cfg.batch_aging_ms == 60000
    assert cfg.amd_post_timeout_s == 30
    assert cfg.login_check_cache_s == 300
    assert cfg.entry_queue_cap == 2000
    assert cfg.shutdown_drain_s == 30
    assert cfg.log_level == "INFO"
    assert cfg.write_tools_enabled is False
    assert cfg.amd_app_name == "TEMP"


@pytest.mark.parametrize("name", REQUIRED)
def test_missing_required_variable_fails_fast(base_env, name):
    """SPEC 16.1 step 1."""
    del base_env[name]
    with pytest.raises(ConfigError) as excinfo:
        load_config(base_env)
    assert name in str(excinfo.value)


def test_config_error_never_prints_a_secret(base_env):
    base_env["CLOCK_MARGIN"] = "nonsense"
    base_env["AMD_PASSWORD"] = "super-secret-placeholder"
    with pytest.raises(ConfigError) as excinfo:
        load_config(base_env)
    assert "super-secret-placeholder" not in str(excinfo.value)


def test_clock_margin_above_one_fails_startup(base_env):
    """SPEC 7.3: CLOCK_MARGIN MUST be <= 1.0."""
    base_env["CLOCK_MARGIN"] = "1.01"
    with pytest.raises(ConfigError):
        load_config(base_env)
    base_env["CLOCK_MARGIN"] = "1.0"
    assert load_config(base_env).clock_margin == 1.0


def test_config_is_frozen(config):
    with pytest.raises(Exception):
        config.clock_margin = 0.5  # type: ignore[misc]


def test_redacted_view_hides_credentials(config):
    view = config.redacted()
    assert view["amd_password"] == "<set>"
    assert config.amd_password not in str(view)


def test_env_example_covers_the_whole_spec_19_table():
    from pathlib import Path

    text = Path(__file__).resolve().parents[2].joinpath(".env.example").read_text()
    for name in list(REQUIRED) + list(DEFAULTS):
        assert f"{name}=" in text, f".env.example is missing {name}"
    # Placeholders only.
    assert "REPLACE_ME" in text


def test_config_does_not_hardcode_an_amd_url():
    """The AMD URL lives in session.py (SPEC 6.2/23.6), not in config."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2].joinpath("gateway/config.py").read_text()
    assert "advancedmd.com" not in src


# -------------------------------------------------------------- queues


async def test_entry_queue_orders_interactive_before_batch(entry_queue, make_record):
    batch = make_record(caller="b", priority=PRIORITY_BATCH)
    interactive = make_record(caller="i", priority=PRIORITY_INTERACTIVE)
    entry_queue.put_nowait(batch)
    entry_queue.put_nowait(interactive)
    assert entry_queue.depth == 2
    assert (await entry_queue.get()) is interactive
    assert (await entry_queue.get()) is batch
    assert entry_queue.depth == 0


async def test_entry_queue_is_fifo_within_a_priority(entry_queue, make_record, fake_clock):
    first = make_record(caller="a")
    fake_clock.advance(1)
    second = make_record(caller="a")
    entry_queue.put_nowait(second)
    entry_queue.put_nowait(first)
    assert (await entry_queue.get()) is first
    assert (await entry_queue.get()) is second


async def test_batch_is_promoted_after_batch_aging_ms(entry_queue, make_record, fake_clock):
    """SPEC 5.3: batch cannot starve."""
    batch = make_record(caller="b", priority=PRIORITY_BATCH)
    entry_queue.put_nowait(batch)
    fake_clock.advance_ms(60001)
    later_interactive = make_record(caller="i", priority=PRIORITY_INTERACTIVE)
    entry_queue.put_nowait(later_interactive)
    # The aged batch record arrived first, so once promoted it wins.
    assert (await entry_queue.get()) is batch


def test_effective_priority_uses_the_injected_clock(make_record, fake_clock):
    batch = make_record(priority=PRIORITY_BATCH)
    now = fake_clock()
    assert batch.effective_priority(now, 60000) == PRIORITY_BATCH
    assert batch.effective_priority(now + 61, 60000) == PRIORITY_INTERACTIVE


def test_per_caller_and_global_depth(entry_queue, make_record):
    for _ in range(3):
        entry_queue.put_nowait(make_record(caller="noisy", priority=PRIORITY_BATCH))
    entry_queue.put_nowait(make_record(caller="quiet"))
    assert entry_queue.depth == 4
    assert entry_queue.depth_for("noisy") == 3
    assert entry_queue.depth_for("quiet") == 1
    assert entry_queue.is_full() is False
    assert set(entry_queue.snapshot()) == {"depth", "oldest_wait_ms"}


async def test_entry_queue_get_waits_for_an_arrival(entry_queue, make_record):
    task = asyncio.ensure_future(entry_queue.get())
    await asyncio.sleep(0)
    assert not task.done()
    record = make_record()
    entry_queue.put_nowait(record)
    assert (await asyncio.wait_for(task, timeout=1)) is record


def test_record_wait_accounting(make_record, fake_clock):
    record = make_record(max_wait_ms=20000)
    assert record.wait_exceeded(fake_clock()) is False
    assert record.wait_exceeded(fake_clock() + 21) is True
    assert record.waited_ms(fake_clock() + 1) == pytest.approx(1000.0)


async def test_record_slot_is_a_future(make_record):
    record = make_record()
    assert isinstance(record.slot, asyncio.Future)
    record.slot.set_result({"ok": True})
    assert (await record.slot) == {"ok": True}


async def test_request_queue_orders_by_priority(request_queue):
    batch = XmlRequest(action="getdemographic", class_="demographics",
                       record_id="r1", priority=PRIORITY_BATCH)
    interactive = XmlRequest(action="getdemographic", class_="demographics",
                             record_id="r2", priority=PRIORITY_INTERACTIVE)
    request_queue.put_nowait(batch)
    request_queue.put_nowait(interactive)
    assert request_queue.depth == 2
    assert (await request_queue.get()) is interactive
    assert (await request_queue.get()) is batch


def test_xml_request_defaults():
    req = XmlRequest(action="getdemographic", class_="demographics",
                     record_id="r", priority=0, slot=sync_slot())
    assert req.retried_after_relogin is False
    assert req.attrs == {} and req.children == []
    assert req.tier == 3
    assert req.id and req.id != req.record_id


# --------------------------------------------------------- interfaces


def test_audit_key_allowlist_is_exactly_spec_17_2():
    assert AUDIT_KEYS == frozenset({
        "ts", "request_id", "caller", "tool", "priority", "outcome",
        "amd_calls", "amd_actions", "tier", "waited_ms", "elapsed_ms",
        "peak", "relogin",
    })
    for forbidden in ("args", "result", "results", "patient_id", "xml", "body"):
        assert forbidden not in AUDIT_KEYS


def test_registry_entry_resolves_canonical_name_first(registry_entry):
    assert registry_entry.names == ("amd_patients_get_demographic", "getdemographic")


def test_caller_is_frozen_and_defaults_to_deny_writes():
    caller = Caller(name="x", priority=PRIORITY_BATCH)
    assert caller.may_write == ()
    assert caller.is_revoked is False
    with pytest.raises(Exception):
        caller.phi = True  # type: ignore[misc]


def test_fake_token_table_default_denies(token_table, registry_entry):
    batch = token_table.lookup("test-batch-token")
    assert token_table.allows(batch, registry_entry) is True  # via alias, D-1
    other = RegistryEntry(name="amd_ehr_getehrnotes", domain="ehr", handler=None,
                          schema={}, aliases=("getehrnotes",))
    assert token_table.allows(batch, other) is False
    assert token_table.lookup("test-revoked-token") is None
    assert token_table.redact(batch) is False


# ------------------------------------------------------------ shim


def test_amd_date_is_unpadded():
    from datetime import date

    assert amd_date(date(2026, 1, 5)) == "1/5/2026"
    assert amd_date("1/5/2026") == "1/5/2026"


async def test_shim_call_builds_a_request_and_awaits_send(fake_send):
    client = AMDClient(fake_send, record_id="rec-1", priority=PRIORITY_BATCH)
    tree = await client.call("lookuppatient", "api", lastname="SYNTHETIC", extra=None)
    assert tree is fake_send.reply
    req = fake_send.sent[0]
    assert (req.action, req.class_, req.priority) == ("lookuppatient", "api", PRIORITY_BATCH)
    assert req.attrs == {"lastname": "SYNTHETIC"}  # None-valued attrs dropped
    assert req.record_id == "rec-1"
    assert client.amd_actions == ["lookuppatient"]


async def test_shim_get_patient_bundle_shape(amd_client, fake_send):
    await amd_client.get_patient_bundle(patient_id="12345")
    req = fake_send.sent[0]
    assert req.action == "getdemographic"
    assert req.class_ == "demographics"
    assert req.attrs == {"patientid": "12345"}


async def test_shim_refuses_chart_number_until_verification(amd_client):
    """Appendix C defect 2: the chart-number path is designed at
    verification time, not guessed here."""
    with pytest.raises(ToolArgsInvalid):
        await amd_client.get_patient_bundle(chart_number="X1")


async def test_shim_reminder_appts_shape(amd_client, fake_send):
    from datetime import date

    await amd_client.get_appointments_via_reminders(date(2026, 3, 4))
    req = fake_send.sent[0]
    assert req.action == "getreminderappts"
    assert req.attrs["startdate"] == "3/4/2026" == req.attrs["enddate"]
    assert req.attrs["starttime"] == "12:00 AM"
    assert req.attrs["endtime"] == "11:59 PM"
    # AMD's server requires apptstatus despite the docs calling it optional.
    assert req.attrs["apptstatus"] == "0,1,2,3,5,10,11,12"
    # Never a write: updconfirm modifies confirmation state.
    assert "updconfirm" not in req.attrs


async def test_shim_datevisits_children(amd_client, fake_send):
    from datetime import date

    await amd_client.get_visits_for_date(date(2026, 3, 4))
    req = fake_send.sent[0]
    assert req.action == "getdatevisits" and req.class_ == "api"
    assert [c.tag for c in req.children] == ["visit", "patient", "insurance"]
    # Attributes getdatevisits rejects with 'Invalid column name'.
    for rejected in ("profile", "providerid", "facilityid", "reason"):
        assert rejected not in req.children[0].attrib


def test_shim_holds_no_token_and_no_endpoint(amd_client):
    """The facade never sees the usercontext or the AMD URL (SPEC 6.2)."""
    assert not hasattr(amd_client, "usercontext")
    assert not hasattr(amd_client, "endpoint_url")


def test_a_non_https_amd_base_url_override_is_refused(base_env):
    """SPEC 17.4: an operator override must not downgrade AMD transport."""
    for value in (
        "http://partnerlogin.advancedmd.com/xmlrpc/processrequest.aspx",
        "ftp://partnerlogin.advancedmd.com/",
        "partnerlogin.advancedmd.com",
    ):
        with pytest.raises(ConfigError) as caught:
            load_config({**base_env, "AMD_BASE_URL": value})
        message = str(caught.value)
        assert "AMD_BASE_URL" in message
        # The error names the variable, never its value.
        assert value not in message


def test_an_https_amd_base_url_override_is_accepted(base_env):
    cfg = load_config({**base_env, "AMD_BASE_URL": "https://alt.advancedmd.com/x.aspx"})
    assert cfg.amd_base_url.startswith("https://")
