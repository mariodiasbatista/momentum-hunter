"""Shared utilities for trader modules."""

_TRANSIENT_KEYWORDS = (
    "connection refused", "connection reset", "connection error",
    "timeout", "timed out", "network", "temporary",
    "service unavailable", "502", "503", "429",
)


def is_transient(exc: Exception) -> bool:
    """True for network/connection errors that may resolve on the next cycle."""
    msg = str(exc).lower()
    return any(k in msg for k in _TRANSIENT_KEYWORDS)


def log_api_error(log, context: str, exc: Exception) -> None:
    """Log transient errors as WARNING, real failures as ERROR."""
    if is_transient(exc):
        log.warning("%s: %s — transient, will retry next cycle", context, exc)
    else:
        log.error("%s: %s", context, exc)
