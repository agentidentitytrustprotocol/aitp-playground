"""Outstanding downstream PoP challenges (RFC-AITP-0005 §6, §10.5).

A capability call presents back a TCT this agent issued. The SDK builds the
challenge (`new_pop_challenge`) and verifies the answer together with the TCT
(`verify_tct(..., pop=...)`), but keeps no nonce cache: accepting each
challenge's response at most once (RFC-AITP-0001 §5.5) is ours. A challenge is
held by its nonce until its `expires_at`, and consumed by the first response
that names it, whether or not that response then verifies.

On HTTP the exchange rides in headers (§6.3): the challenge goes out in
`x-aitp-pop-challenge` on a 401, and the retry carries the holder's answer in
`x-aitp-pop-response`, each a base64url (unpadded) JSON payload.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Callable, Optional

CHALLENGE_HEADER = "x-aitp-pop-challenge"
RESPONSE_HEADER = "x-aitp-pop-response"
DEFAULT_TTL_SECS = 60
DEFAULT_MAX_ENTRIES = 100_000


class PopRejected(Exception):
    """A PoP step refused. `code` is the registered AITP error code, or None
    for a 429 (the challenge set is full), which carries no AITP error."""

    def __init__(self, code: Optional[str], message: str, status: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def encode_header(payload_json: str) -> str:
    return base64.urlsafe_b64encode(payload_json.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_header(value: str) -> str:
    """The JSON a PoP header carries. Raises ValueError when it is not
    base64url of UTF-8."""
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"not base64url JSON: {exc}") from exc


class PopChallenges:
    def __init__(
        self,
        ttl_secs: int = DEFAULT_TTL_SECS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl_secs = ttl_secs
        self.max_entries = max_entries
        self._clock = clock
        self._outstanding: dict[str, tuple[str, float]] = {}  # nonce -> (challenge, expires_at)

    def issue(self, new_challenge: Callable[[int], str]) -> str:
        """Record and return a fresh challenge. `new_challenge(ttl_secs)` is
        the SDK's `new_pop_challenge` bound to the presented TCT."""
        self._prune(self._clock())
        if len(self._outstanding) >= self.max_entries:
            raise PopRejected(None, "too many outstanding PoP challenges", status=429)
        challenge = new_challenge(self.ttl_secs)
        parsed = json.loads(challenge)
        self._outstanding[parsed["nonce"]] = (challenge, float(parsed["expires_at"]))
        return challenge

    def consume(self, response_json: str) -> str:
        """The challenge `response_json` answers, removed so it cannot be
        answered again. An unknown, already-answered or expired nonce is
        `POP_CHALLENGE_INVALID`; a response that names no nonce is
        `POP_RESPONSE_INVALID`."""
        try:
            response = json.loads(response_json)
        except ValueError as exc:
            raise PopRejected("POP_RESPONSE_INVALID", f"pop response is not JSON: {exc}") from exc
        nonce = response.get("nonce_echo") if isinstance(response, dict) else None
        if not isinstance(nonce, str) or not nonce:
            raise PopRejected("POP_RESPONSE_INVALID", "pop response has no nonce_echo")
        now = self._clock()
        self._prune(now)
        entry = self._outstanding.pop(nonce, None)
        if entry is None:
            raise PopRejected(
                "POP_CHALLENGE_INVALID", "unknown, already used or expired PoP challenge",
            )
        return entry[0]

    def _prune(self, now: float) -> None:
        expired = [n for n, (_, until) in self._outstanding.items() if until < now]
        for nonce in expired:
            del self._outstanding[nonce]
