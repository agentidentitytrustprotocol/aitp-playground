"""Read AITP_BOOTSTRAP_FILE and construct an AitpAgent via aitp-py."""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlsplit

import aitp


def load_bootstrap() -> dict[str, Any]:
    path = os.environ.get("AITP_BOOTSTRAP_FILE", "")
    if not path:
        raise RuntimeError("AITP_BOOTSTRAP_FILE not set")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def create_agent(bootstrap: dict[str, Any]) -> "aitp.AitpAgent":
    cfg = bootstrap["aitp"]
    seed_hex: str = cfg["seed_hex"]
    suite: str = cfg.get("signing_suite", "ed25519")
    return aitp.AitpAgent.from_seed(bytes.fromhex(seed_hex), suite=suite)


def revocation_list_uri(handshake_endpoint: str) -> str:
    """This agent's list URL: the handshake endpoint's origin plus the SDK path.

    The origin, not the endpoint's directory: the list is served at the root
    (`aitp.REVOCATION_LIST_PATH`), the same place aitp-rs serves `ListRevoked`.
    """
    parts = urlsplit(handshake_endpoint)
    return f"{parts.scheme}://{parts.netloc}{aitp.REVOCATION_LIST_PATH}"


def get_manifest_json(agent: "aitp.AitpAgent", bootstrap: dict[str, Any]) -> str:
    cfg = bootstrap["aitp"]
    kwargs: dict[str, Any] = {
        "display_name": cfg["display_name"],
        "handshake_endpoint": cfg["handshake_endpoint"],
        "offered_caps": list(cfg["offered_caps"]),
        "ttl_secs": int(cfg.get("ttl_secs", 3600)),
        # RFC-AITP-0008 §1.4: every agent that issues TCTs advertises where its
        # signed revocation list is served. Without it a fail_closed verifier
        # that discovers lists through the Manifest rejects our TCTs.
        "revocation_list_uri": revocation_list_uri(cfg["handshake_endpoint"]),
    }
    identity_type = cfg.get("identity_type", "pinned_key")
    if identity_type == "oidc":
        kwargs["identity_type"] = "oidc"
        kwargs["oidc_issuer"] = cfg.get("oidc_issuer")
        kwargs["oidc_subject"] = cfg.get("oidc_subject")
    # RFC-AITP-0004 §5.1 step 8: a peer whose identity type is not listed
    # here is refused with INCOMPATIBLE_IDENTITY_TYPE. Passed explicitly so
    # the Manifest states it; the default is the SDK's own, [own type].
    kwargs["accepted_identity_types"] = list(cfg.get("accepted_identity_types") or [identity_type])
    return agent.build_manifest(**kwargs)
