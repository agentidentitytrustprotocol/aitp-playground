"""The deny-set is two sets and a snapshot, not one union.

`RevocationState` exists because a single `set[str]` — which is what an agent's
deny-set was until now — cannot express RFC-AITP-0008. These tests pin the
three things that were previously unrepresentable, each of which Phase 6's
plan text assumed without the structure to back it:

* a snapshot **replaces**, so a jti the issuer stops listing stops being denied;
* a **previously verified snapshot** is a thing you can still point at, with the
  timestamps a freshness policy needs;
* **local revocations survive** every snapshot state, because an operator who
  revoked a token does not expect a CP refresh to un-revoke it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

from revocation_state import RevocationState, Snapshot  # noqa: E402


def _state_with_snapshot(entries, *, published_at=1_700_000_000, ttl=3600):
    state = RevocationState()
    state.apply_snapshot(entries, published_at=published_at, expires_at=published_at + ttl)
    return state


def test_a_snapshot_replaces_rather_than_merges() -> None:
    """The property a monotonic union could not have.

    Under the old `set.add()` loop a jti stayed denied forever once it appeared,
    so the agent's view was a high-water mark of everything the CP had ever
    said — which is not what a snapshot means, and makes un-revocation
    impossible.
    """
    state = _state_with_snapshot(["a", "b"])
    assert state.is_revoked("a")

    state.apply_snapshot(["b"], published_at=1_700_000_060, expires_at=1_700_003_660)

    assert not state.is_revoked("a"), (
        "a jti dropped from the issuer's snapshot is still denied — the "
        "snapshot was merged, not replaced"
    )
    assert state.is_revoked("b")


def test_local_revocations_survive_every_snapshot_state() -> None:
    """Local and CP-derived denials are separate sets, unioned at enforcement."""
    state = RevocationState()
    state.revoke_local("local-jti")

    # A snapshot arrives, then a later one that mentions nothing.
    state.apply_snapshot(["cp-jti"], published_at=1_700_000_000, expires_at=1_700_003_600)
    assert state.is_revoked("local-jti") and state.is_revoked("cp-jti")

    state.apply_snapshot([], published_at=1_700_000_060, expires_at=1_700_003_660)
    assert state.is_revoked("local-jti"), (
        "a snapshot refresh cleared an operator's own revocation"
    )
    assert not state.is_revoked("cp-jti")


def test_the_source_of_a_denial_is_distinguishable() -> None:
    """"We revoked this" and "the CP says someone did" are different facts.

    The 403 detail names which, so a CP-propagation bug cannot be mistaken for
    a local one.
    """
    state = RevocationState()
    state.revoke_local("mine")
    state.apply_snapshot(["theirs"], published_at=1_700_000_000, expires_at=1_700_003_600)

    assert state.is_locally_revoked("mine")
    assert not state.is_locally_revoked("theirs")
    assert state.is_revoked("theirs")


def test_no_snapshot_is_not_the_same_as_an_empty_one() -> None:
    """An empty *signed* snapshot is a meaningful assertion.

    RFC-AITP-0008 §1.5: empty lists are signed, and mean "nothing is revoked".
    Never having heard from the issuer is a different state, and a freshness
    policy has to be able to tell them apart.
    """
    never_heard = RevocationState()
    assert never_heard.snapshot is None

    heard_nothing_revoked = _state_with_snapshot([])
    assert heard_nothing_revoked.snapshot is not None
    assert heard_nothing_revoked.snapshot_entry_count == 0


def test_effective_jtis_cannot_be_used_to_mutate_the_state() -> None:
    """The SDK receives this set. It must not be a handle on our internals."""
    state = RevocationState()
    state.revoke_local("a")

    handed_out = state.effective_jtis
    handed_out.add("smuggled")

    assert not state.is_revoked("smuggled")


def test_snapshot_age_is_measured_from_published_at_not_from_fetch() -> None:
    """A snapshot the CP served from cache is already old on arrival.

    The control plane re-signs at most every 60s, so measuring age from when
    *we* fetched it understates it by up to a minute — and a staleness budget
    built on that measures the wrong thing.
    """
    snap = Snapshot(entries=frozenset(), published_at=1_700_000_000, expires_at=1_700_003_600)
    assert snap.age_secs(now=1_700_000_120) == 120
    assert not snap.is_expired(now=1_700_000_120)
    assert snap.is_expired(now=1_700_003_600), "expiry is inclusive of the deadline"


def test_snapshot_entries_are_immutable() -> None:
    snap = Snapshot(entries=frozenset({"a"}), published_at=0, expires_at=1)
    with pytest.raises(AttributeError):
        snap.entries = frozenset()  # type: ignore[misc]
