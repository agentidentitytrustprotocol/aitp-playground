"""LangChain writer chain — with a deterministic fallback when LangChain isn't installed."""
from __future__ import annotations

from llm import build_chat_model  # type: ignore[import-not-found]


async def run_writer(findings: str, *, style: str = "casual") -> str:
    """Produce an article-style response built from the researcher's findings."""
    llm = build_chat_model()
    if llm is not None:
        try:
            from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore
        except Exception:  # noqa: BLE001
            return _stub_article(findings, style)
        sys_msg = SystemMessage(
            content=f"You are a {style} writer. Use the supplied findings to draft a tight 250-word article."
        )
        msg = HumanMessage(content=f"Findings:\n\n{findings}\n\nWrite the article now.")
        out = await llm.ainvoke([sys_msg, msg])
        return getattr(out, "content", str(out))

    return _stub_article(findings, style)


def _stub_article(findings: str, style: str) -> str:
    header = f"# Article ({style})\n\n"
    body = (
        "The agent ecosystem is evolving rapidly, and the following points stand out:\n\n"
        f"{findings.strip()}\n\n"
        "Taken together, these threads suggest that durable agent collaboration "
        "will rest on three pillars: verifiable identity, narrowly-scoped trust "
        "(TCTs), and recoverable session bundles. The next twelve months should "
        "reveal which design choices survive contact with production."
    )
    return header + body
