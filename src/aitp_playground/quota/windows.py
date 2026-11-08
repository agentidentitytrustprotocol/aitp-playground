"""The counters one LLM charge touches, and when each one resets.

Four counters gate real LLM work: global per UTC day and per UTC month, and
per subject IP per UTC day and per UTC month. Windows are UTC calendar
windows (not rolling), so a counter's row key is ``(scope, subject,
window_start)`` and its reset is the start of the next window.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

#: Subject of the global counters.
GLOBAL_SUBJECT = "*"


@dataclass(frozen=True)
class QuotaLimits:
    per_ip_per_day: int
    per_ip_per_month: int
    global_per_day: int
    global_per_month: int

    @classmethod
    def from_settings(cls, settings: object) -> QuotaLimits:
        return cls(
            per_ip_per_day=settings.llm_quota_per_ip_per_day,  # type: ignore[attr-defined]
            per_ip_per_month=settings.llm_quota_per_ip_per_month,  # type: ignore[attr-defined]
            global_per_day=settings.llm_quota_global_per_day,  # type: ignore[attr-defined]
            global_per_month=settings.llm_quota_global_per_month,  # type: ignore[attr-defined]
        )


@dataclass(frozen=True)
class Counter:
    quota: str  # global_daily | global_monthly | per_ip_daily | per_ip_monthly
    scope: str  # stored key: global_day | global_month | ip_day | ip_month
    subject: str
    window_start: date
    limit: int
    resets_at: datetime

    @property
    def key(self) -> tuple[str, str, date]:
        return (self.scope, self.subject, self.window_start)


def next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def counters_for(subject_ip: str | None, limits: QuotaLimits, now: datetime) -> list[Counter]:
    """The counters one charge touches, in a FIXED order.

    Postgres row-locks the counters in this order, so concurrent charges
    serialise per row and can never deadlock. ``subject_ip=None`` charges the
    global counters only.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(UTC).date()
    month = today.replace(day=1)
    day_reset = datetime.combine(today + timedelta(days=1), time(), UTC)
    month_reset = datetime.combine(next_month(month), time(), UTC)
    out = [
        Counter("global_daily", "global_day", GLOBAL_SUBJECT, today,
                limits.global_per_day, day_reset),
        Counter("global_monthly", "global_month", GLOBAL_SUBJECT, month,
                limits.global_per_month, month_reset),
    ]
    if subject_ip is not None:
        out += [
            Counter("per_ip_daily", "ip_day", subject_ip, today,
                    limits.per_ip_per_day, day_reset),
            Counter("per_ip_monthly", "ip_month", subject_ip, month,
                    limits.per_ip_per_month, month_reset),
        ]
    return out


def prune_cutoff(now: datetime) -> date:
    """Rows whose window started before this are dead: 62 days before this month."""
    return now.astimezone(UTC).date().replace(day=1) - timedelta(days=62)
