"""Service configuration loaded from environment."""
from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    port: int = 8000
    host: str = "0.0.0.0"
    scenarios_dir: str = "./scenarios"
    registry_cache_ttl_ms: int = 0
    agent_base_port: int = 8100
    agent_python: str = "python3"
    playground_base_url: str = "http://localhost:8000"
    # Public origin this service advertises for agents it hosts (federated /
    # cross-domain demos). When set, agents hosted via POST /hosted-agents
    # advertise their handshake_endpoint + did:web serviceEndpoint at
    # ``{public_scheme}://{public_host}`` instead of ``http://localhost:{port}``,
    # so a peer on another service resolves and dials a real cross-origin URL.
    # Empty (the default) means "no public origin" — behaves exactly as before.
    public_host: str = ""
    public_scheme: str = "http"
    # NOTE: the did:web-over-http allowlist (test-only escape hatch for the
    # Level 1 federated stack) is deliberately NOT a Settings field.
    # `trust/resolver.py` reads `AITP_DIDWEB_INSECURE_HOSTS` directly from
    # `os.environ` — a raw read, not through pydantic — because `resolve_did_web`
    # is a module-level coroutine with no Settings access, called from both
    # `api/hosted.py` and `trust/orchestrator.py`. Threading Settings into it
    # for a test-only value would be more machinery than the feature deserves.
    # See DECISIONS.md D-19.
    cp_base_url: str = ""
    cp_api_key: str = ""
    # The control plane's AID, pinned. Verifying a revocation snapshot without
    # a pinned expected issuer is close to worthless — the snapshot is
    # self-certifying, so ANY key can sign a well-formed one and it will
    # verify against its own declared issuer. The pin is the difference
    # between checking a signature and checking the *right* signature.
    #
    # Empty means "do not verify", which is the pre-0.6.0 posture and is
    # logged loudly at startup rather than assumed. In the compose stack the
    # CP's identity is deterministic (CP_AID_SEED_HEX), so this can be a known
    # constant; in a real deployment it comes from the CP's published manifest
    # at bootstrap, pinned once — discovery *without* pinning would reintroduce
    # the hole in a new shape.
    cp_aid: str = ""
    # ── Axis B: what to do about the ABSENCE of a fresh snapshot ─────────
    #
    # Kept strictly separate from Axis A (an unverifiable snapshot is always
    # discarded — RFC-AITP-0008 §1.5 makes that a MUST with no knob). These
    # settings govern only the case where we have no *fresh* verified snapshot
    # to consult. Collapsing the two axes into one switch is how a `soft_fail`
    # mode ends up reporting a forged snapshot as not-revoked, which is
    # exactly the behaviour `aitp_verifier`'s single `fail_mode` exhibits and
    # which this repo must not copy.
    #
    # `fail_closed` is the spec's own schema default (§3.1): "Deployments that
    # need availability-first behavior MUST opt into `soft_fail` or
    # `fail_open` explicitly; secure-by-default means revocation enforcement
    # does not silently degrade."
    revocation_fail_mode: str = "fail_closed"
    # RFC-AITP-0008 §3's example value. The timing envelope it has to cover:
    # the CP re-signs at most every 60s, so a served snapshot can already be
    # ~60s old on arrival; the poll cadence below adds up to another 60s;
    # container clock skew is small but nonzero. 300s clears 60+60+skew with
    # better than 2x margin, and the CP's signed expires_at defaults to 3600s,
    # so expiry is never the binding constraint in the demo.
    revocation_max_staleness_secs: int = 300
    # RFC-AITP-0008 §1.4: a consuming peer SHOULD poll. Without a cadence a
    # staleness deadline is either meaningless (nothing refreshes) or a time
    # bomb for a long-running scenario.
    revocation_poll_secs: int = 60
    cp_timeout_ms: int = 5000
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    log_level: str = "INFO"
    # When set, persist run records + events to this SQLite file so they
    # survive a service restart. Empty (the default) keeps the in-memory
    # RunStore — fast, ephemeral, no I/O.
    run_history_db: str = ""

    @property
    def scenarios_path(self) -> Path:
        return Path(self.scenarios_dir).resolve()


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
