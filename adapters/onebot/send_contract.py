"""OneBot outbound send contract: QQ target identities and NapCat failure fingerprints.

Two-phase idempotency, message plans and dead letters live in adapters._shared.send_guard;
this module binds the QQ-specific pieces into it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from adapters._shared.send_guard import (
    AmbiguousAckError,
    IdempotencyClaim,
    SendContractError,
    SendError,
    SendNotStartedError,
    TwoPhaseIdempotencyStore,
    idempotency_key,
    protected_claim_send as _protected_claim_send,
    target_key_of,
)
from platforms.ids import ID_BODY, normalize_id


def validate_send_request(bot: Any, target: Mapping[str, Any] | None) -> str:
    """Validate mandatory routing inputs and return the normalized target identity."""
    if bot is None:
        raise SendContractError("OneBot send missing bot")
    if target is None:
        raise SendContractError("OneBot send missing target")
    return target_identity(target)


# NapCat reports unrelated failures with the same retcode, so only message/wording text separates them.
_AMBIGUOUS_TIMEOUT_HINTS = ("sendmsg", "call api", "networkerror", "network error")
_TEMP_FILE_HINTS = ("enoent", "no such file")
# NapCat fails at message assembly time when an at segment's uid cannot be resolved.
_AT_UID_HINTS = ("get uid", "uid error", "getuiderror")
_NOT_STARTED_HINTS = _TEMP_FILE_HINTS + _AT_UID_HINTS + ("connection refused",)


def send_error_text(exc: BaseException) -> str:
    return f"{getattr(exc, 'message', '') or ''} {getattr(exc, 'wording', '') or ''} {exc}".lower()


def is_temp_file_missing(exc: BaseException) -> bool:
    return any(hint in send_error_text(exc) for hint in _TEMP_FILE_HINTS)


def is_at_uid_failure(exc: BaseException) -> bool:
    return any(hint in send_error_text(exc) for hint in _AT_UID_HINTS)


def classify_send_exception(exc: BaseException) -> SendError:
    """Classify known OneBot/NapCat failures by retry safety."""
    if isinstance(exc, SendError):
        return exc
    text = send_error_text(exc)
    if "timeout" in text and any(hint in text for hint in _AMBIGUOUS_TIMEOUT_HINTS):
        return AmbiguousAckError(
            f"OneBot send acknowledgement is ambiguous: {exc}", cause=exc,
        )
    if isinstance(exc, ConnectionRefusedError) or any(hint in text for hint in _NOT_STARTED_HINTS):
        return SendNotStartedError(
            f"OneBot send did not reach the platform: {exc}", cause=exc,
        )
    if isinstance(exc, (ConnectionError, ConnectionResetError)) or "connection reset" in text:
        return SendError(f"OneBot send failed: {exc}", retryable=True, cause=exc)
    permanent = any(token in text for token in (
        "invalid", "bad request", "forbidden", "not found", "unknown group", "unknown user",
    ))
    if permanent:
        return SendError(f"OneBot permanent send failure: {exc}", retryable=False, cause=exc)
    return AmbiguousAckError(f"OneBot send ack is unknown: {exc}", cause=exc)


_TARGET_IDENTITY_RE = re.compile(rf"(group|private):{ID_BODY}")


def target_identity(target: Mapping[str, Any]) -> str:
    target_type = str(target.get("type") or "").strip()
    if target_type == "group":
        target_id = target.get("group_id")
    elif target_type == "private":
        target_id = target.get("user_id")
    else:
        raise SendContractError(f"unknown QQ target type: {dict(target)!r}")
    field = "group_id" if target_type == "group" else "user_id"
    if target_id is None or str(target_id).strip() == "":
        raise SendContractError(f"{target_type} target missing {field}")
    identity = normalize_id(target_id)
    if not identity:
        raise SendContractError(f"{target_type} target has a malformed {field}: {target_id!r}")
    return f"{target_type}:{identity}"


def target_from_idempotency_key(key: str) -> str:
    """Recover the ``group:<id>`` / ``private:<id>`` identity a claim key was built for."""
    value = target_key_of(key)
    return value if _TARGET_IDENTITY_RE.fullmatch(value) else ""


def make_idempotency_key(
    target: Mapping[str, Any],
    content_identity: str,
    *,
    event_id: str = "",
) -> str:
    """Prefer outbound event identity, falling back to target + content digest."""
    return idempotency_key(target_identity(target), content_identity, event_id=event_id)


class OneBotIdempotencyStore(TwoPhaseIdempotencyStore):
    """The shared store bound to QQ target identities and the ``onebot_*`` log event names."""

    def __init__(self, path: Path | str = "data/onebot_outbound_idempotency.sqlite3", **options: Any) -> None:
        super().__init__(path, target_identity=target_identity, log_namespace="onebot", **options)


async def protected_claim_send(
    store: TwoPhaseIdempotencyStore,
    claim: IdempotencyClaim,
    operation: Callable[[], Awaitable[Any]],
    *,
    sent_ttl: float | None = None,
    message_id_getter: Callable[[Any], str] = lambda result: str(result or ""),
) -> Any:
    """Shield platform send+commit, classifying failures by the NapCat fingerprints."""
    return await _protected_claim_send(
        store, claim, operation,
        classify=classify_send_exception, sent_ttl=sent_ttl, message_id_getter=message_id_getter,
    )
