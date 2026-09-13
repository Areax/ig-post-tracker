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
