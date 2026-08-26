"""One-off, temporary migration: apply manually-verified corrections
directly to data/tracker.db (whichever copy DB_FILE points at - in CI,
the restored cache). Not part of the regular pipeline - meant to be run
once via a dedicated workflow_dispatch, then deleted along with that
workflow.

Why this is needed: db.upsert_ok's sticky-true protection (see its
docstring) stops a *future* downgrade, but doesn't retroactively fix a
day that's already sitting wrong in a given DB copy. Two consecutive
real CI runs both failed to capture these specific days (confirmed real
posts, exact permalinks/timestamps verified against each post's own
page - see git history), so waiting for a lucky future fetch isn't
reliable here. This migration seeds the correct value directly using the
same upsert_ok path a real successful check would use - once applied,
the sticky-true protection makes it permanent regardless of what any
future fetch does or doesn't find.
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "scripts")
import check_posts as cp
import db

CORRECTIONS = [
    ("bry.trieu", "2026-08-17", "https://www.instagram.com/p/DcKW_atmvZc/"),
    ("signed.angela", "2026-08-21", "https://www.instagram.com/p/DcUwvYJt7f7/"),
    ("signed.angela", "2026-08-23", "https://www.instagram.com/p/DcZ5Qavt0i6/"),
]


def main() -> None:
    tz = ZoneInfo(cp.TRACKER_TIMEZONE)
    checked_at = datetime.now(tz).isoformat()
    conn = db.connect(cp.DB_FILE)

    for handle, check_date, permalink in CORRECTIONS:
        before = conn.execute(
            "SELECT status, posted, permalink FROM checks WHERE handle = ? AND check_date = ?",
            (handle, check_date),
        ).fetchone()
        db.upsert_ok(conn, handle, {check_date: {"status": "ok", "posted": True, "permalink": permalink}}, checked_at)
        after = conn.execute(
            "SELECT status, posted, permalink FROM checks WHERE handle = ? AND check_date = ?",
            (handle, check_date),
        ).fetchone()
        print(f"{handle} {check_date}: before={before} after={after}")

    conn.close()


if __name__ == "__main__":
    main()
