"""Human-readable narration of a scenario-run event log.

For each ``RunEvent``-shaped dict the narrator produces one short line
explaining what just happened at the protocol level. Used by:

  - GET /runs/{id}/narrate to return a text/plain rendering of a run.
  - The ``aitp-playground trace`` CLI to print the same lines.

The narrator is intentionally a pure function (no I/O, no state) so the
event log can be replayed later from any source — e.g. piped through a
shell filter or rendered from a stored file.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping


def _short(value: Any, n: int = 20) -> str:
    s = str(value or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def narrate_event(event: Mapping[str, Any]) -> str:
    """Render a single event as one narrative line. Returns the empty
    string for events the narrator chooses not to surface (so callers
    can ``filter(None, ...)`` the output)."""
    etype = event.get("type") or ""
    sid = event.get("step_id") or ""
    initiator = event.get("initiator") or ""
    target = event.get("target") or ""
    agent_id = event.get("agent_id") or event.get("agent") or ""
    capability = event.get("capability") or ""
    grants = event.get("grants") or []
    jti = event.get("jti") or ""
    error = event.get("error") or ""
    result = event.get("result") if isinstance(event.get("result"), dict) else {}

    # Run lifecycle.
    if etype == "run.started":
        return f"[run] started — scenario={event.get('scenario_ref','?')}"
    if etype == "run.complete":
        return "[run] complete"
    if etype == "run.failed":
        return f"[run] FAILED — {error}"
    if etype == "run.cancelled":
        return "[run] cancelled"

    # Agent lifecycle.
    if etype == "agent.spawning":
        return f"[agent] spawning {agent_id} on port {event.get('port')}"
    if etype == "agent.ready":
        return (
            f"[agent] ready    {agent_id}  aid={_short(event.get('aid'), 28)}  "
            f"port={event.get('port')}"
        )

    # Trust + handshake.
    if etype == "trust.peers_resolved":
        peers = event.get("peers") or {}
        return f"[trust] peer manifest URLs resolved ({len(peers)})"
    if etype == "trust.establishing":
        return f"[trust] handshaking  {initiator} -> {target}"
    if etype == "trust.established":
        g = ",".join(grants) if grants else "<all-offered>"
        return (
            f"[trust] established  {initiator} <- {target}  "
            f"grants=[{g}]  jti={_short(jti, 14)}"
        )
    if etype == "handshake.failed":
        return f"[trust] FAILED handshake — {error}"

    # Delegation.
    if etype == "delegation.issuing":
        g = ",".join(grants) if grants else ""
        return f"[delegate] issuing  {initiator} -> {target}  scope=[{g}]"
    if etype == "delegation.issued":
        return "[delegate] issued"
    if etype == "delegation.redeeming":
        return f"[delegate] redeeming  {initiator} -> {target}"
    if etype == "delegation.redeemed":
        return "[delegate] redeemed — fresh TCT minted for delegatee"
    if etype == "delegation.rejected":
        return f"[delegate] REJECTED — {error}"

    # Revocation.
    if etype == "tct.revoked":
        return f"[revoke] local deny-set updated  jti={_short(jti, 14)}"
    if etype == "revocation.published":
        ok = result.get("to_cp") if isinstance(result, dict) else None
        return (
            f"[revoke] published to CP  jti={_short(jti, 14)}  "
            f"to_cp={ok if ok is not None else '?'}"
        )
    if etype == "revocation.list_fetched":
        return (
            f"[revoke] CP revocation-list refreshed  "
            f"jti_count={event.get('jti_count','?')}  added={event.get('added','?')}"
        )
    if etype == "revocation.refresh_failed":
        return f"[revoke] CP revocation refresh FAILED — {error}"

    # Identity rotation.
    if etype == "identity.key.rotated":
        return f"[identity] {agent_id} rotated keys  new_aid={_short(event.get('aid'), 28)}"

    # Workflow / capability calls.
    if etype == "step.started":
        return f"[step]  starting  id={sid}  {agent_id}.{capability}"
    if etype == "step.complete":
        if capability:
            return f"[step]  ok       id={sid}  {agent_id}.{capability}"
        if isinstance(result, dict) and "revoked_jti" in result:
            return f"[step]  ok       id={sid}  revoke_tct"
        if isinstance(result, dict) and "delegatee_aid" in result:
            return f"[step]  ok       id={sid}  delegate"
        return f"[step]  ok       id={sid}"
    if etype == "step.skipped":
        return f"[step]  skipped  id={sid}  ({event.get('notes') or ''})"
    if etype == "step.probing_no_trust":
        return f"[probe] no-trust call  {initiator} -> {target}.{capability}"
    if etype == "step.probing_with_held_tct":
        return f"[probe] with-TCT call  {initiator} -> {target}.{capability}"
    if etype == "step.access_denied":
        sc = ""
        if isinstance(result, dict) and result.get("status_code"):
            sc = f" status={result['status_code']}"
        return f"[probe] DENIED   id={sid}  {capability}{sc}"
    if etype == "step.unexpected_status":
        sc = ""
        if isinstance(result, dict) and result.get("status_code"):
            sc = f" status={result['status_code']}"
        return f"[probe] UNEXPECTED  id={sid}{sc}"

    # Faults.
    if etype == "step.fault_injected":
        return f"[fault] INJECTED  id={sid}  target={target}  ({event.get('notes') or ''})"
    if etype == "step.fault_complete":
        err = result.get("error") if isinstance(result, dict) else ""
        return f"[fault] complete  id={sid}  -> {err or 'no-error?'}"

    # CP enrollment.
    if etype == "cp.enroll_started":
        return f"[cp]    enrolling {agent_id}"
    if etype == "cp.enroll_complete":
        return f"[cp]    enrolled  {agent_id}  aid={_short(event.get('aid'), 28)}"
    if etype == "cp.enroll_failed":
        return f"[cp]    enroll FAILED ({event.get('stage','?')}) — {error}"
    if etype == "cp.enroll_succeeded":
        return f"[cp]    registered {agent_id}"

    # OIDC, TCT renewal, session bundles, SPKI pinning.
    if etype == "oidc.issuer_minted":
        url = result.get("issuer_url") if isinstance(result, dict) else ""
        return f"[oidc]  per-run issuer minted  url={url}"
    if etype == "tct.renewed":
        return (
            f"[renew] {agent_id} <- {target}  "
            f"new_jti={_short(event.get('jti'), 14)}"
        )
    if etype == "session.bundle.exported":
        parts = result.get("participant_aids") if isinstance(result, dict) else []
        sid = result.get("session_id") if isinstance(result, dict) else ""
        return (
            f"[bundle] exported  session={_short(sid, 14)}  "
            f"participants={len(parts or [])}"
        )
    if etype == "session.bundle.verified":
        kind = result.get("kind") if isinstance(result, dict) else ""
        active = result.get("active_aids") if isinstance(result, dict) else []
        return f"[bundle] verified  kind={kind}  active={len(active or [])}"
    if etype == "spki.pin.checked":
        pinned = result.get("is_pinned") if isinstance(result, dict) else None
        return f"[spki]  pin checked  is_pinned={pinned}"

    # CP webhook fan-out.
    if etype == "cp.webhook.subscribed":
        wid = result.get("id") if isinstance(result, dict) else ""
        evs = result.get("events") if isinstance(result, dict) else []
        evs_view = ",".join(evs) if evs else "<all>"
        return f"[cp]    webhook subscribed  id={_short(wid, 14)}  events=[{evs_view}]"
    if etype == "cp.webhook.subscribe_failed":
        return f"[cp]    webhook subscribe FAILED — {event.get('notes') or '?'}"
    if etype == "cp.webhook.delivered":
        return (
            f"[cp]    webhook delivery  event_type={event.get('event_type','?')}  "
            f"delivery={_short(event.get('delivery_id'), 14)}"
        )

    # Capability self-execute.
    if etype == "capability.self_execute":
        return f"[exec]  self     {agent_id}.{capability}"

    # LLM activity (informational; not interesting at the protocol level
    # but useful for the narration to show duration of an llm step).
    if etype == "llm.started":
        return f"[llm]   started   task={event.get('task','?')}"
    if etype == "llm.complete":
        return f"[llm]   complete  task={event.get('task','?')}"

    # Unrecognized events are dropped from the narrative (the raw event
    # log is still available via GET /runs/{id}).
    return ""


def narrate_events(events: Iterable[Mapping[str, Any]]) -> list[str]:
    """Render a sequence of events, dropping the empty (unknown) ones."""
    out: list[str] = []
    for ev in events:
        line = narrate_event(ev)
        if line:
            out.append(line)
    return out
