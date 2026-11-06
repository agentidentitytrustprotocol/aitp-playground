"""Build and write the per-agent bootstrap JSON file."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from ..config import Settings
from ..registry.models import AgentManifest, AgentSpec
from .identity import derive_seed_hex
from .loopback import AgentLoopback


class BootstrapBuilder:
    def __init__(
        self,
        settings: Settings,
        telemetry_token: str | None = None,
        loopback: AgentLoopback | None = None,
    ) -> None:
        self.settings = settings
        self.loopback = loopback or AgentLoopback(settings)
        # Bearer agents send to POST /internal/telemetry. One per playground
        # process; create_app() shares it with the route via app.state.
        self.telemetry_token = telemetry_token or secrets.token_urlsafe(32)

    def build(
        self,
        *,
        run_id: str,
        agent_spec: AgentSpec,
        resolved_manifest: AgentManifest,
        port: int,
        peers: dict[str, dict[str, Any]],
        inputs: dict[str, Any],
        oidc: Any = None,
        public_origin: str | None = None,
        token_budget: int | None = None,
    ) -> dict[str, Any]:
        seed_hex = derive_seed_hex(run_id, agent_spec.id, org=agent_spec.org)
        # A federated/hosted agent advertises its *public* origin so a peer on
        # another service resolves and dials a real cross-origin URL. Without a
        # public origin the agent stays reachable only at localhost — the
        # default for in-process scenario runs, which serve TLS there and bind
        # loopback only. A public origin's TLS terminates in front of the agent
        # (federated/Caddyfile.org-*), which has to accept that proxy's traffic.
        base_origin = public_origin.rstrip("/") if public_origin else self.loopback.origin(port)
        handshake_ep = f"{base_origin}/aitp/handshake/hello"
        did_web_scheme = base_origin.split("://", 1)[0]

        # NOTE: an empty cp_block means "no control plane" — the bootstrap
        # omits the "cp" key entirely (see below), and agents read that as the
        # local-only posture. So every key here must stay conditional on a CP
        # actually being configured; unconditionally adding policy defaults
        # would make `bootstrap["cp"]` always present and silently erase that
        # signal.
        cp_block: dict[str, Any] = {}
        if self.settings.cp_base_url:
            cp_block["base_url"] = self.settings.cp_base_url
            # Axis B policy travels with the CP block: agents are separate
            # processes that never see Settings.
            cp_block["fail_mode"] = self.settings.revocation_fail_mode
            cp_block["max_staleness_secs"] = self.settings.revocation_max_staleness_secs
            cp_block["poll_secs"] = self.settings.revocation_poll_secs
            cp_block["list_ttl_secs"] = self.settings.revocation_list_ttl_secs
        if self.settings.cp_api_key:
            cp_block["api_key"] = self.settings.cp_api_key


        # Per-agent overrides of manifest defaults: signing_suite + the
        # OIDC issuer/subject when identity_type=oidc.
        signing_suite = (
            agent_spec.signing_suite
            or resolved_manifest.spec.aitp.signing_suite
        )
        accepted_identity_types = (
            agent_spec.accepted_identity_types
            or resolved_manifest.spec.aitp.accepted_identity_types
        )

        bootstrap: dict[str, Any] = {
            "run_id": run_id,
            "agent_id": agent_spec.id,
            "port": port,
            "aitp": {
                "seed_hex": seed_hex,
                "display_name": resolved_manifest.spec.aitp.display_name,
                "handshake_endpoint": handshake_ep,
                "offered_caps": list(resolved_manifest.spec.aitp.offered_caps),
                "did_web_host": agent_spec.did_web_host,
                "did_web_scheme": did_web_scheme,
                "ttl_secs": resolved_manifest.spec.aitp.ttl_secs,
                "signing_suite": signing_suite,
                "identity_type": resolved_manifest.spec.aitp.identity_type,
                "accepted_identity_types": (
                    list(accepted_identity_types) if accepted_identity_types else None
                ),
                "oidc_issuer": resolved_manifest.spec.aitp.oidc_issuer,
                "oidc_subject": resolved_manifest.spec.aitp.oidc_subject,
                "insecure_localhost_dangerous": self.settings.insecure_localhost_dangerous,
            },
            "listen_host": "0.0.0.0" if public_origin else "127.0.0.1",
            "loopback_tls": None if public_origin else self.loopback.agent_tls(),
            "peers": peers,
            "playground": {
                "telemetry_url": f"{self.settings.playground_base_url}/internal/telemetry",
                "telemetry_token": self.telemetry_token,
                "run_id": run_id,
            },
            # Spend guard enforced by agents/base/llm.py before every provider
            # call. The runner passes this agent's share of RUN_TOKEN_BUDGET;
            # a hosted agent (no run) gets the whole budget for its lifetime.
            "llm": {
                "token_budget": (
                    token_budget if token_budget is not None else self.settings.run_token_budget
                ),
                "max_tokens_per_call": self.settings.llm_max_tokens_per_call,
            },
            "inputs": dict(inputs),
            # Bearer for the agent's /admin routes (issue #95). Fresh per
            # spawn; the supervisor sends it, the agent's AdminAuthMiddleware
            # checks it. Pass it to supervisor.launch(admin_token=...).
            "admin_token": secrets.token_urlsafe(32),
        }
        if cp_block:
            bootstrap["cp"] = cp_block
        # When the run has OIDC agents, every agent (even pinned-key
        # ones) gets the public JWK + issuer URL so it can verify
        # OIDC peers via the SDK's JwksProvider. Only OIDC-identified
        # agents get the private seed (to mint their own JWTs).
        if oidc is not None:
            oidc_block: dict[str, Any] = {
                "issuer_url": oidc.issuer_url,
                "kid": oidc.kid,
                "public_jwk": dict(oidc.public_jwk),
            }
            if resolved_manifest.spec.aitp.identity_type == "oidc":
                oidc_block["private_seed_b64"] = oidc.private_seed_b64
            bootstrap["oidc"] = oidc_block
        return bootstrap

    def write(self, bootstrap: dict[str, Any]) -> str:
        tmpdir = Path(tempfile.gettempdir()) / "aitp-bootstrap"
        tmpdir.mkdir(parents=True, exist_ok=True)
        path = tmpdir / f"{bootstrap['run_id']}_{bootstrap['agent_id']}.json"
        # 0600: the file carries the agent's seed and admin token.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(bootstrap, indent=2))
        os.chmod(path, 0o600)  # O_CREAT's mode does not apply to an existing file
        return str(path)
