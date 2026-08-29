"""Shared AITP HTTP endpoints mounted by every agent worker.

All protocol operations go through the aitp-py SDK. The agent imports this
module and does `app.include_router(server.router)`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import aitp
from fastapi import APIRouter, HTTPException, Request, Response

from bootstrap import get_manifest_json
from revocation_refresh import refresh_revocations
from revocation_state import RevocationState
from oidc import OidcContext, peer_aid_from_hello_envelope
from tct_claims import decode_claims, tct_event
from telemetry import emit_event

logger = logging.getLogger(__name__)

#: How long to wait after a failed manifest re-mint before trying again.
#: The previous manifest stays valid for another half-TTL, so there is room
#: to back off rather than retry on every single request.
_MANIFEST_REMINT_COOLDOWN_SECS = 30


def ready_lifespan(*, aid: str, port: int, server: "Optional[AitpServer]" = None):
    """FastAPI lifespan: signal readiness, and run the revocation poll.

    Emits ``AITP_AGENT_READY`` once uvicorn has bound the listening socket —
    the supervisor uses that line as the spawn-ready signal, and emitting it
    pre-bind would race against the first HTTP request.

    When a `server` is passed and it has a control plane configured, this also
    owns the background revocation refresh. RFC-AITP-0008 §1.4 says a consuming
    peer SHOULD poll; without a cadence, the staleness budget is either
    meaningless (nothing ever refreshes, so every agent is permanently
    degraded) or a time bomb for a long-running scenario.
    """

    @asynccontextmanager
    async def _lifespan(_app):
        # Fetch a snapshot BEFORE signalling ready. Under the default
        # fail_closed an agent with no verified snapshot rejects every
        # capability call, and a scenario's first call lands milliseconds
        # after the supervisor sees AITP_AGENT_READY — so no poll cadence,
        # however tight, closes that window. The refresh has to be complete
        # before we claim to be ready, which is also why it cannot go over
        # HTTP to ourselves: nothing is listening yet.
        if server is not None:
            await server.refresh_revocations_now(quiet=False)
        sys.stdout.write(f"AITP_AGENT_READY aid={aid} port={port}\n")
        sys.stdout.flush()
        task = server.start_revocation_poll() if server is not None else None
        try:
            yield
        finally:
            if task is not None:
                await server.stop_revocation_poll()

    return _lifespan


class AitpServer:
    def __init__(
        self,
        *,
        agent: "aitp.AitpAgent",
        manifest_json: str,
        port: int,
        bootstrap: dict[str, Any],
        did_web_host: Optional[str] = None,
        revocation: Optional[RevocationState] = None,
        did_web_scheme: str = "http",
    ) -> None:
        self.agent = agent
        self.manifest_json = manifest_json
        # Backoff after a failed re-mint; see _fresh_manifest_json. The lock
        # serializes
        # re-minting against /admin/rotate-keys: `get_manifest` runs in a
        # threadpool (sync route) while `rotate_keys` runs on the event loop,
        # so without it a re-mint that started before a rotation could finish
        # after it and overwrite the new-key manifest with an old-key one —
        # served for up to half a TTL, failing every handshake against it.
        self._manifest_remint_cooldown_until = 0.0
        self._manifest_lock = threading.Lock()
        self.port = port
        self.bootstrap = bootstrap
        self.did_web_host = did_web_host
        self.did_web_scheme = did_web_scheme
        # Shared with build_admin_router so /admin/revoke-tct and
        # /admin/refresh-revocations mutate the same state verify_capability_tct
        # reads. It holds local revocations and the CP snapshot separately —
        # see revocation_state.py for why a single set could not.
        self.revocation: RevocationState = (
            revocation if revocation is not None else RevocationState()
        )
        _cp = bootstrap.get("cp") if isinstance(bootstrap.get("cp"), dict) else {}
        #: Whether a control plane is configured at all. With none, this agent
        #: is in the explicitly-named unchecked posture — local revocations
        #: only — which is logged once at start-up rather than assumed.
        self.cp_configured: bool = bool(_cp.get("base_url"))
        self.revocation_fail_mode: str = _cp.get("fail_mode", "fail_closed")
        self.revocation_max_staleness_secs: int = int(
            _cp.get("max_staleness_secs", 300)
        )
        self.revocation_poll_secs: int = int(_cp.get("poll_secs", 60))
        #: Whether the operator has ASKED for verification: a control plane and
        #: a pinned issuer AID. Deliberately not "and the SDK can do it" —
        #: see below.
        #:
        #: Distinct from "the last fetch worked". Without this distinction an
        #: agent that merely has no pinned AID looks identical to one whose CP
        #: went down, and `fail_closed` rejects every call on a deployment that
        #: has simply not been configured yet — broken-by-default on the very
        #: upgrade that introduces the setting.
        self.can_verify_revocation: bool = bool(self.cp_configured and _cp.get("aid"))
        if not self.cp_configured:
            logger.info(
                "revocation: no control plane configured — enforcing local "
                "revocations only. This is the unchecked posture; a peer's "
                "revocations published via a CP will not be seen."
            )
        elif not _cp.get("aid"):
            logger.warning(
                "revocation: CP configured but no CP_AID pinned — snapshots "
                "cannot be verified, so this agent enforces LOCAL revocations "
                "only. Set CP_AID to the control plane's AID to turn on "
                "snapshot verification and fail_mode=%s.",
                self.revocation_fail_mode,
            )
        elif not hasattr(aitp, "verify_revocation_list"):
            # A pin IS set, so verification was explicitly asked for, and the
            # wheel cannot deliver it. That is DEGRADED, not unchecked: an old
            # SDK must not silently downgrade a deployment that opted in.
            # Treating a capability probe as consent is precisely the unchecked
            # posture this work exists to remove — every snapshot will be
            # discarded with cause=sdk_cannot_verify, and under the default
            # fail_closed that is a loud failure rather than a quiet one.
            logger.error(
                "revocation: CP_AID is pinned but the installed aitp-sdk "
                "cannot verify snapshots (needs >=0.6.0). Every snapshot will "
                "be discarded and this agent runs DEGRADED (fail_mode=%s). "
                "Upgrade the SDK or unset CP_AID to fall back to local-only "
                "revocation deliberately.",
                self.revocation_fail_mode,
            )
        self._sessions: dict[str, Any] = {}  # session_id -> ResponderSession
        # Issued-TCT log keyed by peer AID. Populated when a responder
        # session completes — gives /admin/export-session-bundle access
        # to the TCTs we (the coordinator) handed out.
        self._issued_tcts: dict[str, str] = {}
        # RFC-AITP-0005 hot-path cache. When the installed wheel exposes
        # ``TctStore``, repeat presentations of a byte-identical, still-valid
        # TCT skip the signature check (cheap policy checks still run). Older
        # wheels (or one built without the cache) leave this None and fall
        # back to plain ``verify_tct``. Sized for demo-scale runs.
        self._tct_store = aitp.TctStore(256) if hasattr(aitp, "TctStore") else None
        self._degraded_serves = 0
        self._telemetry_tasks: set[asyncio.Task[None]] = set()
        self._tct_cache_hits = 0
        self._tct_cache_misses = 0
        self.oidc = OidcContext(bootstrap)
        self.router = self._build_router()

    def _new_responder(self):
        """Construct a ResponderSession with the OIDC verifier wired in
        when the run has an OIDC issuer configured. Plain pinned-key
        runs leave both kwargs at their None defaults."""
        if self.oidc.enabled:
            return self.agent.new_responder(
                jwks=self.oidc.jwks,
                trust_anchors=self.oidc.trust_anchors,
            )
        return self.agent.new_responder()

    # ── background revocation refresh ────────────────────────────────────

    def start_revocation_poll(self) -> "Optional[asyncio.Task[None]]":
        """Begin polling the CP for a fresh snapshot. No-op without a CP."""
        if not self.cp_configured:
            return None
        self._poll_task = asyncio.create_task(self._revocation_poll_loop())
        return self._poll_task

    async def stop_revocation_poll(self) -> None:
        task = getattr(self, "_poll_task", None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._poll_task = None

    async def _revocation_poll_loop(self) -> None:
        """Refresh on a cadence, reporting state CHANGES rather than ticks.

        `revocation.poll` itself is rate-limited this way: a 60s poll against
        a control plane that is down would otherwise be one event per minute
        per agent, so this emits when the health flips (ok->failing,
        failing->ok) plus a low-frequency heartbeat, not on every attempt.

        This is layered on top of a signal that is NOT rate-limited: every
        call to `refresh_revocations_now` below goes through the shared ingest
        path (`revocation_refresh.refresh_revocations`), which emits
        `revocation.verify_failed` on every discarded snapshot regardless of
        `quiet` (`DECISIONS.md` D-14). `revocation.poll`'s `healthy` flag
        cannot distinguish a down CP from a forged snapshot — both are
        `False` — so it is a heartbeat on top of the real signal, not a
        replacement for it.
        """
        healthy: Optional[bool] = None
        ticks = 0
        heartbeat_every = max(1, 600 // max(1, self.revocation_poll_secs))

        # The start-up refresh already ran (see `ready_lifespan`), so this
        # loop just sleeps its cadence from the start.
        while True:
            await asyncio.sleep(self.revocation_poll_secs)
            ticks += 1
            try:
                ok = await self.refresh_revocations_now(quiet=True)
            except asyncio.CancelledError:
                raise

            changed = healthy is None or ok != healthy
            if changed or ticks % heartbeat_every == 0:
                await emit_event(
                    "revocation.poll",
                    self.bootstrap,
                    healthy=ok,
                    changed=changed,
                    posture=self.revocation.posture(
                        can_verify=self.can_verify_revocation,
                        max_staleness_secs=self.revocation_max_staleness_secs,
                    ),
                )
            healthy = ok

    async def refresh_revocations_now(self, *, quiet: bool) -> bool:
        """One refresh through the shared ingest path. No HTTP hop.

        Returns whether a snapshot is now verified and in force — which is
        what the poll loop reports state changes on.
        """
        if not self.cp_configured:
            return False
        try:
            result = await refresh_revocations(
                revocation=self.revocation,
                bootstrap=self.bootstrap,
                emit=emit_event,
                quiet=quiet,
            )
        except Exception:  # noqa: BLE001 — a refresh must never kill the agent
            logger.exception("revocation refresh raised")
            return False
        return bool(result.get("verified"))

    def _emit_soon(self, event_type: str, **fields: Any) -> None:
        """Fire a telemetry event from a sync path.

        `verify_capability_tct` is sync but is only ever called from an async
        route handler, so a running loop exists. Scheduling rather than
        awaiting keeps telemetry off the request's critical path — a slow
        collector must never be able to delay a capability call, let alone
        fail one.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (a direct unit-test call). The log line above already
            # recorded the degraded state; dropping the event is correct
            # rather than raising into the caller's request.
            return
        task = loop.create_task(emit_event(event_type, self.bootstrap, **fields))
        # Hold a reference so the task is not garbage-collected mid-flight,
        # and drop it on completion so the set cannot grow without bound.
        self._telemetry_tasks.add(task)
        task.add_done_callback(self._telemetry_tasks.discard)

    def _enforce_revocation_freshness(self) -> None:
        """Apply the Axis B policy for the ABSENCE of a fresh snapshot.

        Never about authenticity — an unverifiable snapshot was already
        discarded at ingest, unconditionally, and no mode here can resurrect
        it. That separation is the whole point of D1: under a collapsed single
        switch, `soft_fail` reports a *forged* snapshot as not-revoked, so an
        attacker who can serve garbage gets the same outcome as one who can
        suppress the list.
        """
        posture = self.revocation.posture(
            can_verify=self.can_verify_revocation,
            max_staleness_secs=self.revocation_max_staleness_secs,
        )
        if posture != "degraded":
            return

        reason = self.revocation.degraded_reason(
            max_staleness_secs=self.revocation_max_staleness_secs
        )
        # Anything that is not an explicit, recognized opt-in fails closed.
        # RFC-AITP-0008 §3.1: "Deployments that need availability-first
        # behavior MUST opt into `soft_fail` or `fail_open` explicitly." A typo
        # is not an opt-in, and neither is a mode this build does not
        # implement — treating an unrecognized value as permissive would make
        # a misspelling silently disable enforcement.
        if self.revocation_fail_mode != "soft_fail":
            # The spec's schema default (§3.1). Distinct detail text from a
            # deny-list hit above, so the two are never confused.
            raise HTTPException(
                status_code=403,
                detail=(
                    f"revocation state degraded ({reason}) and fail_mode is "
                    f"{self.revocation_fail_mode!r} — refusing to honour a TCT "
                    "this agent cannot check against a fresh revocation snapshot"
                ),
            )
        # soft_fail: proceed on the last verified deny-set. §3.1 requires the
        # degraded state to be logged, so it is never silent — and a log line
        # inside a container is not an observable, so it also emits an event.
        # Emitted on the FIRST degraded serve and then every 100th, because
        # once an agent is degraded every single call takes this path and a
        # per-call event would bury the `verify_failed` that explains why.
        self._degraded_serves += 1
        logger.warning(
            "revocation state degraded (%s) — serving on the last verified "
            "deny-set because fail_mode=soft_fail",
            reason,
        )
        if self._degraded_serves == 1 or self._degraded_serves % 100 == 0:
            self._emit_soon(
                "revocation.degraded_serve",
                reason=reason,
                serves=self._degraded_serves,
                fail_mode=self.revocation_fail_mode,
            )

    def _manifest_deadline(self, manifest_json: str) -> float:
        """When the served manifest must be re-minted: its own half-life.

        Read out of the manifest itself, not from config plus a timestamp.
        The earlier version computed this from `bootstrap.ttl_secs` and a
        `_manifest_minted_at` stamped in the constructor — for a manifest the
        constructor did not mint. That was correct only because the agent
        happens to mint milliseconds before constructing, with the same config,
        and it breaks the moment either stops being true: a manifest loaded
        from anywhere else, or a `ttl_secs` that disagrees with what was
        actually baked in. The artifact carries both timestamps; asking it is
        both simpler and impossible to desynchronise.
        """
        body = json.loads(manifest_json)["manifest"]
        published_at = float(body["published_at"])
        expires_at = float(body["expires_at"])
        return published_at + (expires_at - published_at) / 2

    def _fresh_manifest_json(self) -> str:
        """The served manifest, re-minted before it can expire.

        A manifest is signed with a `ttl_secs` lifetime (default 3600,
        `bootstrap.py:32`) and was previously minted once at construction and
        served verbatim for the life of the process. Nothing noticed, because
        nothing in this family verified a peer manifest.

        Peers verify now, and `verify_manifest_json` checks `expires_at`
        against the wall clock with no override. A hosted agent alive longer
        than its TTL would therefore serve a manifest every verifying peer
        rejects — it would drop off the network an hour after start-up.
        Re-minting is the fix for the actual defect: serving a credential past
        its own stated lifetime.

        Re-minting at the **half-life** guarantees a floor: whatever we serve
        always has between TTL/2 and TTL of validity left. That clears the
        control plane's own enrollment guard (which rejects manifests expiring
        within 5 minutes) and `max_staleness_secs` with room to spare, and it
        is the standard shape for self-issued short-lived credentials.

        Re-minting keeps the AID (same key, so same self-certifying
        identifier); only `published_at`, `expires_at` and `signature` move.
        Peers that pin the AID are unaffected. Rotation
        (`/admin/rotate-keys`) still replaces the whole thing, key included.
        """
        now = time.time()
        if now < self._manifest_deadline(self.manifest_json):
            return self.manifest_json
        # A failed re-mint sets a cooldown. Without one, every subsequent
        # request retakes the lock, retries the signature and logs a full
        # traceback — harmless at Ed25519 speed, a self-inflicted
        # request-serialization stall the moment signing moves behind a KMS.
        if now < self._manifest_remint_cooldown_until:
            return self.manifest_json

        with self._manifest_lock:
            # Re-check under the lock: a concurrent request (or a rotation)
            # may already have refreshed it.
            if time.time() < self._manifest_deadline(self.manifest_json):
                return self.manifest_json
            agent = self.agent
            try:
                minted = get_manifest_json(agent, self.bootstrap)
            except Exception:  # noqa: BLE001
                # Serving the previous manifest is strictly better than
                # serving nothing; it stays valid until the full TTL elapses,
                # which leaves a half-TTL window for a retry to succeed.
                self._manifest_remint_cooldown_until = (
                    time.time() + _MANIFEST_REMINT_COOLDOWN_SECS
                )
                logger.exception(
                    "manifest re-mint failed; serving the previous one and "
                    "backing off for %ss",
                    _MANIFEST_REMINT_COOLDOWN_SECS,
                )
                return self.manifest_json
            if agent is not self.agent:
                # A rotation landed while we were signing. The rotation's own
                # manifest is authoritative; ours is signed by a key this
                # agent no longer holds. Discard it.
                logger.info("manifest re-mint superseded by a key rotation")
                return self.manifest_json
            self.manifest_json = minted
            self._manifest_remint_cooldown_until = 0.0
        return self.manifest_json

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/.well-known/aitp-manifest")
        def get_manifest() -> Response:
            return Response(self._fresh_manifest_json(), media_type="application/json")

        @router.post("/admin/rotate-keys")
        async def rotate_keys(request: Request) -> Response:
            """Replace this agent's keypair and republish its manifest under
            the new identity. Existing TCTs that this agent issued become
            invalid because their declared ``issuer`` (the old AID) no longer
            matches the running agent; subsequent capability calls
            presenting those TCTs will be rejected by
            ``verify_capability_tct``.

            The route is intentionally minimal: no rollover window, no
            simultaneous accept-both-keys phase. The point is to demonstrate
            that a peer caching the old manifest must re-handshake against
            the new manifest to obtain a TCT under the new key. In-flight
            handshake sessions are dropped (the responder state was scoped
            to the old key); peers must restart their handshake.
            """
            old_aid = self.agent.aid
            old_manifest = self.manifest_json

            cfg = self.bootstrap.get("aitp", {})
            suite = cfg.get("signing_suite", "ed25519")
            new_agent = aitp.AitpAgent.generate(suite=suite)
            # One mint path, shared with start-up and the half-life re-mint.
            # This used to re-implement `get_manifest_json` inline, and the
            # two copies had already drifted (`display_name` handling, and
            # this one dropped the `identity_type` default) — the same
            # duplicate-logic shape that produced two signature-blind parses
            # elsewhere in this repo.
            new_manifest = get_manifest_json(new_agent, self.bootstrap)
            with self._manifest_lock:
                self.agent = new_agent
                self.manifest_json = new_manifest
                self._manifest_remint_cooldown_until = 0.0
            self._sessions.clear()

            await emit_event(
                "identity.key.rotated", self.bootstrap,
                old_aid=old_aid, new_aid=new_agent.aid,
            )
            return Response(
                json.dumps({
                    "aid": new_agent.aid,
                    "old_aid": old_aid,
                    "manifest_replaced": old_manifest != new_manifest,
                }),
                media_type="application/json",
            )

        if self.did_web_host:
            @router.get("/.well-known/did.json")
            def get_did_document() -> Response:
                host = self.did_web_host or ""
                scheme = self.did_web_scheme or "http"
                doc = json.dumps({
                    "@context": ["https://www.w3.org/ns/did/v1"],
                    "id": f"did:web:{host}",
                    "service": [{
                        "id": f"did:web:{host}#aitp",
                        "type": "AitpManifest",
                        "serviceEndpoint": f"{scheme}://{host}",
                    }],
                })
                return Response(doc, media_type="application/did+json")

        @router.post("/aitp/handshake/hello")
        async def hello(request: Request) -> Response:
            hello_json = (await request.body()).decode()
            responder = self._new_responder()
            # If this agent is OIDC-typed, mint its JWT bound to the
            # initiator's AID extracted from the hello envelope so the
            # initiator's verify_oidc sees aud == its own AID.
            mint_cb = None
            if self.oidc.identity_type == "oidc":
                peer_aid = peer_aid_from_hello_envelope(hello_json) or ""
                mint_cb = self.oidc.mint_jwt_for(
                    audience=peer_aid, agent=self.agent,
                )
            try:
                ack_json, session_id = responder.process_hello(
                    hello_json, oidc_mint_jwt=mint_cb,
                )
            except Exception as exc:  # noqa: BLE001
                await emit_event("handshake.failed", self.bootstrap, error=str(exc))
                return Response(
                    json.dumps({"error": str(exc)}),
                    status_code=400,
                    media_type="application/json",
                )
            self._sessions[session_id] = responder
            await emit_event(
                "handshake.started", self.bootstrap, session_id=session_id, role="responder"
            )
            return Response(
                ack_json,
                media_type="application/json",
                headers={"X-Aitp-Session-Id": session_id},
            )

        @router.post("/aitp/delegation/redeem")
        async def redeem_delegation(request: Request) -> Response:
            """Receive a DelegationToken from a delegatee, verify it against
            our AID, and (if valid) mint a fresh TCT bound to the delegatee's
            cnf key. Returns the new TctEnvelope JSON.

            Verification ensures we (the verifier) are the token's
            ``delegator`` — i.e. the original grantor — so untrusted parties
            cannot redeem against us with chains we never authored.

            v0.1 demo simplification: no proof-of-possession challenge.
            The cnf binding in the issued TCT means any subsequent capability
            call must present the TCT signed by the delegatee's key, so
            stolen-token replay is still bounded.
            """
            body = await request.json()
            token_json: str = body["delegation_token"]
            # Opt-in draft RFC-AITP-0011 multi-hop verification. When the
            # scenario sets ``aitp.allow_multihop_delegation`` AND the wheel
            # was built with multi-hop verification (default in the SDK),
            # accept a chained token up to ``max_delegation_hops``. Otherwise
            # fall back to strict v0.1 single-hop, which rejects any non-empty
            # chain with DELEGATION_MULTIHOP_NOT_SUPPORTED.
            cfg = self.bootstrap.get("aitp", {})
            allow_multihop = bool(cfg.get("allow_multihop_delegation"))
            max_hops = int(cfg.get("max_delegation_hops", 3))
            # RFC-AITP-0006 §4 step 7 / RFC-AITP-0011 §6. Redeeming mints a
            # *fresh TCT* for the delegatee, so skipping this is not a
            # bookkeeping miss: a revoked grant keeps minting credentials for
            # a third party we never re-authorized. The SDK consults the set
            # only after every signature check (RFC-AITP-0008 §3.3).
            #
            # The deny-set is the same union the capability path enforces —
            # local revocations plus the verified CP snapshot. Hop jtis are
            # issued by peers, so for those the CP snapshot is the only
            # source we have; this is the one place a CP-derived entry
            # changes a decision rather than only a diagnosis.
            deny_set = self.revocation.effective_jtis
            # Axis B applies here too, and more sharply than on a capability
            # call: under ``fail_closed`` we refuse to mint a credential
            # while we cannot tell whether its source grant is still live.
            self._enforce_revocation_freshness()
            try:
                if allow_multihop and hasattr(
                    aitp, "verify_delegation_multihop"
                ):
                    verified = aitp.verify_delegation_multihop(
                        token_json, self.agent.aid, max_hops, deny_set,
                    )
                else:
                    verified = aitp.verify_delegation(
                        token_json, self.agent.aid, deny_set,
                    )
            except TypeError as exc:
                # The installed SDK predates the revocation parameter
                # (aitp-sdk < 0.7.0). Fail closed and say so. Probing with
                # ``hasattr`` and silently dropping the deny-set would
                # reinstate exactly the gap this call site exists to close:
                # a capability probe is not consent, so an old SDK must never
                # silently downgrade enforcement — it must refuse loudly.
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "installed aitp-sdk cannot enforce delegation "
                        "revocation (needs >=0.7.0) — refusing to redeem a "
                        f"delegation we cannot check: {exc}"
                    ),
                ) from exc
            except (RuntimeError, ValueError) as exc:
                await emit_event(
                    "delegation.rejected", self.bootstrap, error=str(exc),
                )
                return Response(
                    json.dumps({"error": f"delegation rejected: {exc}"}),
                    status_code=403, media_type="application/json",
                )

            fresh_tct_envelope = self.agent.issue_tct_for_delegatee(verified)
            await emit_event(
                "delegation.redeemed", self.bootstrap,
                delegatee_aid=verified.delegatee,
                grants=list(verified.grants),
                role="issuer",
            )
            return Response(fresh_tct_envelope, media_type="application/json")

        @router.post("/aitp/handshake/commit")
        async def commit(request: Request) -> Response:
            session_id = request.headers.get("x-aitp-session-id", "")
            responder = self._sessions.pop(session_id, None)
            if responder is None:
                return Response(
                    json.dumps({"error": f"No session: {session_id}"}),
                    status_code=404,
                    media_type="application/json",
                )
            commit_json = (await request.body()).decode()
            try:
                ack_json, completed_json = responder.process_commit(commit_json)
            except Exception as exc:  # noqa: BLE001
                await emit_event(
                    "handshake.failed", self.bootstrap,
                    session_id=session_id, error=str(exc),
                )
                return Response(
                    json.dumps({"error": str(exc)}),
                    status_code=400,
                    media_type="application/json",
                )
            # v0.2: process_commit returns
            # ``{"tct": "<compact JWS>", "grant_voucher": "<compact JWS>"|null}``.
            # The TCT is an opaque token; claims live in its JWS payload. Note
            # the returned TCT is the one the PEER issued to us (``iss`` = peer,
            # ``sub`` = self) — the SDK's ``CompletedHandshake`` hands each side
            # the token it now holds. We therefore do not have our own issued
            # token here; RFC-AITP-0010 bundles collect each participant's
            # held (coordinator-issued) token instead (see the runner's
            # export_session_bundle step + /admin/held-tct).
            completed = json.loads(completed_json)
            tct_token = completed["tct"]
            claims = decode_claims(tct_token)
            await emit_event(
                "handshake.complete", self.bootstrap,
                session_id=session_id,
                tct=tct_event(tct_token),
                grants=claims.get("grants", []),
                peer_aid=claims.get("iss"),
                role="responder",
            )
            return Response(ack_json, media_type="application/json")

        @router.get("/admin/tct-cache-stats")
        def tct_cache_stats() -> Response:
            """Report this agent's RFC-AITP-0005 verification-cache counters.

            ``enabled`` is False when the installed wheel lacks ``TctStore``
            (every call verifies fresh). Used by the ``tct-cache-perf``
            scenario to show repeat presentations hitting the cache.
            """
            return Response(
                json.dumps({
                    "enabled": self._tct_store is not None,
                    "hits": self._tct_cache_hits,
                    "misses": self._tct_cache_misses,
                    "size": self._tct_store.len() if self._tct_store is not None else 0,
                }),
                media_type="application/json",
            )

        return router

    def verify_capability_tct(self, tct_token: str, required_grant: str) -> "aitp.TctIdentity":
        """Verify the X-AITP-TCT header on an incoming capability call.

        Under v0.2 the header carries an opaque compact-JWS TCT token.

        Two-stage verification:
          1. Local revocation short-circuit on the TCT's ``jti`` (read from
             the unverified claims for a precise 403; the SDK also re-checks
             the SDK also re-checks the same set below, so this is
             fail-closed either way).
          2. SDK ``verify_tct`` in presented-TCT mode: we pass the TCT's
             own declared ``aud`` as ``expected_audience``. In v0.1/v0.2
             (RFC-AITP-0005) ``aud`` defaults to ``sub``, so this asserts
             "the TCT identifies this holder" — the holder's identity
             claim. The signature check (against the issuer's pubkey
             derived from ``iss``) is the security gate: it proves WE
             (this resource server) actually issued the TCT.

        Any failure produces a 403. Missing/malformed tokens are reported
        distinctly so debugging is easier."""
        if not tct_token:
            raise HTTPException(status_code=403, detail="missing X-AITP-TCT")
        try:
            claims = decode_claims(tct_token)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=f"tct malformed: {exc}") from exc
        jti = claims.get("jti", "")
        if jti and self.revocation.is_revoked(jti):
            # Name the source. "We revoked this" and "the control plane says
            # someone revoked this" are different facts for whoever reads the
            # 403, and collapsing them makes a CP-propagation bug look like a
            # local one.
            source = "local" if self.revocation.is_locally_revoked(jti) else "cp-snapshot"
            raise HTTPException(
                status_code=403, detail=f"tct revoked ({source}): jti={jti}"
            )
        # Axis B. Checked AFTER the deny-set so a genuine revocation keeps its
        # own reason: "this token is revoked" and "I cannot currently tell
        # whether it is" are different answers, and reporting the second for
        # the first would send an operator hunting a CP outage that is not
        # there.
        self._enforce_revocation_freshness()
        # Issuer-AID check: TCTs we issued must declare us as the issuer.
        # After a key rotation our AID changes, so any TCT issued by the
        # pre-rotation key fails this guard before the signature path runs.
        # The check is defensive even outside the rotation flow — a peer
        # presenting a TCT signed by some other resource server has no
        # business calling our capability endpoints.
        declared_issuer = claims.get("iss")
        if declared_issuer and declared_issuer != self.agent.aid:
            raise HTTPException(
                status_code=403,
                detail=f"tct issuer mismatch: {declared_issuer} != {self.agent.aid}",
            )
        declared_audience = claims.get("aud") or claims.get("sub")
        try:
            if self._tct_store is not None:
                before = self._tct_store.len()
                identity = self.agent.verify_tct_cached(
                    tct_token,
                    required_grant,
                    self._tct_store,
                    expected_audience=declared_audience,
                    revoked_jtis=self.revocation.effective_jtis,
                )
                # len-delta hit/miss heuristic: a miss inserts a new entry,
                # a hit reuses an existing one. Exact while size < max_entries
                # (no eviction), which holds for demo-scale runs.
                if self._tct_store.len() > before:
                    self._tct_cache_misses += 1
                else:
                    self._tct_cache_hits += 1
                return identity
            return self.agent.verify_tct(
                tct_token, required_grant,
                expected_audience=declared_audience,
                revoked_jtis=self.revocation.effective_jtis,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=403, detail=f"tct rejected: {exc}") from exc
