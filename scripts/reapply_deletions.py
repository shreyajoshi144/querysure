"""Re-apply account deletions after restoring a database backup (a restored dump resurrects accounts deleted since it was taken).

    python scripts/reapply_deletions.py refs.txt [--dry-run]

``refs.txt`` holds one ``user_ref`` per line, collected from the ``account_deleted`` log events emitted when each account was
deleted (field ``user_ref``; it is an HMAC of the user id under LOG_HASH_KEY, so the SAME LOG_HASH_KEY must be configured as
when the logs were written). Every restored user whose ref matches is deleted with the normal deletion path (objects first,
then rows). Accounts that cannot be deleted right now (live subscription, running jobs, storage error) are reported, not
skipped silently. Exit code 1 if any matched account could not be deleted.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import account_service  # noqa: E402
from db import SessionLocal, User  # noqa: E402
from observability.context import ref  # noqa: E402


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    dry = "--dry-run" in argv
    if len(args) != 1:
        print(__doc__)
        return 2
    with open(args[0], "r", encoding="utf-8") as fh:
        wanted = {line.strip() for line in fh if line.strip()}
    db = SessionLocal()
    matched = deleted = failed = 0
    try:
        for user_id, in db.query(User.id).all():
            if ref(user_id) not in wanted:
                continue
            matched += 1
            if dry:
                print(f"would delete {ref(user_id)}")
                continue
            try:
                account_service.delete_account(db, db.get(User, user_id))
                deleted += 1
                print(f"deleted {ref(user_id)}")
            except account_service.AccountError as exc:
                failed += 1
                db.rollback()
                print(f"FAILED {ref(user_id)}: {exc.code}")
    finally:
        db.close()
    print(f"matched={matched} deleted={deleted} failed={failed} dry_run={dry}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
