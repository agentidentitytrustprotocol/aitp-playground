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
        self.router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/.well-known/aitp-manifest")
        def get_manifest() -> Response:
            return Response(self.manifest_json, media_type="application/json")

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
            responder = self.agent.new_responder()
            try:
                ack_json, session_id = responder.process_hello(hello_json)
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
            try:
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
                ack_json, tct_json = responder.process_commit(commit_json)
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
            tct = json.loads(tct_json)
            await emit_event(
                "handshake.complete", self.bootstrap,
                session_id=session_id,
                grants=tct["tct"]["grants"],
                peer_aid=tct["tct"]["issuer"],
                role="responder",
            )
            return Response(ack_json, media_type="application/json")

        return router

    def verify_capability_tct(self, tct_json: str, required_grant: str) -> "aitp.TctIdentity":
        """Verify the X-AITP-TCT header on an incoming capability call.

        Two-stage verification:
          1. Local revocation short-circuit on the TCT's ``jti``.
          2. SDK ``verify_tct`` in presented-TCT mode: we pass the TCT's
             own declared ``audience`` as ``expected_audience``. In v0.1
             (RFC-AITP-0005) ``audience == subject``, so this asserts
             "the TCT identifies this holder" — the holder's identity
             claim. The signature check (against the issuer's pubkey
             derived from ``tct.issuer``) is the security gate: it
             proves WE (this resource server) actually issued the TCT.

        Any failure produces a 403. The two parse failures (missing
        token, malformed JSON) are reported distinctly so debugging is
        easier."""
        if not tct_json:
            raise HTTPException(status_code=403, detail="missing X-AITP-TCT")
        try:
            tct_obj = json.loads(tct_json).get("tct", {})
        except (json.JSONDecodeError, AttributeError) as exc:
            raise HTTPException(status_code=403, detail=f"tct malformed: {exc}") from exc
        jti = tct_obj.get("jti", "")
        if jti and jti in self.revoked_jtis:
            raise HTTPException(status_code=403, detail=f"tct revoked: jti={jti}")
        declared_audience = tct_obj.get("audience")
        try:
            return self.agent.verify_tct(
                tct_json, required_grant, expected_audience=declared_audience,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=403, detail=f"tct rejected: {exc}") from exc
