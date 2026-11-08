"""FastAPI app factory + dependency wiring."""
from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import agents as agents_api
from .api import cp as cp_api
from .api import dashboard as dashboard_api
from .api import health as health_api
from .api import hosted as hosted_api
from .api import metrics as metrics_api
from .api import registry as registry_api
from .api import runs as runs_api
from .api import quota as quota_api
from .api import cp_proxy as cp_proxy_api
from .api import telemetry as telemetry_api
from .api import webhooks as webhooks_api
from .config import Settings, get_settings
from .cp_client.client import CpClient
from .errors import install_handlers
from .hosting.adapters.registry import build_default_adapter_registry
from .hosting.bootstrap import BootstrapBuilder
from .hosting.hosted import HostedAgentManager
from .hosting.loopback import AgentLoopback
from .hosting.port_allocator import PortAllocator
from .hosting.supervisor import AgentSupervisor
from .limits import RunLimiter, SpendLimits
from .observability.metrics import LLM_QUOTA_STORE_UP, metrics
from .quota import select_store
from .registry.service import RegistryService
from .runner.engine import ScenarioRunner
from .runner.store import build_run_store
from .security import ApiKeyMiddleware, is_loopback_url
from .trust.orchestrator import TrustOrchestrator


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


async def sweep_cp_webhooks(cp: CpClient, settings: Settings, runner: ScenarioRunner) -> int:
    """Delete the CP webhooks a previous process of this deployment left
    behind. Every one points at a run of that process, and no run survives a
    restart: its agents are gone, so nothing more will happen on it. Ours are
    the ones under our own `/webhooks/cp/` URL; one this process has already
    subscribed (a run that started while the sweep ran) is spared. Returns how
    many were deleted. Best-effort: with the CP unreachable they stay, and the
    receiver's 410 for an unknown run tells the CP to stop."""
    hooks = await cp.list_webhooks()
    if not hooks:
        return 0
    prefix = f"{settings.playground_base_url.rstrip('/')}/webhooks/cp/"
    deleted = 0
    for hook in hooks:
        hook_id, url = hook.get("id"), hook.get("url")
        if not isinstance(hook_id, str) or not isinstance(url, str):
            continue
        if not url.startswith(prefix) or hook_id in runner.webhook_ids:
            continue
        if await cp.delete_webhook(hook_id):
            deleted += 1
    if deleted:
        logging.getLogger(__name__).info("deleted %d stale CP webhook(s)", deleted)
    return deleted


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)
    if not settings.playground_api_key.strip():
        logging.getLogger(__name__).warning(
            "PLAYGROUND_API_KEY is not set: every authenticated route "
            "(run create/read/stream, hosted agents, CP views) answers 503"
        )
    logging.getLogger(__name__).info(
        "backend lockdown: AITP_PLAYGROUND_REQUIRE_API_KEY=%s",
        "true (every route but /healthz, the CP webhook and loopback /internal/* "
        "needs the key)" if settings.require_api_key else "false (per-route auth)",
    )
    if not is_loopback_url(settings.playground_internal_url):
        logging.getLogger(__name__).warning(
            "PLAYGROUND_INTERNAL_URL is not a loopback URL: agent telemetry goes "
            "over the network and, with AITP_PLAYGROUND_REQUIRE_API_KEY=true, is "
            "rejected by the gate (only direct-loopback /internal/* is exempt)"
        )

    registry = RegistryService(settings)
    port_alloc = PortAllocator(start=settings.agent_base_port)
    loopback = AgentLoopback(settings)
    supervisor = AgentSupervisor(loopback)
    telemetry_token = secrets.token_urlsafe(32)
    bootstrap_builder = BootstrapBuilder(
        settings, telemetry_token=telemetry_token, loopback=loopback
    )
    adapters = build_default_adapter_registry()
    cp = CpClient(settings)
    trust = TrustOrchestrator(cp, settings, loopback)
    run_store = build_run_store(settings.run_history_db or None)
    hosted = HostedAgentManager(
        registry=registry,
        bootstrap_builder=bootstrap_builder,
        adapters=adapters,
        supervisor=supervisor,
        settings=settings,
    )
    runner = ScenarioRunner(
        registry=registry,
        supervisor=supervisor,
        bootstrap_builder=bootstrap_builder,
        adapters=adapters,
        trust=trust,
        cp=cp,
        port_alloc=port_alloc,
        config=settings,
        store=run_store,
    )

    quota_store = select_store(settings)
    cp_proxy = cp_proxy_api.CpProxy(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sweep = asyncio.create_task(sweep_cp_webhooks(cp, settings, runner))
        # Opens the quota pool now so the first LLM call doesn't pay for it.
        # Never blocks startup: a down database just denies LLM calls.
        await quota_store.start()
        # 0 until a check proves otherwise when the store denies everything
        # or could not connect; charges keep it current afterwards.
        metrics.set(LLM_QUOTA_STORE_UP, 1.0 if quota_store.healthy else 0.0)
        yield
        sweep.cancel()
        await quota_store.aclose()
        await cp_proxy.aclose()
        # Best-effort cleanup of any agent processes still alive
        for run_id in list(run_store.list_ids()):
            supervisor.kill_run(run_id)
        hosted.stop_all()

    app = FastAPI(title="aitp-playground", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.state.runner = runner
    app.state.run_store = run_store
    app.state.supervisor = supervisor
    app.state.cp = cp
    app.state.hosted = hosted
    app.state.spend_limits = SpendLimits(settings)
    app.state.run_limiter = RunLimiter.from_settings(settings)
    app.state.quota_store = quota_store
    app.state.cp_proxy = cp_proxy
    app.state.telemetry_token = telemetry_token

    install_handlers(app)
    # Outermost: rejects before routing when the key is required, and always
    # records who the caller is (key fingerprint, trusted client IP).
    app.add_middleware(ApiKeyMiddleware, settings=settings)

    app.include_router(health_api.router)
    app.include_router(registry_api.router)
    app.include_router(runs_api.router)
    app.include_router(agents_api.router)
    app.include_router(hosted_api.router)
    app.include_router(telemetry_api.router)
    app.include_router(quota_api.router)
    app.include_router(metrics_api.router)
    app.include_router(webhooks_api.router)
    app.include_router(cp_api.router)
    app.include_router(cp_proxy_api.router)
    app.include_router(dashboard_api.router)

    return app


app = create_app()
