"""An agent's revocation state: local revocations and the CP snapshot, apart.

Before this, an agent's deny-set was a single `set[str]` that both
`/admin/revoke-tct` and `/admin/refresh-revocations` called `.add()` on. That
shape cannot express what RFC-AITP-0008 actually specifies, in three separate
ways:

1. **A snapshot is the issuer's complete current deny-set, not an increment.**
   Under a union, a jti that leaves the snapshot stays denied forever, so
   un-revocation is impossible and the agent's view drifts from the issuer's.
2. **"The previously verified snapshot stays current" needs a snapshot to
   exist.** Discarding an unverifiable snapshot is only meaningful if the last
   good one is a thing you can still point at — with its `published_at` and
   `expires_at`, which is also what any freshness policy has to read.
3. **"Local revocations are enforced in every snapshot state" needs the two
   sets kept apart.** Merged, replacing the snapshot wholesale would silently
   drop the operator's own `/admin/revoke-tct` calls.

So: two sets, unioned only at the point of enforcement, plus the metadata of
the snapshot currently in force.

**This module deliberately performs no verification.** Deciding whether a
snapshot is authentic belongs to the SDK (`aitp.verify_revocation_list`); this
type only holds what was decided. Keeping the decision out of here is what
stops it from quietly becoming a second, hand-rolled trust boundary.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

#: What the agent can currently say about revocation.
#:
#: `unchecked` is not a failure — it is the explicitly-named posture for an
#: agent with no control plane, which mirrors the reference implementation's
#: stance: aitp-rs refuses to build a TCT verify context until the revocation
#: decision is made, waivable only by a method called
#: `accept_unchecked_revocation_dangerous`. No silent default; the unsafe
#: direction has to be named out loud.
Posture = Literal["unchecked", "current", "degraded"]


@dataclass(frozen=True)
class Snapshot:
    """A revocation snapshot that has been accepted into force.

    Immutable on purpose: a snapshot is replaced, never edited. `entries` is a
    frozenset so a caller cannot mutate the deny-set out from under the state
    that recorded when it was published.
    """

    entries: frozenset[str]
    published_at: int
    expires_at: int

    def age_secs(self, now: Optional[float] = None) -> float:
        """Seconds since the issuer signed this snapshot.

        Age is measured from `published_at`, not from when we fetched it: a
        snapshot the CP has been serving from cache for a minute is already a
        minute old on arrival, and a freshness budget that ignores that is
        measuring the wrong thing.
        """
        return (time.time() if now is None else now) - self.published_at

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at


@dataclass
class RevocationState:
    """Local revocations, the snapshot in force, and the union of the two.

    Shared by reference between `AitpServer` (which enforces) and the admin
    router (which mutates) — the same sharing the bare `set` had, with a type
    that can answer more than "is this jti in here".
    """

    #: Revocations this agent made itself, via `/admin/revoke-tct`. These never
    #: depend on the control plane and are never cleared by a snapshot: an
    #: operator who revoked a token locally does not expect a CP refresh to
    #: un-revoke it.
    local: set[str] = field(default_factory=set)

    #: The snapshot currently in force, or None if none has ever been accepted.
    #: `None` is meaningfully different from an empty snapshot: an empty
    #: *signed* snapshot is the issuer asserting "nothing is revoked"
    #: (RFC-AITP-0008 §1.5), while None means we have never heard from them.
    snapshot: Optional[Snapshot] = None

    # ── mutation ─────────────────────────────────────────────────────────
    def revoke_local(self, jti: str) -> None:
        self.local.add(jti)

    def apply_snapshot(
        self, entries: Iterable[str], *, published_at: int, expires_at: int
    ) -> None:
        """Put a snapshot into force, **replacing** any previous one.

        Wholesale replacement is the point. Merging would make the agent's view
        a high-water mark of everything the CP has ever said, which is not what
        a snapshot means and cannot represent an un-revocation.

        Call this only for a snapshot that has been *verified*. Verification
        lives at the call site, not here.
        """
        self.snapshot = Snapshot(
            entries=frozenset(entries),
            published_at=int(published_at),
            expires_at=int(expires_at),
        )

    # ── enforcement ──────────────────────────────────────────────────────
    @property
    def effective_jtis(self) -> set[str]:
        """Everything currently denied: local revocations ∪ the snapshot.

        A fresh set each call, so a caller (including the SDK, which takes this
        as `revoked_jtis`) cannot mutate our state by holding onto it.
        """
        if self.snapshot is None:
            return set(self.local)
        return self.local | set(self.snapshot.entries)

    def is_revoked(self, jti: str) -> bool:
        if jti in self.local:
            return True
        return self.snapshot is not None and jti in self.snapshot.entries

    def is_locally_revoked(self, jti: str) -> bool:
        """Whether the deny came from us rather than the control plane.

        Worth reporting separately: "the issuer revoked this" and "the CP says
        someone revoked this" are different facts for whoever reads the 403.
        """
        return jti in self.local

    # ── introspection ────────────────────────────────────────────────────
    @property
    def snapshot_entry_count(self) -> int:
        return 0 if self.snapshot is None else len(self.snapshot.entries)

    def __len__(self) -> int:
        return len(self.effective_jtis)

    # ── freshness (Axis B) ───────────────────────────────────────────────
    #
    # Evaluation lives here, as a pure function of state + clock + policy, so
    # it can be tested without standing up a server. ENFORCEMENT lives in
    # aitp_server — this type never decides what a 403 looks like.
    #
    # Note what is NOT here: nothing about whether a snapshot was authentic.
    # That was settled at ingest and is not revisitable, which is what keeps
    # Axis A and Axis B from collapsing into one switch.

    def posture(
        self,
        *,
        can_verify: bool,
        max_staleness_secs: int,
        now: Optional[float] = None,
    ) -> Posture:
        """Can this agent currently make a revocation decision it trusts?

        `can_verify` is "is verification configured and possible at all" — a
        control plane URL **and** a pinned issuer AID **and** an SDK that can
        check a signature. Not "did the last fetch succeed".

        That distinction is load-bearing, and getting it wrong is worse than
        it looks. `unchecked` means verification was never configured;
        `degraded` means it was, and we currently have no fresh snapshot to
        consult. Collapsing them makes an agent that simply has no pinned AID
        indistinguishable from one whose control plane just went down — so
        `fail_closed` would reject all traffic on a deployment whose only sin
        is not having set a new environment variable yet. That is not
        secure-by-default, it is broken-by-default on upgrade, and the first
        person to hit it disables the mode.
        """
        if not can_verify:
            return "unchecked"
        if self.snapshot is None:
            # Never heard from the issuer. Distinct from an empty snapshot,
            # which IS an assertion ("nothing is revoked", §1.5).
            return "degraded"
        if self.snapshot.is_expired(now):
            return "degraded"
        if self.snapshot.age_secs(now) > max_staleness_secs:
            return "degraded"
        return "current"

    def degraded_reason(
        self,
        *,
        max_staleness_secs: int,
        now: Optional[float] = None,
    ) -> str:
        """Why the posture is degraded, for the 403 detail and telemetry.

        "We never reached the CP" and "the CP went quiet an hour ago" send an
        operator to different places.
        """
        if self.snapshot is None:
            return "no verified snapshot has ever been applied"
        if self.snapshot.is_expired(now):
            return "the last verified snapshot is past its signed expires_at"
        age = int(self.snapshot.age_secs(now))
        return (
            f"the last verified snapshot is {age}s old, over the "
            f"{max_staleness_secs}s staleness budget"
        )
