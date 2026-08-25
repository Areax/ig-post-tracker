"""Decides whether the scheduled daily-check workflow should actually run.

Extracted out of .github/workflows/daily-check.yml (rather than left as
an inline heredoc) specifically so it's unit-testable - see
scripts/tests/test_ci_guard.py. The previous inline version's bug (a tight
time-of-day tolerance window that both of the day's cron firings landed
outside of, so the real check silently never ran at all - see git history)
was hard to catch exactly because it lived only in YAML with no tests.

GitHub's cron scheduler is best-effort and commonly late by anywhere from
minutes to hours (see README's "3. Changing the schedule" and the
workflow file's own comment for specifics), so gating on a narrow time
window is fragile by construction. Instead: only skip a *scheduled*
firing if today's local date has already had a successful run, per
docs/data/history.json's own `updated_at` timestamp. A late firing still
runs; the second of the day's two DST-safety cron entries just no-ops
once the first one succeeds. workflow_dispatch (a human explicitly
asking for a run) always runs regardless.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def last_successful_run_date(history_path: Path, tz: ZoneInfo) -> date | None:
    """The local calendar date of history.json's own `updated_at`, or None
    if the file doesn't exist yet or has no `updated_at`."""
    if not history_path.exists():
        return None
    data = json.loads(history_path.read_text())
    updated_at = data.get("updated_at")
    if not updated_at:
        return None
    return datetime.fromisoformat(updated_at).astimezone(tz).date()


def should_run(event_name: str, history_path: Path, tz: ZoneInfo, today: date) -> bool:
    if event_name != "schedule":
        return True  # a human asked for this explicitly - never skip it
    return last_successful_run_date(history_path, tz) != today


def main() -> None:
    import os

    event_name = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    tz = ZoneInfo(os.environ.get("TZ_NAME", "America/Los_Angeles"))
    history_path = Path(os.environ.get("HISTORY_FILE", "docs/data/history.json"))
    today = datetime.now(tz).date()

    result = should_run(event_name, history_path, tz, today)
    print(
        f"event={event_name}, today (local) is {today.isoformat()}, "
        f"last successful run was {last_successful_run_date(history_path, tz)}",
        file=sys.stderr,
    )
    print("true" if result else "false")


if __name__ == "__main__":
    main()
