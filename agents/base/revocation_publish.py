"""The issuer side of revocation: this agent's own signed revocation list.

RFC-AITP-0008 §1.5 makes a revocation list valid only for tokens its signer
issued, and a verifier discards a list signed by anyone else (spec fixture
rev-011). So the control plane signs nothing: it records revocation entries
and relays them back to their issuer (`GET /api/revocations?iss=<aid>`). The
agent signs that entry set with its own key and serves it at
`/.well-known/aitp-revocation-list`, the path aitp-rs serves `ListRevoked` at
(`REVOCATION_LIST_PATH`).

Two rules decide what is served, and both come from one fact: an empty signed
list is an assertion ("nothing I issued is revoked"), not a placeholder.

* **The CP is down or answers badly:** keep serving the last signed list,
  byte for byte, until its signed `expires_at`. It is not re-signed, since that
  would extend a list we cannot currently confirm.
* **There is no list in force** (never pulled, or the last one expired):
  serve 503 `REVOCATION_UNAVAILABLE`, never an empty list. A verifier under
  the default `fail_closed` then refuses this issuer's tokens, and that refusal
  is the correct outcome.

With no control plane configured there is no revocation record anywhere, so
the empty list is true and the agent signs one. Every agent therefore serves a
list whenever it can honestly say what is in it.

All signing goes through the SDK (`AitpAgent.sign_revocation_list`); this
module only decides *what* to sign and *whether* to serve.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import aitp
import httpx

EventEmitter = Callable[..., Awaitable[None]]

#: Where aitp-rs's `HandshakeServer` serves `ListRevoked`, exported by the
#: binding since aitp-sdk 0.20. The Manifest's
#: `rfc-aitp-0008.revocation_list_uri` points at the agent's origin plus this.
REVOCATION_LIST_PATH: str = aitp.REVOCATION_LIST_PATH

#: Retry-After on our 503, matching the control plane's own.
RETRY_AFTER_SECS = 30


class PullRefused(Exception):
    """The CP answer cannot become a list: keep the last one in force."""

    def __init__(self, cause: str, detail: str) -> None:
        super().__init__(detail)
        self.cause = cause


@dataclass(frozen=True)
class SignedList:
    """One signed list, kept with the facts needed to decide whether to serve it."""

    envelope_json: str
    #: The AID that signed it. A key rotation makes it another issuer's list.
    signer_aid: str
    entry_count: int
    published_at: int
    expires_at: int

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at


class IssuerRevocationList:
    """Pulls this agent's entries from the CP, signs them, and holds the result.

    `agent_provider` returns the agent's *current* `AitpAgent`, not a fixed one:
    `/admin/rotate-keys` swaps the key, and a list signed by the old key names
    the old issuer.
    """

    def __init__(
        self,
        *,
        agent_provider: Callable[[], "aitp.AitpAgent"],
        bootstrap: dict[str, Any],
    ) -> None:
        self._agent_provider = agent_provider
        self.bootstrap = bootstrap
        cp = bootstrap.get("cp") if isinstance(bootstrap.get("cp"), dict) else {}
        self.cp_base_url: Optional[str] = cp.get("base_url") or None
        self.cp_api_key: Optional[str] = cp.get("api_key") or None
        self.ttl_secs: int = int(cp.get("list_ttl_secs", 3600))
        self.current: Optional[SignedList] = None

    # ── serving ──────────────────────────────────────────────────────────
    def serveable(self, now: Optional[float] = None) -> Optional[SignedList]:
        """The list to serve now, or None for a 503.

        Without a CP, an empty list is signed on demand (and re-signed once it
        expires). With one, only a list built from a CP answer is ever served.
        """
        aid = self._agent_provider().aid
        signed = self.current
        if signed is not None and (signed.signer_aid != aid or signed.is_expired(now)):
            signed = None
        if signed is None and self.cp_base_url is None:
            signed = self._sign([])
            self.current = signed
        return signed

    # ── refreshing ───────────────────────────────────────────────────────
    async def refresh(self, *, emit: EventEmitter, quiet: bool = False) -> dict[str, Any]:
        """Pull this issuer's entries and replace the list with a newly signed one.

        On any failure the list in force is left exactly as it was. Returns a
        result dict for the admin route and the poll loop.
        """
        if self.cp_base_url is None:
            signed = self.serveable()
            return {"published": True, "entry_count": signed.entry_count if signed else 0}

        aid = self._agent_provider().aid
        try:
            entries = await self._pull(aid)
        except PullRefused as exc:
            if not quiet:
                await emit(
                    "revocation.publish_failed",
                    self.bootstrap,
                    cause=exc.cause,
                    detail=str(exc),
                    serving=self.serveable() is not None,
                )
            return {"published": False, "cause": exc.cause, "detail": str(exc)}

        previous = self.current.entry_count if self.current is not None else None
        self.current = self._sign(entries)
        if not quiet or previous != len(entries):
            await emit(
                "revocation.list_published",
                self.bootstrap,
                issuer=aid,
                entry_count=len(entries),
                expires_at=self.current.expires_at,
            )
        return {"published": True, "entry_count": len(entries)}

    async def _pull(self, aid: str) -> list[dict[str, Any]]:
        url = f"{self.cp_base_url.rstrip('/')}/api/revocations"
        headers = {"Authorization": f"Bearer {self.cp_api_key}"} if self.cp_api_key else {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params={"iss": aid}, headers=headers)
        except httpx.HTTPError as exc:
            raise PullRefused("cp_unreachable", str(exc)) from exc
        if resp.status_code != 200:
            code = _error_code(resp) or f"http_{resp.status_code}"
            raise PullRefused(code, f"CP answered {resp.status_code} ({code})")
        try:
            body = resp.json()
            if body["iss"] != aid:
                raise PullRefused(
                    "issuer_mismatch", f"CP answered for {body['iss']!r}, not {aid!r}"
                )
            return [_entry(e) for e in body["entries"]]
        except PullRefused:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise PullRefused("malformed_body", f"CP revocation body: {exc}") from exc

    def _sign(self, entries: list[dict[str, Any]]) -> SignedList:
        agent = self._agent_provider()
        envelope_json = agent.sign_revocation_list(entries, expires_in_secs=self.ttl_secs)
        body = json.loads(envelope_json)["revocation_list"]
        return SignedList(
            envelope_json=envelope_json,
            signer_aid=agent.aid,
            entry_count=len(entries),
            published_at=int(body["published_at"]),
            expires_at=int(body["expires_at"]),
        )


def _entry(raw: Any) -> dict[str, Any]:
    """One CP entry in the shape `sign_revocation_list` takes.

    A jti that is not a UUID refuses the whole pull rather than being dropped:
    since aitp-sdk 0.19 a verifier's deny-list rejects non-UUID entries, and a
    list quietly missing one entry is a list that un-revokes it.
    """
    entry: dict[str, Any] = {
        "jti": str(uuid.UUID(raw["jti"])),
        "revoked_at": int(raw["revoked_at"]),
    }
    if raw.get("reason") is not None:
        entry["reason"] = str(raw["reason"])
    return entry


def _error_code(resp: httpx.Response) -> Optional[str]:
    try:
        code = resp.json().get("code")
    except (ValueError, AttributeError):
        return None
    return code if isinstance(code, str) else None
