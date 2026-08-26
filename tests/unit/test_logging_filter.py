"""SPEC 17.3: one filter, the length rule, the key rule, the httpx pin."""
from __future__ import annotations

import io
import logging

import pytest

from gateway.logging_filter import (
    MAX_VALUE_CHARS,
    PINNED_WARNING_LOGGERS,
    REDACTED_KEYS,
    REDACTION,
    RedactingFilter,
    configure,
    redact_value,
)

LONG = "x" * (MAX_VALUE_CHARS + 1)
# Synthetic, body-shaped strings. No PHI.
XML_BODY = "<PPMDResults><Results success=\"1\">" + "<row/>" * 200 + "</Results></PPMDResults>"


@pytest.fixture
def captured():
    """A root logger wired through configure(), capturing to a buffer."""
    stream = io.StringIO()
    saved_handlers = logging.getLogger().handlers[:]
    saved_level = logging.getLogger().level
    log_filter = configure("DEBUG", stream=stream)
    yield stream, log_filter
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


# --------------------------------------------------------- the values


def test_spec_17_3_key_set():
    assert REDACTED_KEYS == {"password", "token", "usercontext", "result", "args"}


def test_the_length_rule_is_200():
    assert MAX_VALUE_CHARS == 200
    assert redact_value("x" * 200) == "x" * 200
    assert redact_value("x" * 201) == REDACTION


@pytest.mark.parametrize("key", sorted(REDACTED_KEYS))
def test_each_named_key_is_redacted_whatever_its_length(key):
    assert redact_value("short", key) == REDACTION
    assert redact_value({key: "short"})[key] == REDACTION
    # Case and whitespace do not get you past it.
    assert redact_value("short", key.upper()) == REDACTION
    assert redact_value("short", f" {key} ") == REDACTION


def test_nested_bodies_are_redacted():
    value = {"meta": {"tier": 2}, "payload": {"result": {"a": 1}, "xml": XML_BODY}}
    out = redact_value(value)
    assert out["meta"]["tier"] == 2
    assert out["payload"]["result"] == REDACTION
    assert out["payload"]["xml"] == REDACTION


def test_numbers_and_short_strings_survive():
    assert redact_value(42) == 42
    assert redact_value(True) is True
    assert redact_value(None) is None
    assert redact_value("getdemographic") == "getdemographic"


# ---------------------------------------------------------- the filter


def test_a_long_message_is_redacted(captured):
    stream, _ = captured
    logging.getLogger("gateway.test").info(XML_BODY)
    out = stream.getvalue()
    assert REDACTION in out
    assert "PPMDResults" not in out


def test_a_long_format_argument_cannot_hide_behind_a_placeholder(captured):
    stream, _ = captured
    logging.getLogger("gateway.test").info("amd replied %s", XML_BODY)
    out = stream.getvalue()
    assert "PPMDResults" not in out
    assert REDACTION in out


def test_a_short_message_survives(captured):
    stream, _ = captured
    logging.getLogger("gateway.test").info("session established")
    assert "session established" in stream.getvalue()


# "args" and "message" are reserved by stdlib logging and cannot be passed
# through extra=; they are covered by the format-argument tests below.
@pytest.mark.parametrize("key", sorted(REDACTED_KEYS - {"args"}))
def test_a_named_key_in_extra_is_redacted(key, captured):
    stream, _ = captured
    logging.getLogger("gateway.test").info(
        "tool finished", extra={key: "supersecretvalue"}
    )
    assert "supersecretvalue" not in stream.getvalue()


def test_a_dict_argument_is_walked(captured):
    stream, _ = captured
    logging.getLogger("gateway.test").info(
        "call %(tool)s", {"tool": "getdemographic", "args": {"patient_id": "999999"}}
    )
    out = stream.getvalue()
    assert "999999" not in out


def test_debug_level_still_does_not_log_bodies(captured):
    """SPEC 17.3: even at DEBUG, bodies are not logged."""
    stream, _ = captured
    logging.getLogger("gateway.test").debug("request body: %s", XML_BODY)
    assert "PPMDResults" not in stream.getvalue()


