"""resolve_authenticated_state: parses a real logged-in account's
Playwright storage_state from IG_SESSION_STATE_JSON, or returns None
(stay anonymous) when it's unset or malformed. See that env var's own
comment in check_posts.py for what this unlocks and why - confirmed,
three independent ways, that anonymous access is capped at each
account's ~12 most recent posts, with no client-side fix available.
"""
import json

import check_posts


def test_unset_returns_none(monkeypatch):
    monkeypatch.setattr(check_posts, "IG_SESSION_STATE_JSON", None)

    assert check_posts.resolve_authenticated_state() is None


def test_valid_storage_state_json_is_parsed(monkeypatch):
    state = {"cookies": [{"name": "sessionid", "value": "abc123"}], "origins": []}
    monkeypatch.setattr(check_posts, "IG_SESSION_STATE_JSON", json.dumps(state))

    assert check_posts.resolve_authenticated_state() == state


def test_malformed_json_is_treated_as_not_configured(monkeypatch):
    monkeypatch.setattr(check_posts, "IG_SESSION_STATE_JSON", "{not valid json")

    assert check_posts.resolve_authenticated_state() is None


def test_valid_json_that_is_not_an_object_is_treated_as_not_configured(monkeypatch):
    """A storage_state is always a JSON object - a bare list or string
    here is a clear misconfiguration, not something to guess at."""
    monkeypatch.setattr(check_posts, "IG_SESSION_STATE_JSON", json.dumps(["not", "an", "object"]))

    assert check_posts.resolve_authenticated_state() is None
