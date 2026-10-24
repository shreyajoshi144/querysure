"""Periodic maintenance (run from cron / a scheduled job, e.g. every 15-60 minutes). Idempotent and safe to run concurrently.

    python scripts/maintenance.py

* Downgrades paid accounts whose billing period (+ BILLING_GRACE_HOURS) ended with no renewal event (fail closed).
* Expires stale pending checkout sessions.
* Purges auth sessions that expired / were revoked more than a day ago.

Prints one JSON line with the counts. Exit code 1 on error.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import purge_dead_sessions  # noqa: E402
from billing_service import reconcile_lapsed_subscriptions  # noqa: E402
from db import CheckoutSession, SessionLocal  # noqa: E402


def run() -> dict:
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        downgraded = reconcile_lapsed_subscriptions(db, now=now)
        expired = db.query(CheckoutSession).filter(
            CheckoutSession.status == "pending", CheckoutSession.expires_at <= now
        ).update({CheckoutSession.status: "expired"}, synchronize_session=False)
        db.commit()
        purged = purge_dead_sessions(db, now=now)
        return {"downgraded_subscriptions": downgraded, "expired_checkouts": int(expired or 0), "purged_sessions": purged}
    finally:
        db.close()


if __name__ == "__main__":
    try:
        print(json.dumps(run()))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": type(exc).__name__}))
        sys.exit(1)
