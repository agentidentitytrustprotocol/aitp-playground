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
from typing import Iterable, Optional


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
