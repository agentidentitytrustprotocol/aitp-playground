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

import aitp
import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from oidc import OidcContext
from revocation_state import RevocationState
from tct_claims import decode_claims, tct_event
from telemetry import emit_event

CapabilityHandler = Callable[[Any], Awaitable[Any]]
ManifestProvider = Callable[[], str]


async def _verify_peer_manifest(
    envelope_json: str, source_url: str, bootstrap: dict[str, Any]
) -> dict[str, Any]:
    """Verify a fetched peer ManifestEnvelope, then return its inner manifest.

    Every field this agent reads out of a peer manifest — the AID it delegates
    to, the handshake endpoint it dials, the capabilities it requests — comes
    from an unauthenticated HTTP fetch. Verifying the envelope first is what
    makes those fields the peer's own claims rather than whatever answered at
    that URL.

    What this establishes, and what it does not: `verify_manifest_json` checks
    the envelope signature against the key embedded in the manifest's own
    `aid`. AITP AIDs are self-certifying, so that proves the manifest was
    minted by the holder of that AID. It does **not** prove that AID is the
    peer you meant — for `did:web` that binding comes from the DID document
    (`trust/resolver.py`), which the federated stack resolves over plain HTTP
    under `AITP_DIDWEB_INSECURE_HOSTS`. Verification here closes substitution
    by anything that cannot produce a self-consistent envelope; it is not a
    trust anchor.

    Raises `HTTPException(502)` — the failure is in the upstream peer's
    response, not in this agent's caller.
    """
    async def _reject(cause: str, detail: str) -> None:
        # A verification failure must never be readable as a transport blip.
        # The `cause` is the field that separates "the peer's manifest does not
        # verify" from "the peer was unreachable" (which raises upstream and
        # never reaches here) — the same fetch-vs-verify distinction Phase 6
        # requires for revocation.
        await emit_event(
            "manifest.verify_failed",
            bootstrap,
            cause=cause,
            source_url=source_url,
        )
        raise HTTPException(status_code=502, detail=detail)

    try:
        aitp.verify_manifest_json(envelope_json)
    except Exception as exc:  # noqa: BLE001 — the SDK raises RuntimeError/ValueError
        message = str(exc)
        cause = "expired" if "expired" in message.lower() else "signature_invalid"
        await _reject(
            cause,
            f"peer manifest from {source_url} failed verification ({cause}): "
            f"{message}. Refusing to read an AID or endpoint out of an "
            "unverified manifest.",
        )
    try:
        envelope = json.loads(envelope_json)
    except ValueError as exc:
        await _reject(
            "malformed",
            f"peer manifest from {source_url} is not JSON: {exc}",
        )
    manifest = envelope.get("manifest") if isinstance(envelope, dict) else None
    if not isinstance(manifest, dict):
        await _reject(
            "malformed",
            f"peer manifest from {source_url} has no `manifest` body",
        )
    return manifest


