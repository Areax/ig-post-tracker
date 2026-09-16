"""random_session_id: IPRoyal requires the `_session-<id>` suffix's id to
be exactly 8 alphanumeric characters - see resolve_proxy_config.
"""
import string

import check_posts


def test_returns_eight_characters():
    assert len(check_posts.random_session_id()) == 8


def test_only_lowercase_alphanumeric():
    allowed = set(string.ascii_lowercase + string.digits)
    session_id = check_posts.random_session_id()
    assert set(session_id) <= allowed


def test_calls_are_not_all_identical():
    ids = {check_posts.random_session_id() for _ in range(20)}
    assert len(ids) > 1
