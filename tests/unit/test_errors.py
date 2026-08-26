"""SPEC 14: the error table, and the PHI rule on error messages."""
from __future__ import annotations

import pytest

from gateway import errors as E

# The SPEC 14 table, transcribed. If a row here disagrees with the code,
# the code is wrong.
SPEC_14 = [
    ("bad_request", 400, False, E.BadRequest),
    ("unauthorized", 401, False, E.Unauthorized),
    ("tool_unknown", 404, False, E.ToolUnknown),
    ("tool_unverified", 409, False, E.ToolUnverified),
    ("tool_forbidden", 403, False, E.ToolForbidden),
    ("tool_args_invalid", 422, False, E.ToolArgsInvalid),
    ("queue_full", 503, True, E.QueueFull),
    ("queue_wait_exceeded", 504, True, E.QueueWaitExceeded),
    ("connector_timeout", 504, True, E.ConnectorTimeout),
    ("amd_unavailable", 502, True, E.AmdUnavailable),
    ("amd_fault", 502, False, E.AmdFault),
    ("session_failed", 502, False, E.SessionFailed),
    ("login_bucket_wait", 503, True, E.LoginBucketWait),
    ("internal", 500, True, E.InternalError),
]


@pytest.mark.parametrize("code,status,retryable,cls", SPEC_14)
def test_every_row_of_the_table(code, status, retryable, cls):
    assert cls.code == code
    assert cls.http_status == status
    assert cls.retryable is retryable
    assert issubclass(cls, E.ConnectorError)


def test_table_is_complete_and_has_no_extras():
    assert {c.code for c in E.ERROR_CLASSES} == {row[0] for row in SPEC_14}
    assert len(E.ERROR_CLASSES) == len(SPEC_14)
    assert set(E.BY_CODE) == {row[0] for row in SPEC_14}


# --------------------------------------------------------------- PHI

# Values an error must never be able to carry. These are synthetic.
PHI_LIKE = [
    "patient_id=999999",
    "SYNTHETIC, PATIENT",
    "<PPMDResults><patient chart='X1'/></PPMDResults>",
    "1980-01-01",
]


def _instantiate(cls):
    if cls is E.AmdFault:
        return cls("1025", "Session has timed out")
    return cls()


@pytest.mark.parametrize("cls", E.ERROR_CLASSES, ids=lambda c: c.code)
def test_message_never_carries_caller_content(cls):
    """SPEC 14: error messages never include args, results, or AMD bodies.

    Structural, not stylistic: the constructors take no free text, so
    there is no parameter through which content could arrive.
    """
    err = _instantiate(cls)
    rendered = " ".join([str(err), repr(err), str(err.to_dict())])
    for phi in PHI_LIKE:
        assert phi not in rendered
    # Nothing dict-shaped or XML-shaped leaked in.
    assert "<" not in rendered
    assert "'args'" not in rendered and '"args"' not in rendered


@pytest.mark.parametrize("cls", E.ERROR_CLASSES, ids=lambda c: c.code)
def test_to_dict_is_the_spec_11_1_shape(cls):
    body = _instantiate(cls).to_dict()
    assert body["code"] == cls.code
    assert body["retryable"] is cls.retryable
    assert isinstance(body["message"], str) and body["message"]
    assert set(body) <= {"code", "message", "retryable", "amd_code", "retry_after_ms"}


def test_amd_fault_carries_code_and_short_description_only():
    err = E.AmdFault("1025", "Session has timed out")
    assert err.amd_code == "1025"
    assert "1025" in err.message
    assert err.to_dict()["amd_code"] == "1025"


def test_amd_fault_truncates_a_chatty_vendor_string():
    err = E.AmdFault("123", "x" * 5000)
    assert len(err.amd_message) == E.AmdFault.DESCRIPTION_MAX
    assert len(err.message) < 400


def test_login_bucket_wait_carries_only_a_pacing_hint():
    err = E.LoginBucketWait(retry_after_ms=45000)
    assert err.to_dict()["retry_after_ms"] == 45000


# ------------------------------------------------------------ mapping


def test_map_passes_connector_errors_through():
    original = E.ToolForbidden()
    assert E.map_to_connector_error(original) is original


def test_map_timeout():
    assert isinstance(E.map_to_connector_error(TimeoutError()), E.ConnectorTimeout)


def test_map_type_error_is_args_invalid():
    # Appendix C defect 1: copied handlers raise TypeError before any XML
    # is built, so no AMD call was spent.
    err = E.map_to_connector_error(TypeError("call() missing 1 required argument"))
    assert isinstance(err, E.ToolArgsInvalid)


def test_map_transport_failure():
    class ConnectError(Exception):
        pass

    assert isinstance(E.map_to_connector_error(ConnectError()), E.AmdUnavailable)
    assert isinstance(E.map_to_connector_error(OSError()), E.AmdUnavailable)


def test_map_unknown_exception_is_internal():
    class Weird(Exception):
        pass

    assert isinstance(E.map_to_connector_error(Weird()), E.InternalError)


def test_mapping_does_not_copy_the_exception_text():
    """A raised exception may carry an args repr. None of it may survive."""
    err = E.map_to_connector_error(ValueError("patient_id=999999 chart=X1"))
    assert "999999" not in str(err)
