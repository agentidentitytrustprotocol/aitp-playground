"""AgentSupervisor: spawn, watch, and kill agent subprocesses."""
from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RunningAgent:
    run_id: str
    agent_id: str
    port: int
    pid: Optional[int]
    aid: str
    manifest_url: str
    status: str = "starting"
    exit_code: Optional[int] = None


class AgentSupervisor:
    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._agents: dict[str, RunningAgent] = {}

    @staticmethod
    def _key(run_id: str, agent_id: str) -> str:
        return f"{run_id}:{agent_id}"

    async def launch(
        self,
        *,
        run_id: str,
        agent_id: str,
        prepared,  # PreparedLaunch (avoid cyclic import)
        port: int,
        startup_timeout_ms: int = 30_000,
    ) -> RunningAgent:
        key = self._key(run_id, agent_id)
        logger.info("Launching agent %s on port %d", agent_id, port)
        proc = subprocess.Popen(
            [prepared.command, *prepared.args],
            cwd=prepared.cwd,
            env=prepared.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        agent = RunningAgent(
            run_id=run_id,
            agent_id=agent_id,
            port=port,
            pid=proc.pid,
            aid="",
            manifest_url=f"http://localhost:{port}/.well-known/aitp-manifest",
        )
        self._processes[key] = proc
        self._agents[key] = agent

        loop = asyncio.get_event_loop()
        deadline = loop.time() + startup_timeout_ms / 1000
        while True:
            if loop.time() > deadline:
                proc.kill()
                raise TimeoutError(
                    f"{agent_id} did not signal ready within {startup_timeout_ms}ms"
                )
            if proc.stdout is None:
                raise RuntimeError("subprocess opened without stdout pipe")
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line:
                # Pipe closed → process exited before signaling ready
                rc = proc.poll()
                stderr = ""
                if proc.stderr is not None:
                    stderr = proc.stderr.read() or ""
                raise RuntimeError(
                    f"{agent_id} exited (rc={rc}) before ready. stderr:\n{stderr}"
                )
            line = line.rstrip()
            logger.info("[%s:%s] %s", run_id, agent_id, line)
            if line.startswith("AITP_AGENT_READY"):
                parts = dict(p.split("=", 1) for p in line.split()[1:] if "=" in p)
                agent.aid = parts.get("aid", "")
                agent.status = "ready"
                # Drain stdout and stderr in the background to prevent pipe blocking
                loop.create_task(self._drain(run_id, agent_id, proc, "stdout"))
                loop.create_task(self._drain(run_id, agent_id, proc, "stderr"))
                break

        return agent

    async def _drain(
        self, run_id: str, agent_id: str, proc: subprocess.Popen[str], stream_name: str
    ) -> None:
        stream = getattr(proc, stream_name)
        if stream is None:
            return
        loop = asyncio.get_event_loop()
        log = logger.info if stream_name == "stdout" else logger.warning
        while True:
            line = await loop.run_in_executor(None, stream.readline)
            if not line:
                break
            log("[%s:%s:%s] %s", run_id, agent_id, stream_name, line.rstrip())

    def kill(self, run_id: str, agent_id: str) -> None:
        key = self._key(run_id, agent_id)
        proc = self._processes.pop(key, None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if key in self._agents:
            self._agents[key].status = "stopped"
            self._agents[key].exit_code = proc.returncode if proc else None

    def kill_run(self, run_id: str) -> None:
        for key in list(self._processes):
            if key.startswith(f"{run_id}:"):
                _, agent_id = key.split(":", 1)
                self.kill(run_id, agent_id)

    def list_running(self) -> list[RunningAgent]:
        return [a for a in self._agents.values() if a.status == "ready"]

    def get(self, run_id: str, agent_id: str) -> Optional[RunningAgent]:
        return self._agents.get(self._key(run_id, agent_id))
