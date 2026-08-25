"""ci_guard decides whether the scheduled workflow actually does
anything. Its predecessor (an inline YAML heredoc gating on a narrow
time-of-day window) had zero tests and, in production, silently rejected
every single scheduled firing since the repo went live - nobody noticed
until a day's data was visibly missing from the live site. These tests
exist so that never happens invisibly again.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from ci_guard import last_successful_run_date, should_run

UTC = ZoneInfo("UTC")


def _write_history(path, updated_at: str | None):
    import json

    path.write_text(json.dumps({"updated_at": updated_at} if updated_at else {}))


def test_workflow_dispatch_always_runs_regardless_of_history(tmp_path):
    history = tmp_path / "history.json"
    _write_history(history, "2026-08-25T00:35:00+00:00")

    assert should_run("workflow_dispatch", history, UTC, today=date(2026, 8, 25)) is True


def test_schedule_skips_when_already_run_today(tmp_path):
    history = tmp_path / "history.json"
    _write_history(history, "2026-08-25T00:35:00+00:00")

    assert should_run("schedule", history, UTC, today=date(2026, 8, 25)) is False


def test_schedule_runs_when_last_run_was_a_prior_day(tmp_path):
    """The actual production bug this replaces: a late-firing cron must
    still run as long as it hasn't already succeeded *today*, no matter
    how late in the day it fires."""
    history = tmp_path / "history.json"
    _write_history(history, "2026-08-24T23:59:00+00:00")

    assert should_run("schedule", history, UTC, today=date(2026, 8, 25)) is True


def test_schedule_runs_when_history_file_does_not_exist_yet(tmp_path):
    history = tmp_path / "does_not_exist.json"

    assert should_run("schedule", history, UTC, today=date(2026, 8, 25)) is True


def test_schedule_runs_when_history_file_has_no_updated_at(tmp_path):
    history = tmp_path / "history.json"
    _write_history(history, None)

    assert should_run("schedule", history, UTC, today=date(2026, 8, 25)) is True


def test_last_successful_run_date_converts_to_the_given_timezone(tmp_path):
    """A run at 23:00 Pacific is still 'today' Pacific even though it's
    already tomorrow in UTC - the date comparison must happen in the
    tracker's own timezone, not the server's."""
    history = tmp_path / "history.json"
    la = ZoneInfo("America/Los_Angeles")
    _write_history(history, "2026-08-25T23:00:00-07:00")  # 2026-08-26T06:00:00 UTC

    assert last_successful_run_date(history, la) == date(2026, 8, 25)
    assert last_successful_run_date(history, UTC) == date(2026, 8, 26)


def test_a_late_second_daily_firing_no_ops_after_the_first_succeeds(tmp_path):
    """Simulates the real dual-cron scenario: both 07:30 and 08:30 UTC
    entries fire the same day: the first one runs and updates history.json,
    the second must see that and skip."""
    history = tmp_path / "history.json"
    tz = ZoneInfo("America/Los_Angeles")
    today = date(2026, 8, 25)

    # Before the first firing: nothing has run today yet.
    assert should_run("schedule", history, tz, today) is True

    # First firing succeeds and updates history.json.
    _write_history(history, datetime(2026, 8, 25, 1, 4, 29, tzinfo=tz).isoformat())

    # Second firing, later the same local day: must not run again.
    assert should_run("schedule", history, tz, today) is False
