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
    # Comma-separated host (or ".suffix") allowlist for which did:web hosts may
    # be resolved over plain http instead of https. Test-only escape hatch for
    # the Level 1 federated stack (e.g. ".aitp.test"); production leaves this
    # empty so did:web always resolves over https. Read directly by
    # trust/resolver.py from the AITP_DIDWEB_INSECURE_HOSTS env var too.
    didweb_insecure_hosts: str = ""
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
