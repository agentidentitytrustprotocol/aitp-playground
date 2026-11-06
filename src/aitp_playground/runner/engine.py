"""ScenarioRunner: full end-to-end orchestration of a scenario run."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

import aitp
import httpx
import jsonschema

from ..capabilities import CapabilitiesProvider, get_capabilities, missing_sdk_features
from ..config import Settings
from ..cp_client.client import CpClient
from ..errors import PlaygroundError, SdkFeatureUnavailableError
from ..hosting.adapters.registry import AdapterRegistry
from ..hosting.bootstrap import BootstrapBuilder
from ..hosting.port_allocator import PortAllocator
from ..hosting.supervisor import AgentSupervisor, RunningAgent
from ..registry.models import ScenarioVersion, WorkflowStep
from ..registry.service import RegistryService
from ..trust.orchestrator import TrustOrchestrator
from .context import RunContext, RunEvent
from .result import RunResult
from .store import RunStore

logger = logging.getLogger(__name__)


class _RunCancelled(Exception):
    """Stops a run whose record turned ``cancelled`` mid-setup. It unwinds
    through the run's ``except`` so ports and bootstrap files are released;
    the cancel route already emitted the terminal ``run.cancelled``."""


def _classify_manifest_verify_failure(exc: BaseException) -> str:
    """Mirror of `agent_admin.classify_manifest_verify_failure`.

    Not a shared import. `agents/base` and `src/aitp_playground` are not
    reliably importable from each other: an agent subprocess gets
    `agents/base`+`agents` on `PYTHONPATH`
    (`hosting/adapters/base.py:_build_env`) but never `src`, while this
    service gets `agents/base` on `sys.path` only in Docker
    (`Dockerfile:PYTHONPATH`) and under pytest (`pyproject.toml`'s
    `pythonpath`) — NOT under the documented local dev command (`uv run
    uvicorn aitp_playground.main:app`). A shared import would pass CI and
    Docker and break local `uvicorn` with an `ImportError` nothing in the
    test suite would catch, since pytest puts both paths on `sys.path`.

    The two copies are pinned in sync by
    `tests/unit/test_manifest_verification.py`'s classifier-parity test — see
    `DECISIONS.md` D-15.
    """
    cause = getattr(exc, "code", None)
    if cause is None:
        cause = "malformed" if isinstance(exc, ValueError) else "unknown"
    return cause


def _held_delegation_chain(
    step_outputs: dict[str, Any], holder: str, issuer: str,
) -> list[str]:
    """Manifest URLs of the delegators behind ``holder``'s grant from
    ``issuer``, if ``holder`` got it by redeeming a delegation (most recent
    redemption wins). Empty when it holds the grant from its own handshake."""
    for out in reversed(list(step_outputs.values())):
        chain = out.get("delegation_chain") if isinstance(out, dict) else None
        if chain and chain.get("holder") == holder and chain.get("issuer") == issuer:
            return list(chain.get("issuer_manifest_urls", []))
    return []


class ScenarioRunner:
    def __init__(
        self,
        *,
        registry: RegistryService,
        supervisor: AgentSupervisor,
        bootstrap_builder: BootstrapBuilder,
        adapters: AdapterRegistry,
        trust: TrustOrchestrator,
        cp: CpClient,
        port_alloc: PortAllocator,
        config: Settings,
        store: RunStore,
        capabilities_provider: CapabilitiesProvider = get_capabilities,
    ) -> None:
        self.registry = registry
        self.supervisor = supervisor
        self.bootstrap_builder = bootstrap_builder
        self.loopback = bootstrap_builder.loopback
        self.adapters = adapters
        self.trust = trust
        self.cp = cp
        self.ports = port_alloc
        self.config = config
        self.store = store
        # The installed SDK's feature report; tests inject one to simulate a
        # wheel without the optional features.
        self.capabilities_provider = capabilities_provider
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        # CP webhooks this process subscribed; the startup sweep spares them.
        self.webhook_ids: set[str] = set()

    async def run(
        self,
        *,
        scenario_ref: str,
        inputs: dict[str, Any],
        run_label: Optional[str] = None,
        run_id: Optional[str] = None,
        template: Optional[str] = None,
    ) -> RunResult:
        run_id = run_id or str(uuid.uuid4())
        ctx = RunContext(
            run_id=run_id,
            scenario_ref=scenario_ref,
            run_label=run_label,
            store=self.store,
        )
        # A cancel can land while POST /runs still has the run pending; it
        # has already emitted run.cancelled, so the run must not start (#118).
        if not self.store.upsert_unless_cancelled(run_id, {
            "run_id": run_id, "status": "running", "scenario_ref": scenario_ref,
            "run_label": run_label, "outputs": {}, "events": [], "error": None,
        }):
            logger.info("Run %s was cancelled before it started", run_id)
            return RunResult(run_id=run_id, status="cancelled")

        # 1. Load scenario (optionally merged with a named template)
        try:
            scenario = self.registry.get_scenario_resolved(
                scenario_ref, template=template,
            )
        except PlaygroundError as exc:
            if not self._is_cancelled(run_id):  # a cancel already emitted the terminal event
                ctx.emit(RunEvent(type="run.failed", code=exc.code, error=str(exc)))
            return self._finalize_failure(run_id, str(exc), ctx)

        # 1b. Refuse a scenario whose steps call SDK features this wheel
        # lacks, before spawning anything (#117: from aitp-sdk 0.24 renewal
        # and session bundles are opt-in build features). Without this the
        # run spawned every agent and died on a 500 from the agent route.
        missing = missing_sdk_features(scenario, self.capabilities_provider)
        if missing:
            refusal = SdkFeatureUnavailableError(
                scenario_ref, missing, self.capabilities_provider().get("version"),
            )
            logger.warning("Run %s refused: %s", run_id, refusal)
            if not self._is_cancelled(run_id):
                ctx.emit(RunEvent(type="run.failed", code=refusal.code, error=str(refusal)))
            return self._finalize_failure(run_id, str(refusal), ctx)

        # A cancel since the start already emitted run.cancelled; a
        # run.started after it would be counted active and never finish.
        if self._is_cancelled(run_id):
            return self._finalize_failure(run_id, "cancelled before start", ctx)
        ctx.emit(RunEvent(
            type="run.started",
            scenario_ref=scenario_ref,
            template=template,
        ))

        # 2. Validate inputs against scenario schema
        try:
            schema = scenario.spec.inputs.schema_
            if schema:
                jsonschema.validate(inputs, schema)
        except jsonschema.ValidationError as exc:
            ctx.emit(RunEvent(
                type="run.failed", code="inputs_invalid",
                error=f"inputs validation: {exc.message}",
            ))
            return self._finalize_failure(run_id, f"inputs validation: {exc.message}", ctx)

        # 3-4. Everything from here on can raise and holds resources (ports,
        # bootstrap files, subprocesses), so it all sits inside one try: a
        # failure always emits a terminal run.failed and releases what was
        # taken. Ports are allocated one by one into the dict so a failure
        # part-way still releases the ones already taken.
        ports: dict[str, int] = {}
        bootstrap_files: dict[str, str] = {}
        running: dict[str, RunningAgent] = {}
        try:
            # 3. Assign ports + resolve manifests + write bootstrap
            for a in scenario.spec.agents:
                ports[a.id] = self.ports.allocate(offset=a.port_offset)
            resolved_manifests = {
                a.id: self.registry.get_agent_manifest(a.ref) for a in scenario.spec.agents
            }

            # If any agent uses OIDC identity, mint a per-run mock issuer
            # (Ed25519 keypair + JWK) so every OIDC agent shares the same
            # trust anchor. Pinned-key-only runs leave run_oidc=None.
            run_oidc = None
            if any(
                resolved_manifests[a.id].spec.aitp.identity_type == "oidc"
                for a in scenario.spec.agents
            ):
                from ..trust.oidc_issuer import RunOidcIssuer
                run_oidc = RunOidcIssuer.generate()
                ctx.emit(RunEvent(
                    type="oidc.issuer_minted",
                    result={"issuer_url": run_oidc.issuer_url, "kid": run_oidc.kid},
                ))

            admin_tokens: dict[str, str] = {}
            # RUN_TOKEN_BUDGET is per run; each agent process enforces its share.
            agent_token_budget = self.config.run_token_budget // max(1, len(scenario.spec.agents))
            for agent_spec in scenario.spec.agents:
                placeholder_peers = {
                    other.id: {
                        "manifest_url": f"{self.loopback.origin(ports[other.id])}/.well-known/aitp-manifest",
                        "did": None,
                    }
                    for other in scenario.spec.agents if other.id != agent_spec.id
                }
                bs = self.bootstrap_builder.build(
                    run_id=run_id,
                    agent_spec=agent_spec,
                    resolved_manifest=resolved_manifests[agent_spec.id],
                    port=ports[agent_spec.id],
                    peers=placeholder_peers,
                    inputs=inputs,
                    oidc=run_oidc,
                    token_budget=agent_token_budget,
                )
                bootstrap_files[agent_spec.id] = self.bootstrap_builder.write(bs)
                admin_tokens[agent_spec.id] = bs["admin_token"]

            # 4. Spawn all agents. A cancel that lands before an agent exists
            # finds nothing to kill, so check before each launch (#118).
            for agent_spec in scenario.spec.agents:
                if self._is_cancelled(run_id):
                    raise _RunCancelled(run_id)
                manifest = resolved_manifests[agent_spec.id]
                adapter = self.adapters.get(manifest.metadata.framework)
                validation = adapter.validate(manifest)
                if not validation.valid:
                    raise PlaygroundError(
                        f"manifest invalid for {agent_spec.id}: {validation.errors}"
                    )
                prepared = adapter.prepare_launch(
                    manifest, bootstrap_files[agent_spec.id], ports[agent_spec.id], self.config
                )
                ctx.emit(RunEvent(type="agent.spawning", agent_id=agent_spec.id, port=ports[agent_spec.id]))
                ra = await self.supervisor.launch(
                    run_id=run_id,
                    agent_id=agent_spec.id,
                    prepared=prepared,
                    port=ports[agent_spec.id],
                    admin_token=admin_tokens[agent_spec.id],
                    startup_timeout_ms=int(manifest.spec.host.get("startupTimeoutMs", 30_000)),
                )
                running[agent_spec.id] = ra
                ctx.emit(RunEvent(type="agent.ready", agent_id=agent_spec.id, aid=ra.aid, port=ra.port))

            # 4b. Provision each agent's responder trust store with the AIDs
            # of the agents this run launched (aitp-sdk >= 0.14 rejects every
            # pinned_key peer that is not in it).
            await self._push_trusted_peers(running, ctx)

            # 5. Resolve peer manifest URLs
            peers = await self.trust.resolve_peers(scenario, running)
            ctx.emit(RunEvent(
                type="trust.peers_resolved",
                peers={k: v.get("manifest_url") for k, v in peers.items()},
            ))

            # 6. Eager pairwise AITP handshakes — only when scenario opts in
            # (default). Scenarios that demonstrate trust gating set
            # `trust.eager: false` so they can control timing themselves.
            if scenario.spec.trust.eager:
                await self._establish_pairwise_trust(scenario, running, peers, ctx)

            # 7. Workflow steps
            step_outputs: dict[str, Any] = {}
            for step in scenario.spec.workflow.steps:
                await self._dispatch_step(
                    step, scenario, running, peers, resolved_manifests,
                    inputs, step_outputs, ctx,
                )

        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, _RunCancelled):
                logger.info("Run %s cancelled before all agents spawned", run_id)
            else:
                logger.exception("Run %s failed: %s", run_id, exc)
            # This is the one failure site that can race `/runs/{id}/cancel`:
            # steps 1-2 above run before any agent exists, so nothing there
            # can be mid-flight when a cancel lands, but a step dispatch can.
            # A cancel kills the subprocesses, which is what turns the next
            # inter-agent call into the exception landing here — so without
            # this check, a cancelled run's OWN kill produces a run.failed
            # event for a run the caller already saw as cancelled. Guarding
            # only the emit (not just the store, in `_finalize_failure`)
            # matters: `observability/metrics.py` treats run.complete,
            # run.failed and run.cancelled identically — decrementing
            # runs_active and incrementing runs_total once each — so an
            # unguarded emit here double-counts one run into two labels.
            # See `DECISIONS.md` D-16.
            existing = self.store.get(run_id)
            if existing is None or existing.get("status") != "cancelled":
                # Only a PlaygroundError's code is a run code: other
                # exceptions may carry an unrelated (even non-str) `code`
                # that would fail RunEvent validation and strand the run.
                ctx.emit(RunEvent(
                    type="run.failed",
                    code=exc.code if isinstance(exc, PlaygroundError) else None,
                    error=str(exc),
                ))
            self._cleanup_run(run_id, ports, bootstrap_files)
            return self._finalize_failure(run_id, str(exc), ctx)
        finally:
            self._cleanup_run(run_id, ports, bootstrap_files)

        # The steps finished, but a cancel may have landed after the last of
        # them: the caller already saw `cancelled`, so don't overwrite it.
        # Events are already in the store via RunContext.emit →
        # store.append_event; don't pass them here or the merge doubles them.
        completed = self.store.upsert_unless_cancelled(run_id, {
            "run_id": run_id, "status": "success", "scenario_ref": scenario_ref,
            "outputs": step_outputs,
            "error": None,
        })
        if completed:
            ctx.emit(RunEvent(type="run.complete"))
        # Ingest the FULL run event log, not just ctx.events. Agent
        # subprocesses deliver their canonical events (handshake.complete,
        # delegation.issued/redeemed, tct.*) to POST /internal/telemetry,
        # which lands them in the store but NOT in ctx.events. The CP
        # projects sessions / TCTs / delegations off exactly those agent
        # events, so ingesting ctx.events alone left every CP projection
        # empty. The store record is a superset of ctx.events (RunContext.emit
        # mirrors each orchestrator event into the store as well).
        record = self.store.get(run_id)
        events_to_ingest = record.get("events") if record else None
        task = asyncio.create_task(
            self.cp.ingest_events(events_to_ingest or ctx.events)
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        self._schedule_webhook_teardown(run_id, after=task)

        if not completed:
            return RunResult(run_id=run_id, status="cancelled", outputs=step_outputs,
                             events=ctx.events)
        return RunResult.success(run_id, step_outputs, ctx.events)

    def _agent_client(self, timeout: float) -> httpx.AsyncClient:
        """A client for this run's agents: their admin token and loopback TLS."""
        return httpx.AsyncClient(
            timeout=timeout, auth=self.supervisor.admin_auth, mounts=self.loopback.mounts(),
        )

    def _cleanup_run(
        self,
        run_id: str,
        ports: dict[str, int],
        bootstrap_files: dict[str, str],
    ) -> None:
        """Idempotent post-run cleanup: kill subprocesses, release ports,
        delete bootstrap temp files. Called in both success and failure paths;
        each operation is wrapped to never raise out of the cleanup itself."""
        try:
            self.supervisor.kill_run(run_id)
        except Exception:  # noqa: BLE001
            logger.exception("kill_run failed for %s", run_id)
        for port in ports.values():
            try:
                self.ports.release(port)
            except Exception:  # noqa: BLE001
                logger.exception("port release failed for %s", port)
        for path in bootstrap_files.values():
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                logger.warning("could not remove bootstrap file %s", path)

    def _schedule_webhook_teardown(
        self, run_id: str, *, after: Optional[asyncio.Task[Any]] = None,
    ) -> None:
        """Delete the run's CP webhook subscription once the run has ended,
        whether it completed, failed or was cancelled (a cancel lands in
        `_finalize_failure`). It waits for `after` (the end-of-run ingest) and
        then `cp_webhook_teardown_grace_secs`, so the CP can still fan out and
        retry the events of the run itself. A run that never subscribed is a
        no-op; a failed delete is left to the startup sweep in `main.py`."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # called outside a run (unit tests): nothing to tear down
            return
        task = loop.create_task(self._teardown_webhook(run_id, after))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _teardown_webhook(
        self, run_id: str, after: Optional[asyncio.Task[Any]],
    ) -> None:
        if after is not None:
            await asyncio.gather(after, return_exceptions=True)
        await asyncio.sleep(self.config.cp_webhook_teardown_grace_secs)
        record = self.store.get(run_id) or {}
        webhook_id = (record.get("cp_webhook") or {}).get("id")
        if webhook_id and await self.cp.delete_webhook(webhook_id):
            logger.info("deleted CP webhook %s of run %s", webhook_id, run_id)

    def _finalize_failure(self, run_id: str, error: str, ctx: RunContext) -> RunResult:
        """The run FAILED — but if it was already cancelled, the record must
        say so, not "failed". `api/runs.py`'s `/cancel` route upserts
        `status="cancelled"` and kills the agent subprocesses; that kill is
        what turns the in-flight run's next inter-agent call into the
        exception that lands here. Without this guard that upsert clobbers
        the cancellation the caller explicitly asked for, one HTTP round-trip
        later. See `DECISIONS.md` D-16.
        """
        self._schedule_webhook_teardown(run_id)
        if self._is_cancelled(run_id):
            return RunResult(run_id=run_id, status="cancelled", events=ctx.events, error=error)
        self.store.upsert(run_id, {
            "run_id": run_id, "status": "failed",
            "outputs": {},
            "error": error,
        })
        return RunResult.failure(run_id, error, events=ctx.events)

    def _is_cancelled(self, run_id: str) -> bool:
        existing = self.store.get(run_id)
        return existing is not None and existing.get("status") == "cancelled"

    # ---- step dispatch ----

    async def _dispatch_step(
        self,
        step: WorkflowStep,
        scenario: ScenarioVersion,
        running: dict[str, RunningAgent],
        peers: dict[str, dict[str, Any]],
        resolved_manifests: dict[str, Any],
        inputs: dict[str, Any],
        step_outputs: dict[str, Any],
        ctx: RunContext,
    ) -> None:
        # Infer step type for backward-compatible scenario YAMLs that omit `type`.
        step_type = step.type or (
            "workflow" if (step.agent and step.capability) else "meta"
        )

        if step_type == "meta":
            ctx.emit(RunEvent(type="step.skipped", step_id=step.id, notes=step.description))
            return

        # Fault injection short-circuit. When a step declares a fault we
        # divert into a single helper that mutates the target URL/port and
        # records the resulting failure as a structured outcome. The run
        # itself never raises out of an injected step — the whole point is
        # to demonstrate the failure path live and let subsequent steps
        # probe the consequences.
        if step.fault is not None:
            await self._dispatch_fault_injected(
                step, step_type, scenario, running, peers, ctx, step_outputs,
            )
            return

        if step_type == "capability_call_no_trust":
            if not step.agent or not step.target_agent or not step.capability:
                raise PlaygroundError(
                    f"step {step.id}: capability_call_no_trust requires agent, target_agent, capability"
                )
            result = await self._call_without_trust(
                running[step.agent], running[step.target_agent], step.capability,
                step.expect_status, ctx,
            )
            step_outputs[step.id] = result
            ctx.emit(RunEvent(type="step.complete", step_id=step.id, result=result))
            return

        if step_type == "capability_probe":
            # Uses /admin/invoke (presents the caller's held TCT) and inspects
            # the inner status code. Used by scoped-capabilities to observe
            # that a TCT only covers the grants it was issued for.
            if not step.agent or not step.target_agent or not step.capability:
                raise PlaygroundError(
                    f"step {step.id}: capability_probe requires agent, target_agent, capability"
                )
            payload = self._resolve_step_input(step, inputs, step_outputs)
            result = await self._probe_with_held_tct(
                running[step.agent], running[step.target_agent], step.capability,
                payload, step.expect_status, ctx,
            )
            step_outputs[step.id] = result
            ctx.emit(RunEvent(type="step.complete", step_id=step.id, result=result))
            return

        if step_type == "handshake":
            if not step.initiator or not step.responder:
                raise PlaygroundError(
                    f"step {step.id}: handshake requires initiator and responder"
                )
            a = running[step.initiator]
            b = running[step.responder]
            # Only the explicit direction is established. Earlier versions also
            # auto-ran the reverse direction with no scope narrowing, which
            # silently overwrote a previously-scoped TCT when scenarios used
            # ``requested_grants`` (e.g. scoped-capabilities). Scenarios that
            # genuinely need both directions list two handshake steps.
            await self._ensure_trust(a, b, peers[step.responder], step.requested_grants, ctx)
            step_outputs[step.id] = {"trust": "established"}
            ctx.emit(RunEvent(type="step.complete", step_id=step.id))
            return

        if step_type == "delegate":
            if not step.delegator or not step.delegatee or not step.via_peer:
                raise PlaygroundError(
                    f"step {step.id}: delegate requires delegator, delegatee, via_peer"
                )
            delegator_ra = running[step.delegator]
            via_ra = running[step.via_peer]
            delegatee_manifest_url = peers[step.delegatee]["manifest_url"]
            scope = list(step.scope or [])
            if not scope:
                raise PlaygroundError(
                    f"step {step.id}: delegate requires non-empty scope"
                )
            ctx.emit(RunEvent(
                type="delegation.issuing",
                initiator=step.delegator, target=step.delegatee,
                grants=scope,
            ))
            async with self._agent_client(15.0) as client:
                r = await client.post(
                    f"{self.loopback.origin(delegator_ra.port)}/admin/delegate",
                    json={
                        "held_tct_peer_port": via_ra.port,
                        "delegatee_manifest_url": delegatee_manifest_url,
                        "scope": scope,
                        "ttl_secs": step.ttl_secs,
                    },
                )
                r.raise_for_status()
                data = r.json()
            # The redeemer verifies the token under each hop issuer's Manifest
            # (aitp-sdk >= 0.16). A delegator that holds its grant through an
            # earlier redemption carries that chain's issuers forward.
            data["issuer_manifest_urls"] = [
                *_held_delegation_chain(step_outputs, step.delegator, step.via_peer),
                delegator_ra.manifest_url,
            ]
            step_outputs[step.id] = data
            ctx.emit(RunEvent(
                type="step.complete", step_id=step.id,
                result={"delegatee_aid": data.get("delegatee_aid"), "scope": scope},
            ))
            return

        if step_type == "redeem_delegation":
            if not step.delegatee or not step.target or not step.via_delegation:
                raise PlaygroundError(
                    f"step {step.id}: redeem_delegation requires delegatee, target, via_delegation"
                )
            prior = step_outputs.get(step.via_delegation)
            if not isinstance(prior, dict) or "delegation_token" not in prior:
                raise PlaygroundError(
                    f"step {step.id}: via_delegation '{step.via_delegation}' did not produce a delegation_token"
                )
            delegatee_ra = running[step.delegatee]
            target_ra = running[step.target]
            # The peer's redeem URL is the manifest URL with the well-known path swapped.
            target_manifest_url = peers[step.target]["manifest_url"]
            redeem_url = target_manifest_url.replace(
                "/.well-known/aitp-manifest", "/aitp/delegation/redeem",
            )
            ctx.emit(RunEvent(
                type="delegation.redeeming",
                initiator=step.delegatee, target=step.target,
            ))
            async with self._agent_client(20.0) as client:
                r = await client.post(
                    f"{self.loopback.origin(delegatee_ra.port)}/admin/redeem-delegation",
                    json={
                        "redeem_url": redeem_url,
                        "delegation_token": prior["delegation_token"],
                        "peer_port": target_ra.port,
                        "issuer_manifest_urls": prior.get("issuer_manifest_urls", []),
                    },
                )
            if step.expect_status is not None:
                # Probe mode, like `capability_probe`: record the target's
                # answer rather than failing the run on a refusal. A 2xx
                # still stores the redeemed TCT (the admin route did that).
                observed = r.status_code
                try:
                    body: Any = r.json()
                except ValueError:
                    body = r.text
                probe = {
                    "status_code": observed,
                    "rejected": observed in (401, 403),
                    "expected_status": step.expect_status,
                    "matched": observed == step.expect_status,
                    "body": body,
                }
                step_outputs[step.id] = probe
                ctx.emit(RunEvent(type="step.complete", step_id=step.id, result=probe))
                return
            r.raise_for_status()
            data = r.json()
            data["delegation_chain"] = {
                "holder": step.delegatee,
                "issuer": step.target,
                "issuer_manifest_urls": list(prior.get("issuer_manifest_urls", [])),
            }
            step_outputs[step.id] = data
            ctx.emit(RunEvent(
                type="step.complete", step_id=step.id,
                result={"redeemed_from": step.target, "by": step.delegatee},
            ))
            return

        if step_type == "revoke_tct":
            if not step.issuer or not step.audience:
                raise PlaygroundError(
                    f"step {step.id}: revoke_tct requires issuer and audience"
                )
            jti = self._find_tct_jti(
                ctx.events, audience=step.audience, issuer=step.issuer,
            )
            if jti is None:
                raise PlaygroundError(
                    f"step {step.id}: no prior trust.established event for "
                    f"audience={step.audience} issuer={step.issuer} — handshake first",
                )
            local_revoke = step.local_revoke is not False
            if not local_revoke and not step.via_cp:
                raise PlaygroundError(
                    f"step {step.id}: local_revoke: false needs via_cp: true"
                )
            if not local_revoke and not self.cp.enabled:
                # CP-only revocation with no CP: there is nowhere to revoke.
                # Record that instead of pretending, so a later step's
                # expectation visibly fails to match.
                ctx.emit(RunEvent(
                    type="step.skipped",
                    step_id=step.id,
                    notes="CP not configured (CP_BASE_URL unset)",
                ))
                step_outputs[step.id] = {"revoked_jti": jti, "skipped": "no cp"}
                return
            if local_revoke:
                issuer_ra = running[step.issuer]
                async with self._agent_client(5.0) as client:
                    r = await client.post(
                        f"{self.loopback.origin(issuer_ra.port)}/admin/revoke-tct",
                        json={"jti": jti},
                    )
                    r.raise_for_status()

            published_to_cp = False
            issuer_refreshed = 0
            refreshed: dict[str, int] = {}
            if step.via_cp:
                # End-to-end revocation: record the jti at the CP under its
                # issuer, then have the issuer pull, re-sign and apply its own
                # list (RFC-AITP-0008 §1.5: a list is valid only from the
                # issuer of the tokens it names). The issuer is the agent that
                # verifies this TCT when the holder presents it, so its
                # refreshed list is what decides the outcome.
                published_to_cp = await self.cp.publish_revocation(
                    jti, reason=step.reason or f"step {step.id}",
                    iss=running[step.issuer].aid,
                )
                # The issuer re-signs its own list now rather than at its next
                # poll, so a verifier fetching it straight after sees the jti.
                async with self._agent_client(10.0) as client:
                    pr = await client.post(
                        f"{self.loopback.origin(running[step.issuer].port)}"
                        "/admin/publish-revocations",
                    )
                issuer_list = pr.json() if pr.is_success else {"published": False}
                ctx.emit(RunEvent(
                    type="revocation.published",
                    step_id=step.id, jti=jti,
                    result={"to_cp": published_to_cp, "reason": step.reason},
                ))
                for agent_id in step.refresh_agents or [step.issuer]:
                    async with self._agent_client(10.0) as client:
                        rr = await client.post(
                            f"{self.loopback.origin(running[agent_id].port)}"
                            "/admin/refresh-revocations",
                        )
                    # Graceful: if an agent can't reach CP, the step still
                    # records what happened — the test harness will see a
                    # zero count and can decide whether to fail.
                    refreshed[agent_id] = (
                        int(rr.json().get("revoked_count", 0)) if rr.is_success else 0
                    )
                issuer_refreshed = refreshed.get(step.issuer, 0)

            result_payload: dict[str, Any] = {"revoked_jti": jti}
            if step.via_cp:
                result_payload.update({
                    "published_to_cp": published_to_cp,
                    "issuer_revoked_count": issuer_refreshed,
                    "local_revoke": local_revoke,
                    "refreshed": refreshed,
                    "issuer_list": issuer_list,
                })
            step_outputs[step.id] = result_payload
            ctx.emit(RunEvent(
                type="step.complete", step_id=step.id, result=result_payload,
            ))
            return

        if step_type == "enroll_with_cp":
            if not step.agent:
                raise PlaygroundError(
                    f"step {step.id}: enroll_with_cp requires agent"
                )
            if not self.cp.enabled:
                ctx.emit(RunEvent(
                    type="step.skipped",
                    step_id=step.id,
                    notes="CP not configured (CP_BASE_URL unset)",
                ))
                step_outputs[step.id] = {"enrolled": False, "skipped": "no cp"}
                return
            ra = running[step.agent]
            ctx.emit(RunEvent(
                type="cp.enroll_started", step_id=step.id, agent_id=step.agent,
            ))
            async with self._agent_client(15.0) as client:
                r = await client.post(
                    f"{self.loopback.origin(ra.port)}/admin/enroll-with-cp",
                    json={},
                )
                r.raise_for_status()
                data = r.json()
            step_outputs[step.id] = data
            ctx.emit(RunEvent(
                type="cp.enroll_complete",
                step_id=step.id, agent_id=step.agent,
                aid=data.get("aid"),
                result={"registered_at": data.get("registered_at")},
            ))
            ctx.emit(RunEvent(type="step.complete", step_id=step.id, result=data))
            return

        if step_type == "cp_subscribe_webhook":
            if not self.cp.enabled:
                ctx.emit(RunEvent(
                    type="step.skipped",
                    step_id=step.id,
                    notes="CP not configured (CP_BASE_URL unset)",
                ))
                step_outputs[step.id] = {"subscribed": False, "skipped": "no cp"}
                return
            base = self.config.playground_base_url.rstrip("/")
            url = f"{base}/webhooks/cp/{ctx.run_id}"
            created = await self.cp.create_webhook(
                url=url,
                events=list(step.events or []),
            )
            if not created:
                ctx.emit(RunEvent(
                    type="cp.webhook.subscribe_failed", step_id=step.id,
                    notes="CP refused or unreachable",
                ))
                step_outputs[step.id] = {"subscribed": False}
                return
            if created.get("id"):
                self.webhook_ids.add(created["id"])
            self.store.upsert(ctx.run_id, {
                "cp_webhook": {
                    "id": created.get("id"),
                    "secret": created.get("secret"),
                    "url": url,
                    "events": list(step.events or []),
                },
            })
            ctx.emit(RunEvent(
                type="cp.webhook.subscribed", step_id=step.id,
                result={
                    "id": created.get("id"),
                    "url": url,
                    "events": list(step.events or []),
                },
            ))
            step_outputs[step.id] = {
                "subscribed": True,
                "webhook_id": created.get("id"),
                "url": url,
                "events": list(step.events or []),
            }
            return

        if step_type == "cp_provision_trust_anchor":
            # Push trust configuration to the Control Plane: register the
            # agent's pinned Ed25519 key (from its published manifest) and,
            # when ``issuer`` is set, an OIDC trust anchor. Then read both
            # registries back to confirm the registration landed.
            if not step.agent:
                raise PlaygroundError(
                    f"step {step.id}: cp_provision_trust_anchor requires agent"
                )
            if not self.cp.enabled:
                ctx.emit(RunEvent(
                    type="step.skipped", step_id=step.id,
                    notes="CP not configured (CP_BASE_URL unset)",
                ))
                step_outputs[step.id] = {"provisioned": False, "skipped": "no cp"}
                return
            ra = running[step.agent]
            namespace = step.namespace or scenario.metadata.pack
            async with self._agent_client(10.0) as client:
                mr = await client.get(ra.manifest_url)
                mr.raise_for_status()
                # Verify before reading key material out of the envelope. This
                # value is pinned into the control plane's trust store, so an
                # unverified read here would let whatever answered at
                # manifest_url choose the key the CP trusts for this agent.
                # The runner launched this agent and knows its AID, so unlike
                # the peer-manifest sites we can check identity too, not just
                # authenticity.
                try:
                    aitp.verify_manifest_json(
                        mr.text,
                        insecure_localhost_dangerous=self.config.insecure_localhost_dangerous,
                    )
                except Exception as exc:  # noqa: BLE001 — SDK raises RuntimeError/ValueError
                    cause = _classify_manifest_verify_failure(exc)
                    ctx.emit(RunEvent(
                        type="manifest.verify_failed", step_id=step.id,
                        agent_id=step.agent, cause=cause,
                        source_url=ra.manifest_url,
                    ))
                    raise PlaygroundError(
                        f"manifest at {ra.manifest_url} failed verification "
                        f"({cause}): {exc} — refusing to pin a trust anchor "
                        "from an unverified manifest"
                    ) from exc
                manifest = mr.json().get("manifest", {})
            # Authenticity is not enough here. Verification proves the envelope
            # was minted by the holder of the AID it declares — but this step
            # pins the key it finds into the control plane's trust store
            # *under `ra.aid`*, so without an identity check whatever answers
            # at this port can serve its own genuinely-signed manifest and have
            # its key trusted as this agent's.
            #
            # "The runner launched it" does not close that: the launch and this
            # fetch are separate channels. If the agent dies between reporting
            # ready and being provisioned, or anything else takes the port, the
            # substitution succeeds and nothing downstream can tell.
            #
            # `ra.aid` is the right thing to compare against: the supervisor
            # reads it from the spawned process's own AITP_AGENT_READY line
            # over a parent-child pipe, not off the network.
            declared_aid = manifest.get("aid")
            if declared_aid != ra.aid:
                raise PlaygroundError(
                    f"manifest at {ra.manifest_url} declares aid "
                    f"{declared_aid!r}, but the runner launched {ra.aid!r} — "
                    "refusing to pin a trust anchor for an agent that is not "
                    "the one answering at this port"
                )
            pubkey = (manifest.get("identity_hint") or {}).get("public_key")
            pinned = None
            if pubkey:
                pinned = await self.cp.upsert_pinned_key(
                    aid=ra.aid, pubkey=pubkey, namespace=namespace, label=step.agent,
                )
            anchor = None
            if step.issuer_url:
                anchor = await self.cp.upsert_trust_anchor(
                    issuer_url=step.issuer_url, namespace=namespace, label=step.agent,
                )
            pinned_keys = await self.cp.list_pinned_keys(namespace=namespace)
            anchors = await self.cp.list_trust_anchors(namespace=namespace)
            result = {
                "provisioned": True,
                "namespace": namespace,
                "pinned_key": pinned,
                "trust_anchor": anchor,
                "pinned_key_count": len(pinned_keys),
                "trust_anchor_count": len(anchors),
            }
            step_outputs[step.id] = result
            ctx.emit(RunEvent(
                type="cp.trust_anchor.provisioned",
                step_id=step.id, agent_id=step.agent, result=result,
            ))
            ctx.emit(RunEvent(type="step.complete", step_id=step.id, result=result))
            return

        if step_type == "cp_delegation_tree":
            # Walk a delegator's delegation chain as the CP observed it
            # (projected from delegation.issued / delegation.redeemed events).
            if not step.agent:
                raise PlaygroundError(
                    f"step {step.id}: cp_delegation_tree requires agent"
                )
            if not self.cp.enabled:
                ctx.emit(RunEvent(
                    type="step.skipped", step_id=step.id,
                    notes="CP not configured (CP_BASE_URL unset)",
                ))
                step_outputs[step.id] = {"delegations": [], "skipped": "no cp"}
                return
            ra = running[step.agent]
            # Flush this run's observed events to the CP and await the ingest
            # before reading its projection. Agent subprocesses deliver their
            # canonical delegation events (delegation.issued / .redeemed) to
            # POST /internal/telemetry, which lands them in the store but not in
            # the CP; the batch CP ingest otherwise only runs after
            # run.complete. Without this flush the query races projection and
            # always returns an empty tree. The CP's POST /api/events awaits its
            # projection before responding, so once this returns the delegation
            # rows are queryable. CP projections are idempotent
            # (onConflictDoNothing), so the post-run batch re-sending these
            # events creates no duplicate rows.
            record = self.store.get(ctx.run_id)
            events_so_far = record.get("events") if record else list(ctx.events)
            await self.cp.ingest_events(events_so_far)
            chain = await self.cp.fetch_delegations(delegator=ra.aid)
            result = {
                "delegator": ra.aid,
                "delegations": chain,
                "count": len(chain),
            }
            step_outputs[step.id] = result
            ctx.emit(RunEvent(
                type="cp.delegation.tree",
                step_id=step.id, agent_id=step.agent, result=result,
            ))
            ctx.emit(RunEvent(type="step.complete", step_id=step.id, result=result))
            return

        if step_type == "renew_tct":
            # RFC-AITP-0013: holder presents the held TCT to the
            # issuer, who mints a fresh envelope with a new jti +
            # expiry. The held TCT is swapped in-place on the holder's
            # /admin side.
            if not step.agent or not step.via_peer:
                raise PlaygroundError(
                    f"step {step.id}: renew_tct requires agent (holder) and via_peer (issuer)"
                )
            holder_ra = running[step.agent]
            issuer_ra = running[step.via_peer]
            async with self._agent_client(15.0) as client:
                r = await client.post(
                    f"{self.loopback.origin(holder_ra.port)}/admin/renew-tct",
                    json={"peer_port": issuer_ra.port},
                )
                r.raise_for_status()
                data = r.json()
            step_outputs[step.id] = data
            ctx.emit(RunEvent(
                type="tct.renewed",
                step_id=step.id,
                agent_id=step.agent,
                target=step.via_peer,
                jti=data.get("jti"),
                result={"expires_at": data.get("expires_at")},
            ))
            ctx.emit(RunEvent(
                type="step.complete", step_id=step.id, result=data,
            ))
            return

        if step_type == "tct_cache_stats":
            # Read an agent's RFC-AITP-0005 verification-cache counters. The
            # tct-cache-perf scenario invokes a capability repeatedly, then
            # reads stats here to show repeat presentations hitting the cache.
            if not step.agent:
                raise PlaygroundError(
                    f"step {step.id}: tct_cache_stats requires agent"
                )
            target_ra = running[step.agent]
            async with self._agent_client(5.0) as client:
                r = await client.get(
                    f"{self.loopback.origin(target_ra.port)}/admin/tct-cache-stats"
                )
                r.raise_for_status()
                data = r.json()
            step_outputs[step.id] = data
            ctx.emit(RunEvent(
                type="tct.cache.stats",
                step_id=step.id,
                agent_id=step.agent,
                result=data,
            ))
            ctx.emit(RunEvent(
                type="step.complete", step_id=step.id, result=data,
            ))
            return

        if step_type == "export_session_bundle":
            if not step.coordinator or not step.participants:
                raise PlaygroundError(
                    f"step {step.id}: export_session_bundle requires "
                    f"coordinator and participants"
                )
            coord_ra = running[step.coordinator]
            # RFC-AITP-0010 bundles package the coordinator-issued TCTs. Under
            # aitp v0.2 the coordinator (responder) never receives its own
            # issued token back from the SDK — each participant holds it. So we
            # collect each participant's held (coordinator-issued) token and
            # hand them to the coordinator to sign into the bundle.
            async with self._agent_client(15.0) as client:
                participant_tcts: list[dict[str, Any]] = []
                for p_id in step.participants:
                    p_ra = running[p_id]
                    held = await client.get(
                        f"{self.loopback.origin(p_ra.port)}/admin/held-tct",
                        params={"peer_port": coord_ra.port},
                    )
                    held.raise_for_status()
                    h = held.json()
                    participant_tcts.append(
                        {"aid": h["aid"], "tct_token": h["tct_token"]}
                    )
                r = await client.post(
                    f"{self.loopback.origin(coord_ra.port)}/admin/export-session-bundle",
                    json={"participant_tcts": participant_tcts},
                )
                r.raise_for_status()
                data = r.json()
            # The verifier needs the coordinator's Manifest (aitp-sdk >= 0.16,
            # RFC-AITP-0010 §5 step 5); carry where to fetch it with the bundle.
            data["coordinator_manifest_url"] = coord_ra.manifest_url
            step_outputs[step.id] = data
            ctx.emit(RunEvent(
                type="session.bundle.exported",
                step_id=step.id,
                agent_id=step.coordinator,
                result={
                    "participant_aids": data.get("participant_aids", []),
                    "session_id": data.get("session_id"),
                },
            ))
            ctx.emit(RunEvent(
                type="step.complete", step_id=step.id, result=data,
            ))
            return

        if step_type == "verify_session_bundle":
            if not step.verifier or not step.via_step:
                raise PlaygroundError(
                    f"step {step.id}: verify_session_bundle requires "
                    f"verifier and via_step"
                )
            prior = step_outputs.get(step.via_step) or {}
            bundle_envelope = prior.get("bundle_envelope")
            if not bundle_envelope:
                raise PlaygroundError(
                    f"step {step.id}: via_step {step.via_step!r} did not "
                    f"produce a bundle_envelope"
                )
            verifier_ra = running[step.verifier]
            async with self._agent_client(15.0) as client:
                r = await client.post(
                    f"{self.loopback.origin(verifier_ra.port)}/admin/verify-session-bundle",
                    json={
                        "bundle_envelope": bundle_envelope,
                        "coordinator_manifest_url": prior.get("coordinator_manifest_url"),
                    },
                )
                r.raise_for_status()
                data = r.json()
            step_outputs[step.id] = data
            ctx.emit(RunEvent(
                type="session.bundle.verified",
                step_id=step.id,
                agent_id=step.verifier,
                result={
                    "kind": data.get("kind"),
                    "active_aids": data.get("active_aids", []),
                    "dropped_aids": data.get("dropped_aids", []),
                },
            ))
            ctx.emit(RunEvent(
                type="step.complete", step_id=step.id, result=data,
            ))
            return

        if step_type == "spki_pin_check":
            # Pure-SDK exercise: compute_spki_hash + SpkiPinVerifier.
            # No agent involved — the playground runs the SDK directly so
            # the demo doesn't have to stand up TLS infrastructure.
            import base64

            if not step.cert_der_b64 or step.pins is None:
                raise PlaygroundError(
                    f"step {step.id}: spki_pin_check requires cert_der_b64 and pins"
                )
            cert_der = base64.b64decode(step.cert_der_b64)
            pin_bytes = [base64.b64decode(p) for p in step.pins]
            computed_hash = aitp.compute_spki_hash(cert_der)
            verifier = aitp.SpkiPinVerifier(pin_bytes)
            is_pinned = verifier.is_pinned(cert_der)
            expected = step.expect_status  # repurposed: 1 = expect pinned, 0 = expect not-pinned
            result = {
                "computed_hash_b64": base64.b64encode(bytes(computed_hash)).decode(),
                "pin_count": verifier.len,
                "is_pinned": is_pinned,
            }
            step_outputs[step.id] = result
            ctx.emit(RunEvent(
                type="spki.pin.checked",
                step_id=step.id,
                result=result,
            ))
            if expected is not None and bool(expected) != is_pinned:
                ctx.emit(RunEvent(
                    type="step.unexpected_status",
                    step_id=step.id,
                    result={"expected_pinned": bool(expected), "actual": is_pinned},
                ))
                raise PlaygroundError(
                    f"step {step.id}: spki_pin_check expected_pinned="
                    f"{bool(expected)} but got {is_pinned}"
                )
            ctx.emit(RunEvent(
                type="step.complete", step_id=step.id, result=result,
            ))
            return

        if step_type == "rotate_keys":
            if not step.agent:
                raise PlaygroundError(
                    f"step {step.id}: rotate_keys requires agent"
                )
            ra = running[step.agent]
            old_aid = ra.aid
            async with self._agent_client(10.0) as client:
                r = await client.post(
                    f"{self.loopback.origin(ra.port)}/admin/rotate-keys",
                    json={},
                )
                r.raise_for_status()
                data = r.json()
            new_aid = data.get("aid") or ""
            ra.aid = new_aid
            # The rotated agent is a new pinned_key identity; its peers must
            # be told before it can handshake with them again.
            await self._push_trusted_peers(running, ctx)
            step_outputs[step.id] = {
                "old_aid": old_aid, "new_aid": new_aid,
            }
            ctx.emit(RunEvent(
                type="identity.key.rotated",
                step_id=step.id, agent_id=step.agent,
                aid=new_aid, notes=f"old_aid={old_aid}",
            ))
            ctx.emit(RunEvent(
                type="step.complete", step_id=step.id,
                result={"old_aid": old_aid, "new_aid": new_aid},
            ))
            return

        # Default workflow step — caller is step.agent, target is the capability holder.
        if not step.agent or not step.capability:
            raise PlaygroundError(
                f"step {step.id}: workflow step requires agent and capability"
            )
        caller_ra = running[step.agent]
        target_agent_id = self._find_capability_holder(
            step.capability, scenario, resolved_manifests, prefer=step.agent,
        )
        if target_agent_id is None:
            raise PlaygroundError(
                f"No agent in scenario offers capability {step.capability}"
            )

        step_input = self._resolve_step_input(step, inputs, step_outputs)
        ctx.emit(RunEvent(
            type="step.started",
            step_id=step.id, agent=step.agent, capability=step.capability,
        ))

        if target_agent_id == step.agent:
            result = await self._self_execute(caller_ra, step.capability, step_input)
        else:
            target_ra = running[target_agent_id]
            result = await self._invoke_capability(
                caller_ra, target_ra, step.capability, step_input, ctx,
            )
        step_outputs[step.id] = result
        ctx.emit(RunEvent(type="step.complete", step_id=step.id, result=result))

    async def _dispatch_fault_injected(
        self,
        step: WorkflowStep,
        step_type: str,
        scenario: ScenarioVersion,
        running: dict[str, RunningAgent],
        peers: dict[str, dict[str, Any]],
        ctx: RunContext,
        step_outputs: dict[str, Any],
    ) -> None:
        """Apply a step.fault transformation and record the outcome.

        Currently supports two pure-engine fault kinds:

          - ``manifest_404``: rewrite the targeted peer's manifest URL
            to a path that doesn't exist. Useful for handshake steps —
            the initiator's manifest GET 404s before the SDK handshake
            state machine ever runs.
          - ``peer_offline``: rewrite the targeted peer's host:port to
            an unbound port so the TCP connect fails. Useful for
            handshake and workflow steps to demonstrate connection-
            level failures.

        The step output is always
        ``{fault_injected: True, kind, target, error}`` and the run
        continues; downstream steps can branch on the result.
        """
        assert step.fault is not None  # narrowed for type-checker
        fault = step.fault

        # Resolve which agent the fault targets. For handshake steps this
        # is the responder; for workflow / capability_probe it's the
        # capability provider. For delegate it's the via_peer (whose
        # manifest the delegator fetches to bind the delegation).
        target_agent_id: Optional[str] = None
        if step_type == "handshake":
            target_agent_id = step.responder
        elif step_type in ("workflow", "capability_probe"):
            target_agent_id = step.target_agent or self._find_capability_holder(
                step.capability or "", scenario, {a.id: None for a in scenario.spec.agents},  # type: ignore[arg-type]
                prefer=step.agent,
            )
            # The lookup above passes Nones so the manifest-driven branch
            # can't fire — fall back to step.agent for self-execute case.
            if target_agent_id is None:
                target_agent_id = step.agent
        elif step_type == "delegate":
            target_agent_id = step.via_peer
        elif step_type == "redeem_delegation":
            target_agent_id = step.target

        if target_agent_id is None or target_agent_id not in peers:
            raise PlaygroundError(
                f"step {step.id}: fault {fault.kind!r} could not resolve target peer "
                f"(step_type={step_type})"
            )

        original_url: str = peers[target_agent_id]["manifest_url"]
        original_port: int = running[target_agent_id].port
        mutated_url, mutated_port = original_url, original_port

        if fault.kind == "manifest_404":
            mutated_url = original_url + "-DOES-NOT-EXIST"
        elif fault.kind == "peer_offline":
            # Use a port that's almost certainly closed. The supervisor
            # never allocates 1; ECONNREFUSED is the expected outcome.
            mutated_port = 1
            mutated_url = original_url.replace(
                f":{original_port}/", f":{mutated_port}/", 1,
            )
        else:  # noqa: E0801 — exhaustive guard for future kinds
            raise PlaygroundError(
                f"step {step.id}: unknown fault kind {fault.kind!r}"
            )

        ctx.emit(RunEvent(
            type="step.fault_injected",
            step_id=step.id,
            target=target_agent_id,
            notes=f"kind={fault.kind} note={fault.note or ''}",
        ))

        # Now run the step's natural action with the mutated peer URL/port,
        # catching whatever the failure surface looks like.
        error_msg: Optional[str] = None
        try:
            if step_type == "handshake":
                # Build a synthetic peer_info dict that the trust path
                # would normally hand us. _ensure_trust only reads
                # manifest_url, so this is enough.
                await self._ensure_trust(
                    running[step.initiator],  # type: ignore[index]
                    running[step.responder],  # type: ignore[index]
                    {"manifest_url": mutated_url},
                    step.requested_grants,
                    ctx,
                )
            elif step_type in ("workflow", "capability_probe"):
                # Synthesize a RunningAgent for the mutated peer so the
                # caller's /admin/invoke posts to the unbound port. The
                # caller's held_tcts is keyed on the *original* peer's
                # port, so /admin/invoke will return 412 "no TCT" when
                # the port changes — peer_offline still demonstrates a
                # transport failure, just one observed at the caller's
                # /admin/invoke rather than at the wire.
                from copy import copy
                fake_target = copy(running[target_agent_id])
                fake_target.port = mutated_port
                # Use the probe path so a 4xx is recorded, not raised.
                payload = self._resolve_step_input(
                    step, ctx.events and {} or {}, step_outputs,
                )
                await self._probe_with_held_tct(
                    running[step.agent or target_agent_id],
                    fake_target,
                    step.capability or "",
                    payload, step.expect_status, ctx,
                )
            else:
                raise PlaygroundError(
                    f"step {step.id}: fault on step_type={step_type!r} is not supported"
                )
        except Exception as exc:  # noqa: BLE001
            error_msg = f"{type(exc).__name__}: {exc}"

        outcome = {
            "fault_injected": True,
            "kind": fault.kind,
            "target": target_agent_id,
            "mutated_url": mutated_url,
            "error": error_msg,
        }
        step_outputs[step.id] = outcome
        ctx.emit(RunEvent(
            type="step.fault_complete",
            step_id=step.id,
            target=target_agent_id,
            result=outcome,
        ))

    async def _call_without_trust(
        self,
        caller: RunningAgent,
        target: RunningAgent,
        capability: str,
        expect_status: Optional[int],
        ctx: RunContext,
    ) -> dict[str, Any]:
        """Call a capability endpoint directly with no X-AITP-TCT header. Used
        by trust-gate scenarios to observe the rejection."""
        ctx.emit(RunEvent(
            type="step.probing_no_trust",
            initiator=caller.agent_id, target=target.agent_id,
            capability=capability,
        ))
        async with self._agent_client(10.0) as client:
            r = await client.post(
                f"{self.loopback.origin(target.port)}/capabilities/{capability}",
                content="{}",
                headers={"Content-Type": "application/json"},
            )
        observed = r.status_code
        rejected = observed in (401, 403)
        matched = (expect_status is None) or (observed == expect_status)
        event_type = "step.access_denied" if (rejected and matched) else "step.unexpected_status"
        ctx.emit(RunEvent(
            type=event_type,
            target=target.agent_id, capability=capability,
            result={"status_code": observed},
        ))
        body: Any
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = r.text
        return {
            "status_code": observed,
            "rejected": rejected,
            "expected_status": expect_status,
            "matched": matched,
            "body": body,
        }

    async def _probe_with_held_tct(
        self,
        caller: RunningAgent,
        target: RunningAgent,
        capability: str,
        payload: Any,
        expect_status: Optional[int],
        ctx: RunContext,
    ) -> dict[str, Any]:
        """Invoke ``capability`` on ``target`` via the caller's /admin/invoke
        (held TCT presented), then report the inner status code without
        crashing the run."""
        ctx.emit(RunEvent(
            type="step.probing_with_held_tct",
            initiator=caller.agent_id, target=target.agent_id,
            capability=capability,
        ))
        async with self._agent_client(30.0) as client:
            r = await client.post(
                f"{self.loopback.origin(caller.port)}/admin/invoke",
                json={
                    "peer_port": target.port,
                    "capability": capability,
                    "payload": payload,
                },
            )
            r.raise_for_status()
            body = r.json()
        # /admin/invoke wraps 4xx/5xx into {error: true, status_code, body}; 2xx
        # responses are the raw peer body, which we treat as status 200.
        if isinstance(body, dict) and body.get("error"):
            observed = int(body.get("status_code", 0))
            inner_body = body.get("body")
        else:
            observed = 200
            inner_body = body
        rejected = observed in (401, 403)
        matched = (expect_status is None) or (observed == expect_status)
        event_type = "step.access_denied" if (rejected and matched) else "step.unexpected_status" if not matched else "step.complete"
        ctx.emit(RunEvent(
            type=event_type,
            initiator=caller.agent_id, target=target.agent_id,
            capability=capability,
            result={"status_code": observed},
        ))
        return {
            "status_code": observed,
            "rejected": rejected,
            "expected_status": expect_status,
            "matched": matched,
            "body": inner_body,
        }

    # ---- helpers ----

    def _find_capability_holder(
        self,
        capability: str,
        scenario: ScenarioVersion,
        resolved_manifests: dict[str, Any],
        prefer: Optional[str] = None,
    ) -> Optional[str]:
        """Return the agent id that offers ``capability``.

        If ``prefer`` is set and that agent offers the capability, it wins —
        this keeps step ``agent: X, capability: Y`` as a self-execute when X
        actually offers Y, instead of routing to some other agent that also
        offers Y (e.g. delegation-chain has both ``researcher`` and
        ``sub-researcher`` offering ``research.query``).
        """
        if prefer is not None:
            manifest = resolved_manifests.get(prefer)
            if manifest is not None and capability in manifest.spec.aitp.offered_caps:
                return prefer
        for agent_spec in scenario.spec.agents:
            manifest = resolved_manifests[agent_spec.id]
            if capability in manifest.spec.aitp.offered_caps:
                return agent_spec.id
        return None

    @staticmethod
    def _find_tct_jti(
        events: list[RunEvent],
        *,
        audience: str,
        issuer: str,
    ) -> Optional[str]:
        """Walk the event log backwards looking for the TCT that ``issuer``
        granted to ``audience`` (i.e. the audience initiated the handshake
        toward the issuer). Returns the most recent ``jti`` or None."""
        for event in reversed(events):
            if (
                event.type == "trust.established"
                and event.initiator == audience
                and event.target == issuer
                and event.jti
            ):
                return event.jti
        return None

    def _resolve_step_input(
        self,
        step: WorkflowStep,
        inputs: dict[str, Any],
        step_outputs: dict[str, Any],
    ) -> Any:
        if step.input_from and step.input_from in step_outputs:
            return step_outputs[step.input_from]
        if step.input_template:
            rendered = step.input_template
            for k, v in inputs.items():
                rendered = rendered.replace(f"{{{{ inputs.{k} }}}}", str(v))
            return rendered
        return inputs

    async def _ensure_trust(
        self,
        initiator: RunningAgent,
        target: RunningAgent,
        peer_info: dict[str, Any],
        requested_grants: Optional[list[str]],
        ctx: RunContext,
    ) -> None:
        ctx.emit(RunEvent(
            type="trust.establishing",
            initiator=initiator.agent_id, target=target.agent_id,
        ))
        async with self._agent_client(20.0) as client:
            r = await client.post(
                f"{self.loopback.origin(initiator.port)}/admin/initiate-handshake",
                json={
                    "peer_manifest_url": peer_info["manifest_url"],
                    # Pass an empty list to request every capability the peer offers.
                    "requested_grants": list(requested_grants or []),
                },
            )
            r.raise_for_status()
            data = r.json()
        ctx.emit(RunEvent(
            type="trust.established",
            initiator=initiator.agent_id,
            target=target.agent_id,
            grants=list(data.get("grants", [])),
            jti=data.get("jti"),
        ))

    async def _push_trusted_peers(
        self, running: dict[str, RunningAgent], ctx: RunContext,
    ) -> None:
        """Replace each agent's pinned-key trust store with the AIDs of the
        other agents in this run.

        The AIDs are ``RunningAgent.aid``: read by the supervisor from each
        child's own ``AITP_AGENT_READY`` line over the parent-child pipe (see
        the ``cp_provision_trust_anchor`` step for why that channel is the
        authentic one), or from the agent's own ``/admin/rotate-keys``
        response after a rotation. Never from a HELLO: a peer's self-asserted
        AID is exactly what the trust store exists to check.
        """
        async with self._agent_client(10.0) as client:
            for agent_id, ra in running.items():
                aids = sorted(
                    other.aid for other_id, other in running.items()
                    if other_id != agent_id and other.aid
                )
                r = await client.post(
                    f"{self.loopback.origin(ra.port)}/admin/trust-peers",
                    json={"aids": aids, "replace": True},
                )
                r.raise_for_status()
        ctx.emit(RunEvent(
            type="trust.peers_pinned",
            peers={agent_id: ra.aid for agent_id, ra in running.items()},
        ))

    async def _establish_pairwise_trust(
        self,
        scenario: ScenarioVersion,
        running: dict[str, RunningAgent],
        peers: dict[str, dict[str, Any]],
        ctx: RunContext,
    ) -> None:
        """Run a bidirectional handshake between every pair of agents.

        Mutual handshakes only give the *initiator* a TCT back, so we run the
        handshake in both directions. After this, every agent holds a TCT for
        every other agent it might call.
        """
        agent_ids = [a.id for a in scenario.spec.agents]
        for i, a_id in enumerate(agent_ids):
            for b_id in agent_ids[i + 1:]:
                a, b = running[a_id], running[b_id]
                await self._ensure_trust(a, b, peers[b_id], None, ctx)
                await self._ensure_trust(b, a, peers[a_id], None, ctx)

    async def _invoke_capability(
        self,
        caller: RunningAgent,
        target: RunningAgent,
        capability: str,
        payload: Any,
        ctx: RunContext,
    ) -> Any:
        async with self._agent_client(180.0) as client:
            r = await client.post(
                f"{self.loopback.origin(caller.port)}/admin/invoke",
                json={
                    "peer_port": target.port,
                    "capability": capability,
                    "payload": payload,
                },
            )
            r.raise_for_status()
            try:
                body = r.json()
            except json.JSONDecodeError:
                return {"raw": r.text}
        # /admin/invoke wraps inner 4xx/5xx in an error envelope. For a normal
        # workflow step that's a hard failure — the chain can't continue with
        # a rejection as its "result". Probe steps use the dedicated path.
        if isinstance(body, dict) and body.get("error"):
            raise PlaygroundError(
                f"peer rejected {capability}: "
                f"status={body.get('status_code')} body={body.get('body')}"
            )
        return body

    async def _self_execute(
        self,
        runner_agent: RunningAgent,
        capability: str,
        payload: Any,
    ) -> Any:
        async with self._agent_client(180.0) as client:
            r = await client.post(
                f"{self.loopback.origin(runner_agent.port)}/admin/self-execute",
                json={"capability": capability, "payload": payload},
            )
            # Mirror _invoke_capability's body-surfacing rather than a blind
            # raise_for_status(): a bare httpx.HTTPStatusError's message is
            # just "{status} {reason} for url '...'" with no body, so a
            # well-formed error the agent returns (e.g. call_llm_or_502's 502)
            # would otherwise be discarded here even though it's diagnosable.
            if not r.is_success:
                try:
                    detail = r.json()
                except json.JSONDecodeError:
                    detail = r.text
                raise PlaygroundError(
                    f"{runner_agent.agent_id} self-execute {capability} failed: "
                    f"status={r.status_code} detail={detail}"
                )
            try:
                return r.json()
            except json.JSONDecodeError:
                return {"raw": r.text}
