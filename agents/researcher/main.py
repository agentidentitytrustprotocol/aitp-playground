"""CrewAI researcher agent worker — identity & trust via aitp-py."""
from __future__ import annotations

import json
from typing import Any

import uvicorn
from fastapi import FastAPI, Request

from agent_admin import build_admin_router
from revocation_state import RevocationState
from aitp_server import AitpServer, ready_lifespan
from bootstrap import create_agent, get_manifest_json, load_bootstrap
from telemetry import emit_event

from .crew import build_crew  # type: ignore[import-not-found]


bootstrap = load_bootstrap()
PORT = int(bootstrap["port"])
agent = create_agent(bootstrap)
manifest_json = get_manifest_json(agent, bootstrap)

app = FastAPI(
    title=f"agent-{bootstrap['agent_id']}",
    lifespan=ready_lifespan(aid=agent.aid, port=PORT),
)

# Shared by AitpServer (which checks it on every capability call) and the admin
# router's /admin/revoke-tct + /admin/refresh-revocations (which mutate it).
# Module-level so every request sees the same state. It keeps local
# revocations and the CP snapshot separate — see revocation_state.py.
_revocation = RevocationState()

server = AitpServer(
    agent=agent,
    manifest_json=manifest_json,
    port=PORT,
    bootstrap=bootstrap,
    did_web_host=bootstrap["aitp"].get("did_web_host"),
    did_web_scheme=bootstrap["aitp"].get("did_web_scheme", "http"),
    revocation=_revocation,
)
app.include_router(server.router)


def _payload_to_topic(payload: Any) -> str:
    """Normalize a self-execute or HTTP payload into a topic string."""
    if payload is None:
        return str(bootstrap["inputs"].get("topic", "AI"))
    if isinstance(payload, dict):
        return str(payload.get("topic") or payload.get("text") or payload)
    if isinstance(payload, str):
        s = payload.strip()
        if s.startswith('"') and s.endswith('"'):
            try:
                return str(json.loads(s))
            except Exception:  # noqa: BLE001
                return s
        return s
    return str(payload)


async def do_research(payload: Any, commissioned_by: str = "self") -> dict[str, Any]:
    topic = _payload_to_topic(payload)
    await emit_event(
        "llm.started", bootstrap, task="research",
        topic=topic, commissioned_by=commissioned_by,
    )
    crew = build_crew({"topic": topic})
    # CrewAI ≥1.0 refuses sync kickoff() inside a running event loop. Use the
    # async variant when available; the offline stub only has the sync one.
    result = await crew.kickoff_async() if hasattr(crew, "kickoff_async") else crew.kickoff()
    findings = str(result.raw) if hasattr(result, "raw") else str(result)
    await emit_event("llm.complete", bootstrap, task="research", topic=topic)
    return {"findings": findings, "topic": topic, "agent": bootstrap["agent_id"]}


async def do_deep_research(payload: Any, commissioned_by: str = "self") -> dict[str, Any]:
    topic = _payload_to_topic(payload)
    await emit_event(
        "llm.started", bootstrap, task="research.deep",
        topic=topic, commissioned_by=commissioned_by,
    )
    crew = build_crew({"topic": topic, "depth": "deep"})
    result = await crew.kickoff_async() if hasattr(crew, "kickoff_async") else crew.kickoff()
    findings = str(result.raw) if hasattr(result, "raw") else str(result)
    await emit_event("llm.complete", bootstrap, task="research.deep", topic=topic)
    return {"deep_findings": findings, "topic": topic, "agent": bootstrap["agent_id"]}


_held_tcts: dict[int, str] = {}
# Both capabilities are registered. The actual capabilities the agent advertises
# come from its manifest; an unadvertised capability would just never be
# requested. Registering both keeps the same Python image usable with both the
# `researcher` and `researcher-extended` manifests.
app.include_router(build_admin_router(
    agent=agent,
    bootstrap=bootstrap,
    held_tcts=_held_tcts,
    revocation=_revocation,
    issued_tcts=server._issued_tcts,
    capabilities={
        "research.query": do_research,
        "research.deep": do_deep_research,
    },
    # Bound to the freshness accessor, not the stored string, so
    # /admin/enroll-with-cp sends the *current* manifest: it reflects a
    # rotate-keys call automatically, and — since the CP verifies what it is
    # sent — it is also re-minted before it can expire on a long-lived agent.
    manifest_provider=server._fresh_manifest_json,
))


@app.post("/capabilities/research.query")
async def research_query(request: Request) -> dict[str, Any]:
    tct_json = request.headers.get("x-aitp-tct", "")
    identity = server.verify_capability_tct(tct_json, "research.query")
    body_bytes = await request.body()
    payload: Any
    if body_bytes:
        text = body_bytes.decode()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text
    else:
        payload = None
    return await do_research(payload, commissioned_by=identity.peer_aid)


@app.post("/capabilities/research.deep")
async def research_deep(request: Request) -> dict[str, Any]:
    tct_json = request.headers.get("x-aitp-tct", "")
    identity = server.verify_capability_tct(tct_json, "research.deep")
    body_bytes = await request.body()
    payload: Any
    if body_bytes:
        text = body_bytes.decode()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text
    else:
        payload = None
    return await do_deep_research(payload, commissioned_by=identity.peer_aid)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