def build_admin_router(
    *,
    agent,                                       # aitp.AitpAgent
    bootstrap: dict[str, Any],
    held_tcts: dict[int, str],                   # peer_port -> tct_token (mutated in place)
    revocation: RevocationState,                 # mutated by /admin/revoke-tct + refresh
    capabilities: Optional[dict[str, CapabilityHandler]] = None,
    manifest_provider: Optional[Callable[[], str]] = None,
    issued_tcts: Optional[dict[str, str]] = None,  # peer_aid -> tct_token (responder-side)
    held_vouchers: Optional[dict[int, str]] = None,  # peer_port -> grant_voucher token
) -> APIRouter:
    """Build the /admin router. ``capabilities`` maps capability name to an
    async handler invoked by ``/admin/self-execute``. The handler receives the
    parsed JSON payload (str/dict/None) and returns a JSON-serializable value.

    ``revocation`` is the same state object referenced by ``AitpServer`` so that
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
    oidc = OidcContext(bootstrap)
    # v0.2 grant vouchers, keyed by the same peer_port as ``held_tcts``. A
    # delegation is built from the voucher the handshake (or a redemption)
    # handed us, not from the TCT. Router-local by default so the worker
    # mains need no extra wiring; persists for the worker's lifetime.
    if held_vouchers is None:
        held_vouchers = {}

    def _new_session():
        if oidc.enabled:
            return agent.new_session(jwks=oidc.jwks, trust_anchors=oidc.trust_anchors)
        return agent.new_session()

    @router.post("/initiate-handshake")
    async def initiate_handshake(request: Request) -> dict[str, Any]:
        body = await request.json()
        peer_manifest_url: str = body["peer_manifest_url"]
        requested_grants: Optional[list[str]] = body.get("requested_grants")

        session = _new_session()
        async with httpx.AsyncClient(timeout=15.0) as client:
            manifest_res = await client.get(peer_manifest_url)
            manifest_res.raise_for_status()
            peer_manifest_json = manifest_res.text

            # Verify BEFORE reading any field out of the envelope — the AID
            # below is handed straight to the handshake as the peer identity.
            peer_manifest = await _verify_peer_manifest(
                peer_manifest_json, peer_manifest_url, bootstrap
            )
            # Wire field is `offered_capabilities` (see aitp-manifest types).
            offered = list(peer_manifest.get("offered_capabilities", []))
            if requested_grants is None or len(requested_grants) == 0:
                grants = offered
            else:
                grants = list(requested_grants)

            mint_cb = None
            if oidc.identity_type == "oidc":
                peer_aid = peer_manifest.get("aid", "")
                mint_cb = oidc.mint_jwt_for(audience=peer_aid, agent=agent)
            hello = session.build_hello(peer_manifest_json, grants, oidc_mint_jwt=mint_cb)
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

        # v0.2: complete() returns
        # ``{"tct": "<compact JWS>", "grant_voucher": "<compact JWS>"|null}``.
        completed = json.loads(session.complete(r2.text))
        tct_token = completed["tct"]
        claims = decode_claims(tct_token)
        peer_port = _port_from_url(peer_manifest["handshake_endpoint"])
        held_tcts[peer_port] = tct_token
        if completed.get("grant_voucher"):
            held_vouchers[peer_port] = completed["grant_voucher"]

        await emit_event(
            "handshake.complete",
            bootstrap,
            session_id=session_id,
            tct=tct_event(tct_token),
            grants=claims.get("grants", []),
            peer_aid=claims.get("iss"),
            role="initiator",
        )
        return {
            "grants": claims.get("grants", []),
            "session_id": session_id,
            "peer_aid": claims.get("iss"),
            "peer_port": peer_port,
            "jti": claims.get("jti"),
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
        # Where the peer's capability endpoints live. In-process scenario runs
        # leave this unset and the peer is reachable at localhost:{peer_port}.
        # Cross-origin (federated) callers pass the peer's public base URL
        # (e.g. https://org-b.aitp.test) so the call actually crosses the
        # domain boundary instead of dialing our own loopback.
        peer_base_url: str = (body.get("peer_base_url") or f"http://localhost:{peer_port}").rstrip("/")

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
                f"{peer_base_url}/capabilities/{capability}",
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

    @router.get("/held-tct")
    async def get_held_tct(peer_port: int) -> dict[str, Any]:
        """Return the compact-JWS TCT this agent holds for ``peer_port``
        (the token the peer issued to us during the handshake), plus our own
        AID. Read-only. Used by the RFC-AITP-0010 coordinator to gather the
        participant-held, coordinator-issued tokens it needs to build a
        session bundle — under v0.2 the responder/issuer never receives its
        own issued token back, so the holders supply them.
        """
        token = held_tcts.get(peer_port)
        if not token:
            raise HTTPException(
                status_code=412,
                detail=f"No TCT held for port {peer_port} — run handshake first",
            )
        return {"aid": agent.aid, "peer_port": peer_port, "tct_token": token}

    @router.post("/renew-tct")
    async def renew_tct(request: Request) -> dict[str, Any]:
        """Holder side of RFC-AITP-0005 §10 TCT renewal.

        Body:
          - ``peer_port``: the issuer's port; we look up our held TCT in
            ``held_tcts[peer_port]``, build a renewal request via the
            SDK, POST it to the issuer's ``/admin/process-renewal``, and
            swap our held TCT to the freshly issued envelope.

        Returns the new TCT's ``{jti, expires_at}`` so the runner can
        emit a structured event.
        """
        body = await request.json()
        peer_port = int(body["peer_port"])

        current_tct_token = held_tcts.get(peer_port)
        if not current_tct_token:
            raise HTTPException(
                status_code=412,
                detail=f"No TCT held for port {peer_port} — run handshake first",
            )

        request_payload = agent.build_renewal_request(current_tct_token)

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"http://localhost:{peer_port}/admin/process-renewal",
                content=request_payload,
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
        # Issuer returns ``{"tct": "<compact JWS>", "grant_voucher": ...}``.
        renewed = json.loads(r.text)
        new_token = renewed["tct"]
        held_tcts[peer_port] = new_token
        if renewed.get("grant_voucher"):
            held_vouchers[peer_port] = renewed["grant_voucher"]

        claims = decode_claims(new_token)
        await emit_event(
            "tct.renewal.requested", bootstrap,
            tct=tct_event(new_token),
            jti=claims.get("jti"),
            peer_port=peer_port,
        )
        return {
            "jti": claims.get("jti"),
            "expires_at": claims.get("exp"),
            "issuer": claims.get("iss"),
            "subject": claims.get("sub"),
        }

    @router.post("/export-session-bundle")
    async def export_session_bundle(request: Request) -> dict[str, Any]:
        """RFC-AITP-0010 coordinator side. Build a SessionBundleEnvelope
        from the TCTs this agent has issued to the listed participants.

        Body:
          - ``participants``: ``{agent_id: peer_port}``. We map peer_port
            back to a held TCT via this agent's outbound-handshake
            records — but in the bundle the *issued* TCTs are the
            relevant ones. Each participant must have completed an
            inbound handshake with us first (so we know the AID + held
            TCT); the responder records these in ``self._sessions`` on
            ``AitpServer``. To keep this admin route framework-light,
            the test fixture instead passes ``participant_tcts``
            directly when the responder cache isn't reachable.
          - ``participant_tcts`` (optional): ``[{aid, tct_token}]`` (the
            legacy ``tct_envelope`` key is still accepted) — if provided,
            skip the responder-cache lookup. ``tct_token`` is a v0.2
            compact-JWS TCT string.

        Returns ``{bundle_envelope, session_id, participant_aids}``.
        """
        import time
        import uuid

        import aitp

        body = await request.json()
        provided_tcts: list[dict[str, Any]] = list(
            body.get("participant_tcts") or []
        )

        # Default path: derive participants from issued_tcts. RFC-0010
        # bundles require each participant TCT to have been *issued by*
        # the coordinator — i.e., the coordinator is the responder
        # (issuer) side of the handshake. We populate issued_tcts on
        # commit (see AitpServer); the playground engine drives
        # handshakes such that the coordinator is the responder.
        if not provided_tcts and issued_tcts:
            for recipient_aid, tct_token in issued_tcts.items():
                provided_tcts.append({
                    "aid": recipient_aid,
                    "tct_token": tct_token,
                })

        if not provided_tcts:
            raise HTTPException(
                status_code=412,
                detail="no participants — handshake first or pass participant_tcts",
            )

        builder = aitp.SessionBundleBuilder(agent)
        session_id = str(uuid.uuid4())
        builder.session_id(session_id)
        builder.issued_at(int(time.time()))
        participant_aids: list[str] = []
        for p in provided_tcts:
            builder.participant(p["aid"], p.get("tct_token") or p["tct_envelope"])
            participant_aids.append(p["aid"])
        bundle_envelope = builder.build()
        await emit_event(
            "session.bundle.exported", bootstrap,
            session_id=session_id,
            participant_count=len(participant_aids),
        )
        return {
            "bundle_envelope": bundle_envelope,
            "session_id": session_id,
            "participant_aids": participant_aids,
        }

    @router.post("/verify-session-bundle")
    async def verify_session_bundle_endpoint(request: Request) -> dict[str, Any]:
        """Verifier side of RFC-AITP-0010. Returns the BundleOutcome dict
        (``{kind, active_aids, dropped_aids}``) from the SDK so the
        playground engine can attach it as the step result.
        """
        import aitp

        body = await request.json()
        envelope: str = body["bundle_envelope"]
        outcome = aitp.verify_session_bundle(envelope, agent.aid)
        await emit_event(
            "session.bundle.verified", bootstrap,
            kind=outcome.get("kind"),
            active_count=len(outcome.get("active_aids", [])),
        )
        return outcome

    @router.post("/process-renewal")
    async def process_renewal(request: Request) -> Response:
        """Issuer side of TCT renewal. Verifies the renewal request and
        mints a fresh TctEnvelope JSON. The manifest-expiry bound is
        derived from this agent's manifest TTL.
        """
        import time

        request_payload = (await request.body()).decode("utf-8")
        cfg = bootstrap.get("aitp", {})
        ttl_secs = int(cfg.get("ttl_secs", 3600))
        manifest_exp = int(time.time()) + ttl_secs
        # Returns ``{"tct": "<compact JWS>", "grant_voucher": ...}``; we hand
        # the whole envelope back to the holder, who stores both halves.
        new_tct_envelope_json = agent.process_renewal_request(
            request_payload, manifest_exp, ttl_secs,
        )
        new_token = json.loads(new_tct_envelope_json)["tct"]
        claims = decode_claims(new_token)
        await emit_event(
            "tct.renewal.issued", bootstrap,
            tct=tct_event(new_token),
            jti=claims.get("jti"),
            subject=claims.get("sub"),
        )
        return Response(new_tct_envelope_json, media_type="application/json")

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

        # v0.2: a delegation is built from the grant voucher the handshake
        # handed us alongside the TCT, not from the TCT itself. The
        # delegatee's key binding is derived from its AID by the SDK, so we
        # no longer pass its public key.
        held_voucher = held_vouchers.get(held_peer_port)
        if not held_voucher:
            raise HTTPException(
                status_code=412,
                detail=(
                    f"No grant voucher held for port {held_peer_port} — "
                    f"run handshake first"
                ),
            )

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(delegatee_manifest_url)
            r.raise_for_status()
        # Verify BEFORE reading the AID: this value is the delegation's
        # recipient. Anything that can answer at delegatee_manifest_url and is
        # not checked here receives the delegation, scope and all.
        delegatee_manifest = await _verify_peer_manifest(
            r.text, delegatee_manifest_url, bootstrap
        )
        delegatee_aid = delegatee_manifest["aid"]

        token_json = agent.build_delegation(
            held_voucher, delegatee_aid, scope, ttl_secs,
        )
        await emit_event(
            "delegation.issued", bootstrap,
            # Include the signed delegation token so the CP can project a
            # delegations row. The CP's parseDelegation reads
            # payload.tct.{token,claims} for jti / src_jti (parent) /
            # iss (delegator) / sub (delegatee); without the token it only
            # saw delegatee_aid + scope and dropped the event.
            tct=tct_event(token_json),
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
        # Peer returns ``{"tct": "<compact JWS>", "grant_voucher": ...}``: a
        # fresh TCT bound to our key, plus a voucher so we can re-delegate.
        try:
            redeemed = json.loads(r.text)
            tct_token = redeemed["tct"]
            held_tcts[peer_port] = tct_token
            if redeemed.get("grant_voucher"):
                held_vouchers[peer_port] = redeemed["grant_voucher"]
            claims = decode_claims(tct_token)
            await emit_event(
                "delegation.redeemed", bootstrap,
                tct=tct_event(tct_token),
                peer_aid=claims.get("iss"),
                grants=claims.get("grants"),
                jti=claims.get("jti"),
            )
        except (ValueError, KeyError):
            held_tcts[peer_port] = r.text
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
        # Local, deliberately separate from anything the CP says. A snapshot
        # refresh replaces the CP-derived set wholesale; it must never clear
        # an operator's own revocation.
        revocation.revoke_local(jti)
        await emit_event("tct.revoked", bootstrap, jti=jti)
        return {"revoked": jti, "total_revoked": len(revocation)}

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
        """Pull the Control Plane's revocation list and merge every
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
            return {
                "revoked_count": len(revocation),
                "added": 0,
                "skipped": "no cp configured",
            }

        # The pinned issuer. Without it we cannot verify — any key can sign a
        # well-formed snapshot that validates against its own declared issuer,
        # so an unpinned check would confirm only that *somebody* signed it.
        #
        # Deliberately NOT accepted from the request body: the URL may be
        # overridden per-call, but if the pin were overridable too, an
        # attacker-chosen endpoint could simply supply its own AID and the
        # verification would become a formality.
        expected_issuer = (
            bootstrap.get("cp", {}).get("aid")
            if isinstance(bootstrap.get("cp"), dict)
            else None
        )

        url = f"{cp_base_url.rstrip('/')}/.well-known/aitp-revocation-list"
        headers = {"Authorization": f"Bearer {cp_api_key}"} if cp_api_key else {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                envelope_json = resp.text
        except Exception as exc:  # noqa: BLE001
            # Transport failure only. This must never alias a verification
            # failure: collapsing them is how a signing-convention break gets
            # triaged as a network blip.
            await emit_event("revocation.refresh_failed", bootstrap, error=str(exc))
            return {"revoked_count": len(revocation), "added": 0, "error": str(exc)}

        async def _discard(cause: str, detail: str) -> dict[str, Any]:
            """RFC-AITP-0008 §1.5: an unverifiable snapshot is DISCARDED.

            Not applied, not partially applied, not merged. The previously
            verified snapshot stays in force and the deny-set is untouched.
            This is a MUST, so it has no mode knob — `revocation_fail_mode`
            governs the *absence* of a fresh snapshot, never its authenticity.
            """
            await emit_event(
                "revocation.verify_failed", bootstrap, cause=cause, detail=detail
            )
            return {
                "revoked_count": len(revocation),
                "added": 0,
                "discarded": cause,
                "detail": detail,
            }

        if not expected_issuer:
            return await _discard(
                "no_expected_issuer",
                "no CP AID pinned (set CP_AID) — refusing to apply an "
                "unverifiable revocation snapshot",
            )
        if not hasattr(aitp, "verify_revocation_list"):
            return await _discard(
                "sdk_cannot_verify",
                "installed aitp-sdk has no verify_revocation_list (needs "
                ">=0.6.0) — refusing to apply an unverified snapshot",
            )

        try:
            aitp.verify_revocation_list(envelope_json, expected_issuer)
        except Exception as exc:  # noqa: BLE001
            cause = getattr(exc, "code", None) or "signature_invalid"
            return await _discard(cause, str(exc))

        # Verified. Parse the exact RFC-AITP-0008 §1.5 envelope — no tolerant
        # fallback. Once the signature is checked, accepting a second shape is
        # a downgrade path: a body the parser accepts but the verifier did not
        # sign over.
        body_obj = json.loads(envelope_json)["revocation_list"]
        jtis = [
            e["jti"]
            for e in body_obj.get("entries", [])
            if isinstance(e, dict) and isinstance(e.get("jti"), str)
        ]
        previous = revocation.snapshot_entry_count
        # Wholesale replacement, not a merge — a snapshot is the issuer's
        # complete current deny-set, so a jti it no longer lists is no longer
        # revoked by them.
        revocation.apply_snapshot(
            jtis,
            published_at=int(body_obj["published_at"]),
            expires_at=int(body_obj["expires_at"]),
        )

        await emit_event(
            "revocation.list_fetched",
            bootstrap,
            jti_count=len(jtis),
            added=max(0, len(jtis) - previous),
            verified=True,
            issuer=expected_issuer,
        )
        return {
            "revoked_count": len(revocation),
            "snapshot_count": len(jtis),
            "added": max(0, len(jtis) - previous),
            "verified": True,
        }

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
    # scheme://host[:PORT]/path — used as a stable per-peer key for held TCTs.
    scheme = url.split("://", 1)[0] if "://" in url else "http"
    host_part = url.split("://", 1)[-1].split("/", 1)[0]
    if ":" in host_part:
        return int(host_part.split(":", 1)[1])
    return 443 if scheme == "https" else 80
