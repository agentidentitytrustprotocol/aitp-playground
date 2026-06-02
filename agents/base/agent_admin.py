"""Shared admin routes used by every agent worker.

The playground tells an agent to:
  * `/admin/initiate-handshake` — handshake with a peer manifest URL using aitp-py
  * `/admin/invoke`             — invoke a capability on a peer using a held TCT
  * `/admin/self-execute`       — run one of our own registered capabilities locally
                                  (no peer call, no TCT — used when the scenario step
                                  targets the same agent that offers the capability)

All AITP protocol logic lives in the aitp-py SDK.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

from telemetry import emit_event

CapabilityHandler = Callable[[Any], Awaitable[Any]]
ManifestProvider = Callable[[], str]


def build_admin_router(
    *,
    agent,                                       # aitp.AitpAgent
    bootstrap: dict[str, Any],
    held_tcts: dict[int, str],                   # peer_port -> tct_json (mutated in place)
    revoked_jtis: set[str],                      # mutated in place by /admin/revoke-tct
    capabilities: Optional[dict[str, CapabilityHandler]] = None,
    manifest_provider: Optional[Callable[[], str]] = None,
) -> APIRouter:
    """Build the /admin router. ``capabilities`` maps capability name to an
    async handler invoked by ``/admin/self-execute``. The handler receives the
    parsed JSON payload (str/dict/None) and returns a JSON-serializable value.

    ``revoked_jtis`` is the same set referenced by ``AitpServer`` so that
    revoking a TCT via ``/admin/revoke-tct`` makes subsequent capability calls
    that present that TCT fail with 403.

    ``manifest_provider``, when set, returns the current ManifestEnvelope
    JSON string for this agent (typically a closure over ``AitpServer``'s
    ``manifest_json`` field, so that a key rotation is observable through
    /admin/enroll-with-cp without restarting the agent). Required for the
    /admin/enroll-with-cp route.
    """
    router = APIRouter(prefix="/admin")
    caps: dict[str, CapabilityHandler] = dict(capabilities or {})

    @router.post("/initiate-handshake")
    async def initiate_handshake(request: Request) -> dict[str, Any]:
        body = await request.json()
        peer_manifest_url: str = body["peer_manifest_url"]
        requested_grants: Optional[list[str]] = body.get("requested_grants")

        session = agent.new_session()
        async with httpx.AsyncClient(timeout=15.0) as client:
            manifest_res = await client.get(peer_manifest_url)
            manifest_res.raise_for_status()
            peer_manifest_json = manifest_res.text

            peer_manifest = json.loads(peer_manifest_json)["manifest"]
            # Wire field is `offered_capabilities` (see aitp-manifest types).
            offered = list(peer_manifest.get("offered_capabilities", []))
            if requested_grants is None or len(requested_grants) == 0:
                grants = offered
            else:
                grants = list(requested_grants)

            hello = session.build_hello(peer_manifest_json, grants)
            hello_ep = peer_manifest["handshake_endpoint"]
            r1 = await client.post(
                hello_ep,
                content=hello,
                headers={"Content-Type": "application/json"},
            )
            r1.raise_for_status()
            session_id = r1.headers["x-aitp-session-id"]

            commit = session.process_hello_ack(r1.text, session_id)
            commit_ep = hello_ep.replace("/hello", "/commit")
            r2 = await client.post(
                commit_ep,
                content=commit,
                headers={
                    "Content-Type": "application/json",
                    "X-Aitp-Session-Id": session_id,
                },
            )
            r2.raise_for_status()

        tct_json = session.complete(r2.text)
        tct = json.loads(tct_json)
        peer_port = _port_from_url(peer_manifest["handshake_endpoint"])
        held_tcts[peer_port] = tct_json

        await emit_event(
            "handshake.complete",
            bootstrap,
            session_id=session_id,
            grants=tct["tct"]["grants"],
            peer_aid=tct["tct"]["issuer"],
            role="initiator",
        )
        return {
            "grants": tct["tct"]["grants"],
            "session_id": session_id,
            "peer_aid": tct["tct"]["issuer"],
            "peer_port": peer_port,
            "jti": tct["tct"].get("jti"),
        }

    @router.post("/invoke")
    async def invoke_capability(request: Request) -> Any:
        """Call ``capability`` on the peer at ``peer_port`` presenting our held
        TCT for that peer. On a 2xx response we return the peer's body verbatim
        (so workflow chains see the natural shape: ``{findings: ...}`` etc.).
        On a 4xx/5xx response we return ``{status_code, body, error: true}`` so
        the playground's probe step can observe the rejection without crashing
        the run."""
        body = await request.json()
        peer_port: int = int(body["peer_port"])
        capability: str = body["capability"]
        payload = body.get("payload")

        tct_json = held_tcts.get(peer_port)
        if not tct_json:
            raise HTTPException(
                status_code=412,
                detail=f"No TCT held for port {peer_port} — run handshake first",
            )

        if isinstance(payload, (dict, list)):
            content = json.dumps(payload)
        else:
            content = "" if payload is None else str(payload)

        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"http://localhost:{peer_port}/capabilities/{capability}",
                content=content,
                headers={
                    "Content-Type": "application/json",
                    "X-AITP-TCT": tct_json,
                },
            )
        try:
            inner_body: Any = r.json()
        except Exception:  # noqa: BLE001
            inner_body = r.text
        if r.is_success:
            return inner_body
        return {
            "error": True,
            "status_code": r.status_code,
            "body": inner_body,
        }

    @router.post("/delegate")
    async def issue_delegation(request: Request) -> dict[str, Any]:
        """Issue a DelegationToken using a TCT this agent holds.

        Body:
          - ``held_tct_peer_port``: the port whose handshake produced the
            held TCT. We look it up in ``held_tcts[port]``.
          - ``delegatee_manifest_url``: where to fetch the delegatee's
            manifest. We use its AID and identity_hint.public_key to bind
            the delegation.
          - ``scope``: capabilities to delegate. Must be a subset of the
            held TCT's grants — the SDK enforces this at build time.
          - ``ttl_secs`` (optional): delegation lifetime; defaults to SDK
            default (3600).

        Returns ``{"delegation_token": <DelegationEnvelope JSON>}``.
        """
        body = await request.json()
        held_peer_port = int(body["held_tct_peer_port"])
        delegatee_manifest_url: str = body["delegatee_manifest_url"]
        scope: list[str] = list(body["scope"])
        ttl_secs = body.get("ttl_secs")

        held_tct_json = held_tcts.get(held_peer_port)
        if not held_tct_json:
            raise HTTPException(
                status_code=412,
                detail=f"No TCT held for port {held_peer_port} — run handshake first",
            )

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(delegatee_manifest_url)
            r.raise_for_status()
        delegatee_manifest = r.json()["manifest"]
        delegatee_aid = delegatee_manifest["aid"]
        delegatee_pk_b64u = delegatee_manifest["identity_hint"]["public_key"]

        token_json = agent.build_delegation(
            held_tct_json, delegatee_aid, delegatee_pk_b64u, scope, ttl_secs,
        )
        await emit_event(
            "delegation.issued", bootstrap,
            delegatee_aid=delegatee_aid, scope=scope,
        )
        return {
            "delegation_token": token_json,
            "delegatee_aid": delegatee_aid,
            "scope": scope,
        }

    @router.post("/redeem-delegation")
    async def redeem_delegation(request: Request) -> dict[str, Any]:
        """Present a DelegationToken to a peer's /aitp/delegation/redeem
        endpoint, receive a fresh TCT bound to our key, and store it in
        ``held_tcts`` keyed by the peer's port. Subsequent capability calls
        to that peer will present this redeemed TCT via /admin/invoke.

        Body:
          - ``redeem_url``: peer's redemption URL (typically
            ``http://host:port/aitp/delegation/redeem``).
          - ``delegation_token``: the DelegationEnvelope JSON received from
            the delegator's /admin/delegate response.
          - ``peer_port``: the port we'll use as the key in held_tcts.
        """
        body = await request.json()
        redeem_url: str = body["redeem_url"]
        token_json: str = body["delegation_token"]
        peer_port = int(body["peer_port"])

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                redeem_url,
                json={"delegation_token": token_json},
            )
            r.raise_for_status()
        # Peer returns a TctEnvelope JSON.
        tct_envelope = r.text
        held_tcts[peer_port] = tct_envelope
        try:
            parsed = json.loads(tct_envelope)["tct"]
            await emit_event(
                "delegation.redeemed", bootstrap,
                peer_aid=parsed.get("issuer"),
                grants=parsed.get("grants"),
                jti=parsed.get("jti"),
            )
        except Exception:  # noqa: BLE001
            await emit_event("delegation.redeemed", bootstrap, peer_port=peer_port)
        return {"ok": True, "peer_port": peer_port}

    @router.post("/revoke-tct")
    async def revoke_tct(request: Request) -> dict[str, Any]:
        """Add a TCT jti to this agent's local revocation set.

        Subsequent capability calls that present a TCT with this jti are
        rejected by ``AitpServer.verify_capability_tct`` before any signature
        check. This is the demo flow for RFC-AITP-0008 fail-closed behavior;
        the optional CP propagation (see /admin/refresh-revocations) lets a
        peer pick up the same jti from a signed revocation list.
        """
        body = await request.json()
        jti: str = body["jti"]
        revoked_jtis.add(jti)
        await emit_event("tct.revoked", bootstrap, jti=jti)
        return {"revoked": jti, "total_revoked": len(revoked_jtis)}

    @router.post("/enroll-with-cp")
    async def enroll_with_cp(request: Request) -> dict[str, Any]:
        """Self-enroll this agent into the Control Plane registry.

        Two-step CP flow per aitp-control-plane:

          1. POST /api/registry/enroll with the agent's ManifestEnvelope
             JSON to mint a short-lived (5 min) enrollment token. This
             endpoint is public.
          2. POST /api/registry/agents with the same manifest and
             Authorization: Bearer <token> from step 1 to register
             (or re-register) the agent.

        Body (optional override):
          - cp_base_url: explicit override; falls back to
            bootstrap['cp']['base_url'].

        We pass the agent's *current* manifest JSON (held by AitpServer
        and threaded into the admin router by the worker's main module
        — see ``manifest_provider`` argument on ``build_admin_router``).

        Returns the resulting registry entry shape, the enrollment-token
        ttl, and the agent's AID for cross-checking.
        """
        body = await request.json() if await request.body() else {}
        cp_base_url = body.get("cp_base_url") or (
            bootstrap.get("cp", {}).get("base_url") if isinstance(bootstrap.get("cp"), dict) else None
        )
        if not cp_base_url:
            raise HTTPException(
                status_code=412,
                detail="No CP base_url available (set CP_BASE_URL or pass cp_base_url in body)",
            )
        if manifest_provider is None:
            raise HTTPException(
                status_code=500,
                detail="agent worker did not wire manifest_provider into build_admin_router",
            )

        manifest_json = manifest_provider()
        base = cp_base_url.rstrip("/")
        # Step 1 — enroll, get a one-time token.
        async with httpx.AsyncClient(timeout=10.0) as client:
            enroll = await client.post(
                f"{base}/api/registry/enroll",
                content=manifest_json,
                headers={"Content-Type": "application/json"},
            )
            if not enroll.is_success:
                await emit_event(
                    "cp.enroll_failed", bootstrap,
                    stage="enroll", status_code=enroll.status_code,
                    body=enroll.text,
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"CP /enroll returned {enroll.status_code}: {enroll.text}",
                )
            enroll_resp = enroll.json()
            token = enroll_resp.get("token")
            if not token:
                raise HTTPException(
                    status_code=502, detail=f"CP /enroll returned no token: {enroll_resp}",
                )

            # Step 2 — register the manifest using the token.
            register = await client.post(
                f"{base}/api/registry/agents",
                content=manifest_json,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
            if not register.is_success:
                await emit_event(
                    "cp.enroll_failed", bootstrap,
                    stage="register", status_code=register.status_code,
                    body=register.text,
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"CP /agents returned {register.status_code}: {register.text}",
                )
            registered = register.json()

        await emit_event(
            "cp.enroll_succeeded", bootstrap,
            aid=registered.get("aid"),
            registered_at=registered.get("registeredAt"),
        )
        return {
            "enrolled": True,
            "aid": registered.get("aid"),
            "registered_at": registered.get("registeredAt"),
            "token_expires_in": enroll_resp.get("expiresIn"),
        }

    @router.post("/refresh-revocations")
    async def refresh_revocations(request: Request) -> dict[str, Any]:
        """Pull the Control Plane's signed revocation list and merge every
        jti into this agent's local deny-set.

        This is how a TCT holder learns that its token was revoked: the
        original issuer marks the jti on the CP (via /api/revocation/entries),
        and any peer that consults the CP list will then short-circuit any
        capability call that presents that jti — without ever asking the
        issuer.

        Body (all optional):
          - cp_base_url: explicit override. When omitted, the agent uses
            ``bootstrap['cp']['base_url']`` (set by the playground supervisor
            from CP_BASE_URL at spawn time).
          - cp_api_key: bearer token for gated CP routes. The well-known
            revocation list is public so this is rarely needed, but we
            accept it for forward-compat.

        Returns the count of jtis now in the local deny set, plus how many
        new entries this refresh added.
        """
        body = await request.json() if await request.body() else {}
        cp_base_url = body.get("cp_base_url") or (
            bootstrap.get("cp", {}).get("base_url") if isinstance(bootstrap.get("cp"), dict) else None
        )
        cp_api_key = body.get("cp_api_key") or (
            bootstrap.get("cp", {}).get("api_key") if isinstance(bootstrap.get("cp"), dict) else None
        )
        if not cp_base_url:
            return {"revoked_count": len(revoked_jtis), "added": 0, "skipped": "no cp configured"}

        url = f"{cp_base_url.rstrip('/')}/.well-known/aitp-revocation-list"
        headers = {"Authorization": f"Bearer {cp_api_key}"} if cp_api_key else {}
        added = 0
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            await emit_event(
                "revocation.refresh_failed", bootstrap, error=str(exc),
            )
            return {"revoked_count": len(revoked_jtis), "added": 0, "error": str(exc)}

        # Same envelope-tolerant parse as the playground's CpClient: accept
        # either {"revocation_list": {"entries": [...]}} or
        # {"entries": [...]} at the top level.
        entries: list[Any] = []
        if isinstance(data, dict):
            inner = data.get("revocation_list") or data
            if isinstance(inner, dict):
                entries = list(inner.get("entries") or [])
        for entry in entries:
            jti_val = entry.get("jti") if isinstance(entry, dict) else entry
            if isinstance(jti_val, str) and jti_val and jti_val not in revoked_jtis:
                revoked_jtis.add(jti_val)
                added += 1

        await emit_event(
            "revocation.list_fetched", bootstrap,
            jti_count=len(entries), added=added,
        )
        return {"revoked_count": len(revoked_jtis), "added": added}

    @router.post("/self-execute")
    async def self_execute(request: Request) -> Any:
        """Run one of our own registered capabilities. No peer call, no TCT.

        Used when the scenario step's `agent` matches the agent that offers the
        capability — the playground asks the agent to execute its own work
        without a round-trip through the AITP handshake/invoke flow.
        """
        body = await request.json()
        capability: str = body["capability"]
        payload = body.get("payload")
        handler = caps.get(capability)
        if handler is None:
            raise HTTPException(
                status_code=404,
                detail=f"capability {capability} not registered on this agent",
            )
        await emit_event("capability.self_execute", bootstrap, capability=capability)
        return await handler(payload)

    return router


def _port_from_url(url: str) -> int:
    # http://host:PORT/path
    host_part = url.split("://", 1)[-1].split("/", 1)[0]
    if ":" in host_part:
        return int(host_part.split(":", 1)[1])
    return 80
