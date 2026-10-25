"""Replay and freshness checks for the handshake endpoint (RFC-AITP-0001 §5.5).

The SDK's envelope verification establishes authenticity and structure only;
the receiving endpoint owns replay and freshness. RFC-AITP-0009 §3.1 fixes the
order: the `message_id` deny list runs first, then the timestamp window, and
both before the SDK parses or verifies anything.

The deny list is keyed on `message_id`, never on the envelope bytes (RFC-AITP-0001
§5.4.3): a re-serialised copy of a captured envelope is still the same message.
An id is held until its envelope's timestamp leaves the window. After that a
replay is refused by the timestamp check instead, so the set stays bounded by
the traffic one window can carry. There is no per-source rate limit here (the
§3.1 step between the two); a full set refuses new envelopes instead.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional

DEFAULT_TOLERANCE_SECS = 300
DEFAULT_MAX_ENTRIES = 100_000


class HandshakeRejected(Exception):
    """An envelope refused before it reached the SDK. `code` is the
    registered AITP error code, or None for a 429, which RFC-AITP-0009 §3.1
    answers with no AITP error; `status` is the HTTP status to answer with."""

    def __init__(self, code: Optional[str], message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class ReplayGuard:
    def __init__(
        self,
        tolerance_secs: int = DEFAULT_TOLERANCE_SECS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.tolerance_secs = tolerance_secs
        self.max_entries = max_entries
        self._clock = clock
        self._seen: dict[str, float] = {}  # message_id -> forget-after (unix secs)

    def check(self, envelope_json: str) -> None:
        """Admit `envelope_json` once, or raise `HandshakeRejected`."""
        message_id, timestamp = _read_header(envelope_json)
        now = self._clock()
        self._prune(now)

        # 1. Deny list.
        if message_id in self._seen:
            raise HandshakeRejected("REPLAY_DETECTED", f"message_id already seen: {message_id}")

        # 2. Timestamp window, against our clock.
        if timestamp is None or abs(now - timestamp) > self.tolerance_secs:
            raise HandshakeRejected(
                "TIMESTAMP_EXPIRED",
                f"envelope timestamp outside ±{self.tolerance_secs}s",
            )

        # Only an envelope inside the window is recorded: the signature covers
        # the timestamp, so a copy of a stale one can never become fresh.
        # Recorded before the SDK runs, so an id is consumed even when the
        # rest of the envelope then fails.
        if len(self._seen) >= self.max_entries:
            # Refuse rather than forget an id still inside the window.
            raise HandshakeRejected(None, "replay deny list is full", status=429)
        self._seen[message_id] = timestamp + self.tolerance_secs

    def _prune(self, now: float) -> None:
        expired = [mid for mid, until in self._seen.items() if until < now]
        for mid in expired:
            del self._seen[mid]


def _read_header(envelope_json: str) -> tuple[str, Optional[float]]:
    """The envelope's `message_id` and `timestamp`, read without trusting
    anything else in it. A missing or unreadable `message_id` is an invalid
    envelope; a missing or non-numeric `timestamp` comes back as None and is
    refused by the window check."""
    try:
        envelope = json.loads(envelope_json)
    except ValueError as exc:
        raise HandshakeRejected("INVALID_ENVELOPE", f"envelope is not JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise HandshakeRejected("INVALID_ENVELOPE", "envelope is not a JSON object")
    message_id = envelope.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise HandshakeRejected("INVALID_ENVELOPE", "envelope has no message_id")
    timestamp = envelope.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        timestamp = None
    return message_id, timestamp
