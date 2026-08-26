"""LangChain writer agent worker — identity & trust via aitp-py."""
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

from .chain import run_writer  # type: ignore[import-not-found]


bootstrap = load_bootstrap()
PORT = int(bootstrap["port"])
agent = create_agent(bootstrap)
manifest_json = get_manifest_json(agent, bootstrap)

# Shared by AitpServer (enforcement) and the admin router
# (/admin/revoke-tct + /admin/refresh-revocations). Keeps local
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
app = FastAPI(
    title=f"agent-{bootstrap['agent_id']}",
    # Constructed AFTER the server so the lifespan can own the background
    # revocation poll; without a cadence the staleness budget is meaningless.
    lifespan=ready_lifespan(aid=agent.aid, port=PORT, server=server),
)
app.include_router(server.router)


def _payload_to_findings(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, dict):
        return str(payload.get("findings") or payload.get("text") or payload)
    if isinstance(payload, str):
        s = payload.strip()
        if s.startswith('"') and s.endswith('"'):
            try:
                return str(json.loads(s))
            except Exception:  # noqa: BLE001
                return s
        return s
    return str(payload)


async def do_write(payload: Any, commissioned_by: str = "self") -> dict[str, Any]:
    findings = _payload_to_findings(payload)
    style = str(bootstrap["inputs"].get("style", "casual"))
    await emit_event(
        "llm.started", bootstrap, task="write", commissioned_by=commissioned_by,
    )
    article = await run_writer(findings, style=style)
    await emit_event("llm.complete", bootstrap, task="write")
    return {"article": article, "style": style, "agent": bootstrap["agent_id"]}


_held_tcts: dict[int, str] = {}
app.include_router(build_admin_router(
    agent=agent,
    bootstrap=bootstrap,
    held_tcts=_held_tcts,
    revocation=_revocation,
    capabilities={"write.content": do_write},
    manifest_provider=server._fresh_manifest_json,
    issued_tcts=server._issued_tcts,
))


@app.post("/capabilities/write.content")
async def write_content(request: Request) -> dict[str, Any]:
    tct_json = request.headers.get("x-aitp-tct", "")
    identity = server.verify_capability_tct(tct_json, "write.content")
    body = await request.body()
    payload: Any
    if body:
        text = body.decode()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text
    else:
        payload = None
    return await do_write(payload, commissioned_by=identity.peer_aid)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
