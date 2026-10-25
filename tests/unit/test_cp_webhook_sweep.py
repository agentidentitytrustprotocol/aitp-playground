"""The startup sweep of CP webhooks a previous process left behind.

No run survives a restart, so every subscription under this deployment's own
`/webhooks/cp/` URL is stale, except one this process has already made.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

from aitp_playground.config import Settings
from aitp_playground.main import sweep_cp_webhooks

BASE = "https://playground.test"


class FakeCp:
    def __init__(self, hooks: Optional[list[dict[str, Any]]]) -> None:
        self.hooks = hooks
        self.deleted: list[str] = []

    async def list_webhooks(self) -> Optional[list[dict[str, Any]]]:
        return self.hooks

    async def delete_webhook(self, webhook_id: str) -> bool:
        self.deleted.append(webhook_id)
        return True


def _sweep(cp: FakeCp, owned: set[str] = frozenset()) -> Any:
    return sweep_cp_webhooks(
        cp, Settings(playground_base_url=BASE + "/"), SimpleNamespace(webhook_ids=set(owned)),
    )


async def test_only_our_own_stale_webhooks_are_deleted() -> None:
    cp = FakeCp([
        {"id": "old", "url": f"{BASE}/webhooks/cp/run-a"},
        {"id": "live", "url": f"{BASE}/webhooks/cp/run-b"},
        {"id": "theirs", "url": "https://other.test/webhooks/cp/run-a"},
        {"id": "lookalike", "url": f"{BASE}.evil/webhooks/cp/run-a"},
        {"id": "elsewhere", "url": f"{BASE}/hooks/run-a"},
        {"url": f"{BASE}/webhooks/cp/no-id"},
    ])

    assert await _sweep(cp, owned={"live"}) == 1
    assert cp.deleted == ["old"]


async def test_a_cp_that_cannot_list_deletes_nothing() -> None:
    cp = FakeCp(None)
    assert await _sweep(cp) == 0
    assert cp.deleted == []
