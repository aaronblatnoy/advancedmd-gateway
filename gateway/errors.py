"""Error codes, SPEC 14.

One class per row of the SPEC 14 table. Every class carries `code`,
`http_status` and `retryable`, so the HTTP layer, the MCP layer and the
SDK all read the same three fields and nobody re-derives a mapping.

PHI RULE (SPEC 14, 17.1): an error message MUST NEVER contain tool args,
result content, or an AMD response body. That is enforced structurally
here, not by discipline: ConnectorError takes no free-text message at
all. Each class has a fixed, constant message. The single exception is
AmdFault, which carries AMD's numeric fault code plus AMD's own short
fault description -- and nothing else.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "ConnectorError",
    "BadRequest",
    "Unauthorized",
    "ToolUnknown",
    "ToolUnverified",
    "ToolForbidden",
    "ToolArgsInvalid",
    "QueueFull",
    "QueueWaitExceeded",
    "ConnectorTimeout",
    "AmdUnavailable",
    "AmdFault",
    "SessionFailed",
    "LoginBucketWait",
    "InternalError",
    "ERROR_CLASSES",
    "BY_CODE",
    "map_to_connector_error",
]


class ConnectorError(Exception):
    """Base of the SPEC 14 hierarchy.

    Subclasses set the three class attributes below. Instances take no
    caller-supplied text: `str(err)` is always the constant MESSAGE.
    """

    code: str = "internal"
    http_status: int = 500
    retryable: bool = True
    MESSAGE: str = "unexpected connector error"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)

    @property
    def message(self) -> str:
        return self.MESSAGE

    def to_dict(self) -> dict[str, Any]:
        """The SPEC 11.1 error object. PHI-free by construction."""
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class BadRequest(ConnectorError):
    code = "bad_request"
    http_status = 400
    retryable = False
    MESSAGE = "malformed request body"


class Unauthorized(ConnectorError):
    code = "unauthorized"
    http_status = 401
    retryable = False
    MESSAGE = "unknown or revoked token"


class ToolUnknown(ConnectorError):
    code = "tool_unknown"
    http_status = 404
    retryable = False
    MESSAGE = "no such tool"


class ToolUnverified(ConnectorError):
    code = "tool_unverified"
    http_status = 409
    retryable = False
    MESSAGE = "tool exists but is not yet verified"


class ToolForbidden(ConnectorError):
    code = "tool_forbidden"
    http_status = 403
    retryable = False
    MESSAGE = "caller policy denies this tool"


class ToolArgsInvalid(ConnectorError):
    code = "tool_args_invalid"
    http_status = 422
    retryable = False
    # Deliberately does not name the offending argument or its value.
    MESSAGE = "arguments do not match the tool schema"


class QueueFull(ConnectorError):
    code = "queue_full"
    http_status = 503
    retryable = True
    MESSAGE = "queue cap reached"


class QueueWaitExceeded(ConnectorError):
    code = "queue_wait_exceeded"
    http_status = 504
    retryable = True
    MESSAGE = "max_wait_ms elapsed before the tool started"


class ConnectorTimeout(ConnectorError):
    code = "connector_timeout"
    http_status = 504
    retryable = True
    MESSAGE = "receiver gave up waiting for the result"


class AmdUnavailable(ConnectorError):
    code = "amd_unavailable"
    http_status = 502
    retryable = True
    MESSAGE = "AdvancedMD unreachable after retries"


class SessionFailed(ConnectorError):
    code = "session_failed"
    http_status = 502
    retryable = False
    MESSAGE = "AdvancedMD session could not be established"


class LoginBucketWait(ConnectorError):
    code = "login_bucket_wait"
    http_status = 503
    retryable = True
    MESSAGE = "login rate bucket is full"

    def __init__(self, retry_after_ms: int | None = None) -> None:
        super().__init__()
        # A pacing hint only; carries no request content.
        self.retry_after_ms = int(retry_after_ms) if retry_after_ms else None

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        if self.retry_after_ms is not None:
            out["retry_after_ms"] = self.retry_after_ms
        return out


class InternalError(ConnectorError):
    code = "internal"
    http_status = 500
    retryable = True
    MESSAGE = "unexpected connector error"


class AmdFault(ConnectorError):
    """AdvancedMD returned success="0" (SPEC 6.4).

    Carries AMD's fault code and AMD's own short fault description. It
    MUST NOT be constructed from a response body, an args dict, or any
    element text: pass the parsed code and the parsed short description
    only. Descriptions are truncated so a chatty vendor string can never
    become a body dump.
    """

    code = "amd_fault"
    http_status = 502
    retryable = False
    MESSAGE = "AdvancedMD returned a fault"
    DESCRIPTION_MAX = 200

    def __init__(self, amd_code: str | None, message: str | None = None) -> None:
        super().__init__()
        self.amd_code = str(amd_code) if amd_code is not None else None
        desc = (message or "").strip()
        self.amd_message: str | None = desc[: self.DESCRIPTION_MAX] or None

    @property
    def message(self) -> str:
        if self.amd_code and self.amd_message:
            return f"{self.MESSAGE} {self.amd_code}: {self.amd_message}"
        if self.amd_code:
            return f"{self.MESSAGE} {self.amd_code}"
        return self.MESSAGE

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out["amd_code"] = self.amd_code
        return out


#: Every concrete SPEC 14 class, in table order.
ERROR_CLASSES: tuple[type[ConnectorError], ...] = (
    BadRequest,
    Unauthorized,
    ToolUnknown,
    ToolUnverified,
    ToolForbidden,
    ToolArgsInvalid,
    QueueFull,
    QueueWaitExceeded,
    ConnectorTimeout,
    AmdUnavailable,
    AmdFault,
    SessionFailed,
    LoginBucketWait,
    InternalError,
)

BY_CODE: dict[str, type[ConnectorError]] = {c.code: c for c in ERROR_CLASSES}

# Exception type names that mean "the transport to AMD failed". Matched by
# name rather than by import so that errors.py imports no HTTP library
# (SPEC 6.2 / 23.6).
_TRANSPORT_EXC_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "TransportError",
        "NetworkError",
        "ConnectionError",
        "ConnectionResetError",
        "OSError",
    }
)


def map_to_connector_error(exc: BaseException) -> ConnectorError:
    """Translate any exception escaping a handler into a SPEC 14 error.

    Used by the worker loop (SPEC 5.4). MUST NOT read str(exc) into the
    returned error: the mapping is by type only, so nothing an exception
    happens to carry (an args repr, an XML body) can reach a caller.
    """
    if isinstance(exc, ConnectorError):
        return exc
    if isinstance(exc, TimeoutError):  # asyncio.TimeoutError is this since 3.11
        return ConnectorTimeout()
    if isinstance(exc, (TypeError, ValueError)):
        # Copied handlers raise TypeError on bad attribute names before any
        # XML is built (SPEC Appendix C defect 1). No AMD call was spent.
        return ToolArgsInvalid()
    names = {t.__name__ for t in type(exc).__mro__}
    if names & _TRANSPORT_EXC_NAMES:
        return AmdUnavailable()
    return InternalError()
