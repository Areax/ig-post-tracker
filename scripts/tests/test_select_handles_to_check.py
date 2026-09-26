"""select_handles_to_check: narrows a full run down to a targeted retry
(TARGET_HANDLES or RETRY_FAILING_ONLY), so fixing a handful of currently-
failing handles doesn't re-burn proxy bandwidth re-checking everyone who
already succeeded. Neither env var set is the normal, full-run case.
"""
from datetime import date

import check_posts
import db


def test_neither_env_var_set_returns_all_handles(monkeypatch, conn):
    monkeypatch.setattr(check_posts, "TARGET_HANDLES", None)
    monkeypatch.setattr(check_posts, "RETRY_FAILING_ONLY", False)

    result = check_posts.select_handles_to_check(["a", "b", "c"], conn, [date(2026, 8, 17)])

    assert result == ["a", "b", "c"]


def test_target_handles_filters_to_the_named_subset(monkeypatch, conn):
    monkeypatch.setattr(check_posts, "TARGET_HANDLES", "b,c")
    monkeypatch.setattr(check_posts, "RETRY_FAILING_ONLY", False)

    result = check_posts.select_handles_to_check(["a", "b", "c"], conn, [date(2026, 8, 17)])

    assert result == ["b", "c"]


def test_target_handles_strips_at_signs_and_whitespace(monkeypatch, conn):
    monkeypatch.setattr(check_posts, "TARGET_HANDLES", " @a , b ")
    monkeypatch.setattr(check_posts, "RETRY_FAILING_ONLY", False)

    result = check_posts.select_handles_to_check(["a", "b", "c"], conn, [date(2026, 8, 17)])

    assert result == ["a", "b"]


def test_target_handles_preserves_all_handles_order_not_the_request_order(monkeypatch, conn):
    monkeypatch.setattr(check_posts, "TARGET_HANDLES", "c,a")
    monkeypatch.setattr(check_posts, "RETRY_FAILING_ONLY", False)

    result = check_posts.select_handles_to_check(["a", "b", "c"], conn, [date(2026, 8, 17)])

    assert result == ["a", "c"]


def test_target_handles_ignores_unknown_names_without_crashing(monkeypatch, conn, capsys):
    monkeypatch.setattr(check_posts, "TARGET_HANDLES", "a,not_tracked")
    monkeypatch.setattr(check_posts, "RETRY_FAILING_ONLY", False)

    result = check_posts.select_handles_to_check(["a", "b"], conn, [date(2026, 8, 17)])

    assert result == ["a"]
    assert "not_tracked" in capsys.readouterr().err


def test_target_handles_wins_over_retry_failing_only_when_both_set(monkeypatch, conn):
    monkeypatch.setattr(check_posts, "TARGET_HANDLES", "a")
    monkeypatch.setattr(check_posts, "RETRY_FAILING_ONLY", True)
    db.fill_gaps_with_error(conn, "b", [date(2026, 8, 17)], "boom", "t", today=date(2026, 8, 20))

    result = check_posts.select_handles_to_check(["a", "b"], conn, [date(2026, 8, 17)])

    assert result == ["a"]


def test_retry_failing_only_auto_detects_from_the_db(monkeypatch, conn):
    monkeypatch.setattr(check_posts, "TARGET_HANDLES", None)
    monkeypatch.setattr(check_posts, "RETRY_FAILING_ONLY", True)
    db.upsert_ok(conn, "healthy", {"2026-08-17": {"status": "ok", "posted": True}}, "t")
    db.fill_gaps_with_error(conn, "broken", [date(2026, 8, 17)], "boom", "t", today=date(2026, 8, 20))

    result = check_posts.select_handles_to_check(["healthy", "broken"], conn, [date(2026, 8, 17)])

    assert result == ["broken"]