def test_the_filter_returns_true_so_nothing_is_dropped():
    record = logging.makeLogRecord({"msg": "hello", "args": ()})
    assert RedactingFilter().filter(record) is True


# ------------------------------------------------------- the wiring


def test_there_is_exactly_one_handler_and_one_filter(captured):
    _, log_filter = captured
    root = logging.getLogger()
    # pytest attaches its own capture handlers; ours is the only one the
    # gateway installs, and it carries exactly one filter.
    ours = [h for h in root.handlers if log_filter in h.filters]
    assert len(ours) == 1
    assert ours[0].filters == [log_filter]


def test_uvicorns_own_handlers_are_filtered_too():
    """SPEC 17.3: a logger with propagate=False is a second path out."""
    import logging.config

    from uvicorn.config import LOGGING_CONFIG

    saved_handlers = logging.getLogger().handlers[:]
    saved_level = logging.getLogger().level
    try:
        logging.config.dictConfig(LOGGING_CONFIG)
        log_filter = configure("INFO", stream=io.StringIO())
        reachable = []
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            reachable.extend(logging.getLogger(name).handlers)
        assert reachable, "uvicorn installed no handlers; the test proves nothing"
        for handler in reachable:
            assert log_filter in handler.filters
        for name, logger in list(logging.Logger.manager.loggerDict.items()):
            if isinstance(logger, logging.Logger) and not logger.propagate:
                for handler in logger.handlers:
                    assert log_filter in handler.filters, name
    finally:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            logger.handlers = []
            logger.propagate = True


@pytest.mark.parametrize("name", PINNED_WARNING_LOGGERS)
def test_http_libraries_are_pinned_to_warning(name, captured):
    assert logging.getLogger(name).level == logging.WARNING
    assert "httpx" in PINNED_WARNING_LOGGERS


def test_a_pinned_logger_cannot_emit_an_info_body(captured):
    stream, _ = captured
    logging.getLogger("httpx").info("HTTP Request body: %s", XML_BODY)
    assert stream.getvalue() == ""


def test_configure_sets_the_requested_level(captured):
    configure("WARNING", stream=io.StringIO())
    assert logging.getLogger().level == logging.WARNING
    configure("INFO", stream=io.StringIO())
    assert logging.getLogger().level == logging.INFO


# --------------------------------------------------- exception state


IDENTITY_SHAPED = "patient Aaaa Bbbb dob 1/1/1900 chart AB1234"


def test_a_traceback_never_reaches_the_stream(captured):
    """SPEC 17.3: the Formatter appends exc_info AFTER the filter runs."""
    stream, _ = captured
    log = logging.getLogger("gateway.test.exc")
    payload = "z" * 400
    try:
        raise ValueError(f"{IDENTITY_SHAPED} {payload}")
    except ValueError:
        log.exception("unhandled receiver exception")
    written = stream.getvalue()
    assert payload not in written
    assert IDENTITY_SHAPED not in written
    assert "Traceback" not in written
    assert "ValueError" in written


def test_a_chained_exception_reports_class_names_only(captured):
    stream, _ = captured
    log = logging.getLogger("gateway.test.exc")
    try:
        try:
            raise KeyError(IDENTITY_SHAPED)
        except KeyError as exc:
            raise RuntimeError("y" * 400) from exc
    except RuntimeError:
        log.exception("sender loop failed")
    written = stream.getvalue()
    assert IDENTITY_SHAPED not in written
    assert "y" * 400 not in written
    assert "RuntimeError" in written and "KeyError" in written


def test_exc_info_is_cleared_off_the_record():
    record = logging.makeLogRecord({"msg": "boom", "args": ()})
    try:
        raise ValueError(IDENTITY_SHAPED)
    except ValueError:
        import sys

        record.exc_info = sys.exc_info()
    RedactingFilter().filter(record)
    assert record.exc_info is None
    assert record.exc_text is None
    assert IDENTITY_SHAPED not in record.getMessage()
