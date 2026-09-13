"""resolve_identity's private/no-visible-posts short-circuit.

IG's own `is_private` flag is the precise signal when present, but it
doesn't reliably show up even for accounts confirmed private by hand
(tested live against Kirossound, 2026-09-13: `is_private` absent, edges
and count both empty/missing). `total_post_count` alone isn't reliable
either - it came back None for a real, healthy public account
(torch_boy) the same day. The one signal that held up in practice: no
edges AND no count at all means nothing about the account's posts was
visible to us, `is_private` flag or not - resolve_identity treats that
combination the same as a confirmed private account instead of silently
returning "success" with nothing in it. goto_profile and fetch_in_page
are mocked - no Playwright/network involved.
"""
import check_posts

PRIVATE_MESSAGE = "account is private or has no visible posts"


def _mock_web_profile_info(monkeypatch, user):
    monkeypatch.setattr(check_posts, "goto_profile", lambda page, handle: None)
    monkeypatch.setattr(
        check_posts, "fetch_in_page",
        lambda page, url, params, headers: ({"data": {"user": user}}, None, False),
    )


def test_is_private_flag_short_circuits_with_a_clear_message(monkeypatch):
    _mock_web_profile_info(monkeypatch, {"id": "123", "is_private": True})

    user_id, avatar, media, error, was_blocked, total_post_count = check_posts.resolve_identity(
        "some_private_handle", page=None,
    )

    assert user_id is None
    assert media is None
    assert error == PRIVATE_MESSAGE
    assert was_blocked is False, "a private account is not an IG block"


def test_no_edges_and_no_count_is_flagged_even_without_the_is_private_flag(monkeypatch):
    """The Kirossound case: is_private absent/false, but the response
    still carries no evidence of any posts at all - flag it the same way
    as an explicit is_private:true rather than returning an empty
    'success'."""
    _mock_web_profile_info(monkeypatch, {"id": "123", "is_private": False})

    user_id, avatar, media, error, was_blocked, total_post_count = check_posts.resolve_identity(
        "some_handle", page=None,
    )

    assert user_id is None
    assert media is None
    assert error == PRIVATE_MESSAGE
    assert was_blocked is False


def test_public_account_is_unaffected(monkeypatch):
    _mock_web_profile_info(monkeypatch, {
        "id": "123",
        "is_private": False,
        "profile_pic_url": "https://avatar/",
        "edge_owner_to_timeline_media": {"count": 1, "edges": [
            {"node": {"taken_at_timestamp": 123, "shortcode": "abc"}},
        ]},
    })

    user_id, avatar, media, error, was_blocked, total_post_count = check_posts.resolve_identity(
        "some_public_handle", page=None,
    )

    assert user_id == "123"
    assert error is None
    assert total_post_count == 1
    assert media == [{"taken_at": 123, "code": "abc", "media_type": None, "product_type": None}]


def test_confirmed_zero_post_count_is_not_flagged_as_private(monkeypatch):
    """A real `count: 0` (the account's timeline metadata itself confirms
    zero posts, ever) is actual evidence, not an unknown - unlike a
    missing/None count, it must not be treated as private."""
    _mock_web_profile_info(monkeypatch, {
        "id": "123",
        "edge_owner_to_timeline_media": {"count": 0, "edges": []},
    })

    user_id, avatar, media, error, was_blocked, total_post_count = check_posts.resolve_identity(
        "some_handle", page=None,
    )

    assert user_id == "123"
    assert error is None
    assert total_post_count == 0
    assert media == []
