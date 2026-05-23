"""CrewAI research crew — with a deterministic fallback if CrewAI isn't installed."""
from __future__ import annotations

import os
from typing import Any


def _has_crewai_and_key() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import crewai  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def build_crew(inputs: dict[str, Any]):
    """Return an object with a .kickoff() method. Real CrewAI when available, stub otherwise.

    Recognized inputs:
      - topic: subject of research (required)
      - depth: "short" (default) — overview; "deep" — multi-angle deeper analysis
    """
    topic = str(inputs.get("topic", "AI"))
    depth = str(inputs.get("depth", "short")).lower()
    if depth not in ("short", "deep"):
        depth = "short"

    if _has_crewai_and_key():
        from crewai import Agent, Crew, Process, Task  # type: ignore

        researcher = Agent(
            role="Senior Research Analyst",
            goal=f"Surface high-signal facts and recent developments about: {topic}",
            backstory="Veteran analyst with a strong nose for primary sources.",
            verbose=False,
            allow_delegation=False,
        )
        if depth == "deep":
            task_desc = (
                f"Produce a thorough (500-800 word) brief on '{topic}'. "
                "Cover historical context, technical detail, key debates, and 3 "
                "likely future directions. Cite primary sources per claim."
            )
            expected = "A multi-section markdown brief with citations."
        else:
            task_desc = (
                f"Produce a concise (200-300 word) brief on '{topic}'. "
                "Group findings into bullet points with one-line evidence per bullet."
            )
            expected = "A markdown bullet list of 5-8 high-signal findings."
        research_task = Task(
            description=task_desc, expected_output=expected, agent=researcher,
        )
        return Crew(agents=[researcher], tasks=[research_task], process=Process.sequential)

    return _StubCrew(topic, depth)


class _StubCrew:
    """Deterministic stand-in for CrewAI that returns a believable shape."""

    def __init__(self, topic: str, depth: str = "short") -> None:
        self.topic = topic
        self.depth = depth

    def kickoff(self):
        if self.depth == "deep":
            text = (
                f"# Deep brief: {self.topic}\n\n"
                f"## Background\n"
                f"{self.topic} sits at the intersection of multiple research threads.\n\n"
                f"## Technical landscape\n"
                f"- Protocol design choices favor verifiable identity over centralized roots.\n"
                f"- Capability tokens narrow the blast radius of any single compromise.\n"
                f"- Federation models cluster around manifest-based discovery.\n\n"
                f"## Debates\n"
                f"- Pinned-key vs OIDC trust anchors; tradeoffs in revocation and rotation.\n"
                f"- Delegation depth: single-hop versus multi-hop chains.\n\n"
                f"## Likely directions\n"
                f"- Stronger session-bundle semantics for multi-party coordination.\n"
                f"- ZK proofs for compliance attestation without leaking PII.\n"
                f"- Standardized revocation distribution.\n"
            )
        else:
            text = (
                f"- {self.topic} continues to attract significant research interest.\n"
                f"- Recent work focuses on protocol design, trust, and federation.\n"
                f"- Agent identity is emerging as a foundational primitive.\n"
                f"- Cross-org workflows depend on verifiable manifests and TCTs.\n"
                f"- Production deployments still grapple with policy/grant alignment."
            )

        class _Result:
            def __init__(self, raw: str) -> None:
                self.raw = raw

            def __str__(self) -> str:
                return self.raw

        return _Result(text)
