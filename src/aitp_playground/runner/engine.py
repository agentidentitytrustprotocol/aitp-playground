"""ScenarioRunner: full end-to-end orchestration of a scenario run."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
import jsonschema

from ..config import Settings
from ..cp_client.client import CpClient
from ..errors import PlaygroundError
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
    ) -> None:
        self.registry = registry
        self.supervisor = supervisor
        self.bootstrap_builder = bootstrap_builder
        self.adapters = adapters
        self.trust = trust
        self.cp = cp
        self.ports = port_alloc
        self.config = config
        self.store = store
        self._bg_tasks: set[asyncio.Task[Any]] = set()

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
        self.store.upsert(run_id, {
            "run_id": run_id, "status": "running", "scenario_ref": scenario_ref,
            "outputs": {}, "events": [], "error": None,
        })

        # 1. Load scenario (optionally merged with a named template)
        try:
            scenario = self.registry.get_scenario_resolved(
                scenario_ref, template=template,
            )
        except PlaygroundError as exc:
            ctx.emit(RunEvent(type="run.failed", error=str(exc)))
            return self._finalize_failure(run_id, str(exc), ctx)

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
            ctx.emit(RunEvent(type="run.failed", error=f"inputs validation: {exc.message}"))
            return self._finalize_failure(run_id, f"inputs validation: {exc.message}", ctx)

        # 3. Assign ports + resolve manifests + write bootstrap
        ports = {a.id: self.ports.allocate(offset=a.port_offset) for a in scenario.spec.agents}
        resolved_manifests = {
            a.id: self.registry.get_agent_manifest(a.ref) for a in scenario.spec.agents
        }
        bootstrap_files: dict[str, str] = {}
        for agent_spec in scenario.spec.agents:
            placeholder_peers = {
                other.id: {
                    "manifest_url": f"http://localhost:{ports[other.id]}/.well-known/aitp-manifest",
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
            )
            bootstrap_files[agent_spec.id] = self.bootstrap_builder.write(bs)

        # 4. Spawn all agents
        running: dict[str, RunningAgent] = {}
        try:
            for agent_spec in scenario.spec.agents:
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
                    startup_timeout_ms=int(manifest.spec.host.get("startupTimeoutMs", 30_000)),
                )
                running[agent_spec.id] = ra
                ctx.emit(RunEvent(type="agent.ready", agent_id=agent_spec.id, aid=ra.aid, port=ra.port))

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
            logger.exception("Run %s failed: %s", run_id, exc)
            ctx.emit(RunEvent(type="run.failed", error=str(exc)))
            self._cleanup_run(run_id, ports, bootstrap_files)
            return self._finalize_failure(run_id, str(exc), ctx)
        finally:
            self._cleanup_run(run_id, ports, bootstrap_files)

        ctx.emit(RunEvent(type="run.complete"))
        task = asyncio.create_task(self.cp.ingest_events(ctx.events))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

        result = RunResult.success(run_id, step_outputs, ctx.events)
        # Events are already in the store via RunContext.emit → store.append_event;
        # don't pass them here or upsert's merge would double them.
        self.store.upsert(run_id, {
            "run_id": run_id, "status": "success", "scenario_ref": scenario_ref,
            "outputs": step_outputs,
            "error": None,
        })
        return result

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

    def _finalize_failure(self, run_id: str, error: str, ctx: RunContext) -> RunResult:
        self.store.upsert(run_id, {
            "run_id": run_id, "status": "failed",
            "outputs": {},
            "error": error,
        })
        return RunResult.failure(run_id, error, events=ctx.events)

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
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    f"http://localhost:{delegator_ra.port}/admin/delegate",
                    json={
                        "held_tct_peer_port": via_ra.port,
                        "delegatee_manifest_url": delegatee_manifest_url,
                        "scope": scope,
                        "ttl_secs": step.ttl_secs,
                    },
                )
                r.raise_for_status()
                data = r.json()
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
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    f"http://localhost:{delegatee_ra.port}/admin/redeem-delegation",
                    json={
                        "redeem_url": redeem_url,
                        "delegation_token": prior["delegation_token"],
                        "peer_port": target_ra.port,
                    },
                )
                r.raise_for_status()
                data = r.json()
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
            issuer_ra = running[step.issuer]
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(
                    f"http://localhost:{issuer_ra.port}/admin/revoke-tct",
                    json={"jti": jti},
                )
                r.raise_for_status()

            published_to_cp = False
            audience_refreshed = 0
            if step.via_cp:
                # End-to-end revocation: publish the jti to the CP, then
                # pull the updated revocation list into the audience's
                # local deny-set so the audience also fails closed. The
                # issuer rejects on its local list; the audience rejection
                # is the federation story.
                published_to_cp = await self.cp.publish_revocation(
                    jti, reason=step.reason or f"step {step.id}",
                )
                ctx.emit(RunEvent(
                    type="revocation.published",
                    step_id=step.id, jti=jti,
                    result={"to_cp": published_to_cp, "reason": step.reason},
                ))
                audience_ra = running[step.audience]
                refresh_body = {
                    "cp_base_url": self.config.cp_base_url,
                    "cp_api_key": self.config.cp_api_key,
                }
                async with httpx.AsyncClient(timeout=10.0) as client:
                    rr = await client.post(
                        f"http://localhost:{audience_ra.port}/admin/refresh-revocations",
                        json=refresh_body,
                    )
                    # Graceful: if the audience can't reach CP, the step
                    # still records what happened — the test harness will
                    # see audience_refreshed=0 and can decide whether to
                    # fail.
                    if rr.is_success:
                        audience_refreshed = int(rr.json().get("revoked_count", 0))

            result_payload = {"revoked_jti": jti}
            if step.via_cp:
                result_payload.update({
                    "published_to_cp": published_to_cp,
                    "audience_revoked_count": audience_refreshed,
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
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    f"http://localhost:{ra.port}/admin/enroll-with-cp",
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

        if step_type == "rotate_keys":
            if not step.agent:
                raise PlaygroundError(
                    f"step {step.id}: rotate_keys requires agent"
                )
            ra = running[step.agent]
            old_aid = ra.aid
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"http://localhost:{ra.port}/admin/rotate-keys",
                    json={},
                )
                r.raise_for_status()
                data = r.json()
            new_aid = data.get("aid") or ""
            ra.aid = new_aid
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
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"http://localhost:{target.port}/capabilities/{capability}",
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
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"http://localhost:{caller.port}/admin/invoke",
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
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"http://localhost:{initiator.port}/admin/initiate-handshake",
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
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                f"http://localhost:{caller.port}/admin/invoke",
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
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                f"http://localhost:{runner_agent.port}/admin/self-execute",
                json={"capability": capability, "payload": payload},
            )
            r.raise_for_status()
            try:
                return r.json()
            except json.JSONDecodeError:
                return {"raw": r.text}
