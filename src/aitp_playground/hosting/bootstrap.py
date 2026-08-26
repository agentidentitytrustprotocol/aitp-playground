"""Build and write the per-agent bootstrap JSON file."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from ..config import Settings
from ..registry.models import AgentManifest, AgentSpec
from .identity import derive_seed_hex


class BootstrapBuilder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

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
    ) -> dict[str, Any]:
        seed_hex = derive_seed_hex(run_id, agent_spec.id, org=agent_spec.org)
        # A federated/hosted agent advertises its *public* origin so a peer on
        # another service resolves and dials a real cross-origin URL. Without a
        # public origin the agent stays reachable only at localhost — the
        # default for in-process scenario runs.
        base_origin = public_origin.rstrip("/") if public_origin else f"http://localhost:{port}"
        handshake_ep = f"{base_origin}/aitp/handshake/hello"
        did_web_scheme = base_origin.split("://", 1)[0]

        cp_block: dict[str, Any] = {}
        if self.settings.cp_base_url:
            cp_block["base_url"] = self.settings.cp_base_url
        if self.settings.cp_api_key:
            cp_block["api_key"] = self.settings.cp_api_key
        # Agents run as separate processes and read CP config from this block,
        # never from Settings — so the expected-issuer pin has to travel here
        # or /admin/refresh-revocations cannot verify anything.
        if self.settings.cp_aid:
            cp_block["aid"] = self.settings.cp_aid

        # Per-agent overrides of manifest defaults: signing_suite + the
        # OIDC issuer/subject when identity_type=oidc.
        signing_suite = (
            agent_spec.signing_suite
            or resolved_manifest.spec.aitp.signing_suite
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
                "oidc_issuer": resolved_manifest.spec.aitp.oidc_issuer,
                "oidc_subject": resolved_manifest.spec.aitp.oidc_subject,
            },
            "peers": peers,
            "playground": {
                "telemetry_url": f"{self.settings.playground_base_url}/internal/telemetry",
                "run_id": run_id,
            },
            "inputs": dict(inputs),
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
        path.write_text(json.dumps(bootstrap, indent=2))
        return str(path)
