"""AI Usage Limits
Caps how often Gemini runs, so one person (or a stuck loop) can't use up the provider quota or run up cost.

Every Gemini call that is actually made is recorded in ai_usage_events; reused (cached) results are free.
Limits are rolling windows, configurable per environment:

    AI_LIMIT_<FEATURE>_PER_USER_DAY        per person, last 24 hours
    AI_LIMIT_<FEATURE>_GLOBAL_DAY          whole company, last 24 hours, keeps us under the provider's daily quota
    AI_LIMIT_PER_USER_MINUTE               burst limit per person across all AI features
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Literal, Optional
from uuid import UUID

from fastapi import HTTPException

from postgresql_db.database import get_pool

Feature = Literal["CHAT"]
FEATURE_NAMES: Dict[str, str] = {
    "CHAT": "assistant chat replies",
}

# Defaults sized for the free Gemini tier (20 requests per model per day); raise them with a paid key.
_DEFAULTS = {
    "CHAT": {"PER_USER_DAY": 10, "GLOBAL_DAY": 20},
}
DAY = timedelta(hours=24)
MINUTE = timedelta(minutes=1)


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, default)))
    except ValueError:
        return default


def limit(feature: Feature, scope: str) -> int:
    return _env_int(f"AI_LIMIT_{feature}_{scope}", _DEFAULTS[feature][scope])


def per_user_minute() -> int:
    return _env_int("AI_LIMIT_PER_USER_MINUTE", 3)


@dataclass
class Usage:
    used: int
    limit: int
    resets_at: Optional[datetime]  # when the oldest call in the window drops out


class AiLimitExceeded(HTTPException):
    def __init__(self, message: str):
        super().__init__(status_code=429, detail=message)


def _when(resets_at: Optional[datetime]) -> str:
    if not resets_at:
        return "later"
    minutes = max(1, int((resets_at - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
    if minutes < 60:
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(minutes / 60)
    return f"in about {hours} hour{'s' if hours != 1 else ''}"


async def _count(conn, where: str, args: list, window: timedelta) -> Usage:
    row = await conn.fetchrow(
        f"""
        SELECT count(*) AS used, min(created_at) AS oldest
        FROM ai_usage_events
        WHERE created_at > NOW() - $1::interval AND {where}
        """,
        window,
        *args,
    )
    return Usage(int(row["used"]), 0, row["oldest"] + window if row["oldest"] else None)


async def reserve(feature: Feature, user_id: Optional[UUID]) -> None:
    """Record one Gemini call, or raise AiLimitExceeded (HTTP 429) with a message saying when it frees up.
    A transaction-level lock makes the check-and-record atomic, so parallel clicks can't slip past a limit."""
    name = FEATURE_NAMES[feature]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('ai_usage_events'))")

            cap = limit(feature, "GLOBAL_DAY")
            if cap:
                u = await _count(conn, "feature = $2", [feature], DAY)
                if u.used >= cap:
                    raise AiLimitExceeded(
                        f"The company-wide limit of {cap} {name} per day has been reached. It frees up {_when(u.resets_at)}."
                    )
            if user_id:
                burst = per_user_minute()
                if burst:
                    u = await _count(conn, "user_id = $2", [user_id], MINUTE)
                    if u.used >= burst:
                        raise AiLimitExceeded(f"Too many AI requests in a row. Try again {_when(u.resets_at)}.")
                cap = limit(feature, "PER_USER_DAY")
                if cap:
                    u = await _count(conn, "feature = $2 AND user_id = $3", [feature, user_id], DAY)
                    if u.used >= cap:
                        raise AiLimitExceeded(
                            f"You've used your {cap} {name} for the last 24 hours. More are available {_when(u.resets_at)}."
                        )

            await conn.execute(
                "INSERT INTO ai_usage_events (feature, user_id) VALUES ($1, $2)",
                feature, user_id,
            )


async def usage_for(user_id: UUID) -> Dict[str, dict]:
    """What a person has left, per feature, for showing in the UI."""
    out: Dict[str, dict] = {}
    pool = get_pool()
    async with pool.acquire() as conn:
        for feature in FEATURE_NAMES:
            mine = await _count(conn, "feature = $2 AND user_id = $3", [feature, user_id], DAY)
            everyone = await _count(conn, "feature = $2", [feature], DAY)
            user_cap, global_cap = limit(feature, "PER_USER_DAY"), limit(feature, "GLOBAL_DAY")
            remaining = [c - u for c, u in ((user_cap, mine.used), (global_cap, everyone.used)) if c]
            out[feature] = {
                "used": mine.used,
                "limit": user_cap,
                "company_used": everyone.used,
                "company_limit": global_cap,
                "resets_at": mine.resets_at,
                "company_resets_at": everyone.resets_at,
                "remaining": max(0, min(remaining)) if remaining else None,
            }
    return out
