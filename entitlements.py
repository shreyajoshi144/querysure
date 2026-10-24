"""Centralized QuerySure plan and daily query entitlements."""

from __future__ import annotations

PLAN_CONFIG = {
    "free": {"name": "Free", "price_monthly_usd": 0, "daily_query_limit": 10},
    "pro": {"name": "Pro", "price_monthly_usd": 5, "daily_query_limit": 50},
    "team": {"name": "Team", "price_monthly_usd": 15, "daily_query_limit": 100},
    "business": {"name": "Business", "price_monthly_usd": None, "daily_query_limit": None},
}

DEFAULT_PLAN = "free"


def set_effective_plan(user, plan: str | None) -> str:
    """Set the server-owned effective plan after trusted business logic.

    This is the only plan transition helper. Client requests must not call it,
    and billing state must reach it only through a verified billing service.
    """
    normalized = (plan or DEFAULT_PLAN).lower()
    if normalized not in PLAN_CONFIG:
        raise ValueError("Unknown plan")
    user.plan = normalized
    return normalized


def get_plan(plan: str | None) -> dict:
    return PLAN_CONFIG.get((plan or DEFAULT_PLAN).lower(), PLAN_CONFIG[DEFAULT_PLAN])


def daily_limit(plan: str | None) -> int | None:
    return get_plan(plan)["daily_query_limit"]
