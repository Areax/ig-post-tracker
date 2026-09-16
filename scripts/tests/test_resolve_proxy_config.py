"""resolve_proxy_config: builds Playwright's proxy dict from
PROXY_SERVER/USERNAME/PASSWORD, or returns None (direct connection) when
they're not all set. All-or-nothing on purpose - see the function's own
docstring for why a partial config isn't guessed at.
"""
import check_posts


def test_no_env_vars_set_returns_none(monkeypatch):
    monkeypatch.setattr(check_posts, "PROXY_SERVER", None)
    monkeypatch.setattr(check_posts, "PROXY_USERNAME", None)
    monkeypatch.setattr(check_posts, "PROXY_PASSWORD", None)

    assert check_posts.resolve_proxy_config() is None


def test_all_three_set_returns_playwright_proxy_dict(monkeypatch):
    monkeypatch.setattr(check_posts, "PROXY_SERVER", "http://geo.iproyal.com:12321")
    monkeypatch.setattr(check_posts, "PROXY_USERNAME", "user-session-abc123")
    monkeypatch.setattr(check_posts, "PROXY_PASSWORD", "hunter2")

    assert check_posts.resolve_proxy_config() == {
        "server": "http://geo.iproyal.com:12321",
        "username": "user-session-abc123",
        "password": "hunter2",
    }


def test_partial_config_is_treated_as_not_configured(monkeypatch):
    """A server with no credentials (or vice versa) is almost certainly a
    mistake, not an intentional unauthenticated-proxy setup - must not be
    silently half-applied."""
    monkeypatch.setattr(check_posts, "PROXY_SERVER", "http://geo.iproyal.com:12321")
    monkeypatch.setattr(check_posts, "PROXY_USERNAME", None)
    monkeypatch.setattr(check_posts, "PROXY_PASSWORD", None)

    assert check_posts.resolve_proxy_config() is None


def test_session_id_appends_iproyal_rotation_suffix(monkeypatch):
    """A session_id must resolve to IPRoyal's own
    `_session-<id>_lifetime-<minutes>m` PASSWORD suffix, so a fresh id
    gets a fresh (confirmed live, 2026-09-16: always different) exit IP -
    see resolve_proxy_config's own docstring for why it's the password
    and not the username (the latter makes Chromium's proxy auth fail
    outright), and PROXY_ROTATE_EVERY's comment for why this replaced
    one sticky session per run."""
    monkeypatch.setattr(check_posts, "PROXY_SERVER", "http://geo.iproyal.com:12321")
    monkeypatch.setattr(check_posts, "PROXY_USERNAME", "user123")
    monkeypatch.setattr(check_posts, "PROXY_PASSWORD", "hunter2")
    monkeypatch.setattr(check_posts, "PROXY_SESSION_LIFETIME_MINUTES", 10)

    assert check_posts.resolve_proxy_config("ab12cd34") == {
        "server": "http://geo.iproyal.com:12321",
        "username": "user123",
        "password": "hunter2_session-ab12cd34_lifetime-10m",
    }


def test_no_session_id_leaves_password_unchanged(monkeypatch):
    monkeypatch.setattr(check_posts, "PROXY_SERVER", "http://geo.iproyal.com:12321")
    monkeypatch.setattr(check_posts, "PROXY_USERNAME", "user123")
    monkeypatch.setattr(check_posts, "PROXY_PASSWORD", "hunter2")

    assert check_posts.resolve_proxy_config()["password"] == "hunter2"


def test_session_id_still_requires_the_full_base_config(monkeypatch):
    monkeypatch.setattr(check_posts, "PROXY_SERVER", None)
    monkeypatch.setattr(check_posts, "PROXY_USERNAME", None)
    monkeypatch.setattr(check_posts, "PROXY_PASSWORD", None)

    assert check_posts.resolve_proxy_config("ab12cd34") is None
