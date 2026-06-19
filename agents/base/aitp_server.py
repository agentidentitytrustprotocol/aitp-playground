"""Shared AITP HTTP endpoints mounted by every agent worker.

All protocol operations go through the aitp-py SDK. The agent imports this
module and does `app.include_router(server.router)`.
"""
from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

import aitp
from fastapi import APIRouter, HTTPException, Request, Response

from oidc import OidcContext, peer_aid_from_hello_envelope
from tct_claims import decode_claims, tct_event
from telemetry import emit_event


def ready_lifespan(*, aid: str, port: int):
    """FastAPI lifespan that emits ``AITP_AGENT_READY`` once uvicorn has bound
    the listening socket. The supervisor uses this line as the spawn-ready
    signal; emitting it pre-bind would race against the first HTTP request."""

    @asynccontextmanager
    async def _lifespan(_app):
        sys.stdout.write(f"AITP_AGENT_READY aid={aid} port={port}\n")
        sys.stdout.flush()
        yield

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
        revoked_jtis: Optional[set[str]] = None,
    ) -> None:
        self.agent = agent
        self.manifest_json = manifest_json
        self.port = port
        self.bootstrap = bootstrap
        self.did_web_host = did_web_host
        # The set is shared with build_admin_router so /admin/revoke-tct can
        # mutate it and verify_capability_tct will see the change.
        self.revoked_jtis: set[str] = revoked_jtis if revoked_jtis is not None else set()
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

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/.well-known/aitp-manifest")
        def get_manifest() -> Response:
            return Response(self.manifest_json, media_type="application/json")

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
            manifest_kwargs: dict[str, Any] = {
                "display_name": cfg.get("display_name", ""),
                "handshake_endpoint": cfg.get("handshake_endpoint", ""),
                "offered_caps": list(cfg.get("offered_caps", [])),
                "ttl_secs": int(cfg.get("ttl_secs", 3600)),
            }
            if cfg.get("identity_type") == "oidc":
                manifest_kwargs["identity_type"] = "oidc"
                manifest_kwargs["oidc_issuer"] = cfg.get("oidc_issuer")
                manifest_kwargs["oidc_subject"] = cfg.get("oidc_subject")
            new_manifest = new_agent.build_manifest(**manifest_kwargs)
            self.agent = new_agent
            self.manifest_json = new_manifest
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
                doc = json.dumps({
                    "@context": ["https://www.w3.org/ns/did/v1"],
                    "id": f"did:web:{host}",
                    "service": [{
                        "id": f"did:web:{host}#aitp",
                        "type": "AitpManifest",
                        "serviceEndpoint": f"http://{host}",
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
            try:
                if allow_multihop and hasattr(
                    aitp, "verify_delegation_multihop"
                ):
                    verified = aitp.verify_delegation_multihop(
                        token_json, self.agent.aid, max_hops,
                    )
                else:
                    verified = aitp.verify_delegation(token_json, self.agent.aid)
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
             ``revoked_jtis`` below, so this is fail-closed either way).
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
        if jti and jti in self.revoked_jtis:
            raise HTTPException(status_code=403, detail=f"tct revoked: jti={jti}")
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
                    revoked_jtis=self.revoked_jtis,
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
                revoked_jtis=self.revoked_jtis,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=403, detail=f"tct rejected: {exc}") from exc
