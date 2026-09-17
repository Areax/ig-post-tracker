"""is_proxy_connection_error: distinguishes a dead proxy tunnel (the
connection itself never got established) from any real HTTP response,
even a blocked one - see PROXY_CONNECTION_ERROR_CODES and its call site
in main() for why this triggers an immediate forced rotation instead of
being treated like any other per-handle error.
"""
import check_posts


def test_tunnel_connection_failed_is_a_proxy_error():
    error = (
        "navigation to profile page failed: Page.goto: "
        "net::ERR_TUNNEL_CONNECTION_FAILED at https://www.instagram.com/some_handle/"
    )
    assert check_posts.is_proxy_connection_error(error) is True


def test_various_known_connection_codes_are_recognized():
    for code in check_posts.PROXY_CONNECTION_ERROR_CODES:
        assert check_posts.is_proxy_connection_error(f"navigation to profile page failed: net::{code}")


def test_none_is_not_a_proxy_error():
    assert check_posts.is_proxy_connection_error(None) is False


def test_a_real_http_level_block_is_not_a_proxy_error():
    """A 401/429 proves the tunnel worked - Instagram responded. This
    must not trigger a forced rotation the same way a dead tunnel does;
    it's was_blocked/CONSECUTIVE_BLOCK_LIMIT's job instead."""
    assert check_posts.is_proxy_connection_error("rate limited (HTTP 401: Please wait a few minutes)") is False


def test_the_private_message_is_not_a_proxy_error():
    assert check_posts.is_proxy_connection_error(check_posts.PRIVATE_OR_NO_POSTS_MESSAGE) is False
