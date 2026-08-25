"""extract_embedded_timeline is the third-tier fallback for accounts
where web_profile_info fails outright (e.g. bry.trieu's persistent
Instagram-side "laser.provider" schema error) - it recovers real post
data from the profile page's own embedded Relay preload JSON instead.
Verified empirically against known-good taken_at timestamps for two real
accounts before being implemented - see git history for the actual
cross-check. These tests lock in that the parsing itself is correct and
doesn't crash on the inputs Instagram's markup can actually throw at it.
"""
from __future__ import annotations

import json

from check_posts import _find_key, extract_embedded_timeline
from playwright.sync_api import Error as PlaywrightError


class _FakePage:
    def __init__(self, blocks: list[str], raise_error: bool = False):
        self._blocks = blocks
        self._raise_error = raise_error

    def eval_on_selector_all(self, selector, js):
        if self._raise_error:
            raise PlaywrightError("navigation context was destroyed")
        return self._blocks


def _timeline_block(edges: list[dict]) -> str:
    """Wraps edges in a minimal shape matching Instagram's real nesting -
    the exact wrapper structure doesn't matter to _find_key, only that
    `polaris_ordered_timeline_connection` appears somewhere inside."""
    return json.dumps({
        "require": [[
            "irrelevant", "wrapper", [], [{
                "__bbox": {
                    "result": {
                        "data": {
                            "xig_user_by_username": {
                                "polaris_ordered_timeline_connection": {"edges": edges}
                            }
                        }
                    }
                }
            }]
        ]]
    })


def _node(code: str, caption: str, user_pk: str = "663398771", media_type: int = 1, product_type: str = "feed"):
    return {
        "node": {
            "code": code,
            "accessibility_caption": caption,
            "media_type": media_type,
            "product_type": product_type,
            "user": {"pk": user_pk},
        }
    }


def test_recovers_user_id_and_dated_posts_from_a_real_shaped_block():
    edges = [
        _node("DcC_DnXlG_9", "Photo by Bryant Trieu | Men’s Performance Coach on August 14, 2026."),
        _node("Db_V0gAG228", "Photo by Bryant Trieu on August 13, 2026."),
    ]
    page = _FakePage([_timeline_block(edges)])

    user_id, media = extract_embedded_timeline(page)

    assert user_id == "663398771"
    assert len(media) == 2
    assert media[0]["code"] == "DcC_DnXlG_9"
    assert media[1]["code"] == "Db_V0gAG228"


def test_posts_with_unparseable_captions_are_skipped_not_guessed():
    edges = [
        _node("good_post", "Photo by Someone on August 14, 2026."),
        _node("no_date_at_all", "Photo by Someone with no date info"),
        _node("weird_format", "Photo taken 08/14/2026"),  # not the "on Month Day, Year." shape
    ]
    page = _FakePage([_timeline_block(edges)])

    user_id, media = extract_embedded_timeline(page)

    assert [m["code"] for m in media] == ["good_post"]


def test_noisy_trailing_ocr_text_does_not_confuse_the_date_pattern():
    """Real captions can have extra auto-generated alt-text appended after
    the date, including its own stray "on" occurrences - the first match
    (immediately after "by X") must win, not a later false one."""
    caption = (
        "Video by Caroline Chuang on August 19, 2026. May be an image of "
        "one or more people and text that says 'Going on a date on your "
        "Goingonadateonyour3 30s 12:15 12:15PM PM II -'."
    )
    edges = [_node("DcQI56RRtyu", caption)]
    page = _FakePage([_timeline_block(edges)])

    _, media = extract_embedded_timeline(page)

    assert len(media) == 1
    # August 19, 2026 noon Pacific
    from datetime import datetime
    from zoneinfo import ZoneInfo
    expected = int(datetime(2026, 8, 19, 12, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp())
    assert media[0]["taken_at"] == expected


def test_edges_without_a_code_are_skipped():
    edges = [{"node": {"accessibility_caption": "on August 14, 2026."}}]  # no "code" key
    page = _FakePage([_timeline_block(edges)])

    user_id, media = extract_embedded_timeline(page)

    assert media == []


def test_no_matching_block_returns_none_and_empty_list():
    page = _FakePage(["{}", '{"unrelated": "data"}'])

    user_id, media = extract_embedded_timeline(page)

    assert user_id is None
    assert media == []


def test_malformed_json_in_a_matching_block_is_skipped_gracefully():
    page = _FakePage(["not valid json but mentions polaris_ordered_timeline_connection anyway"])

    user_id, media = extract_embedded_timeline(page)

    assert user_id is None
    assert media == []


def test_finds_the_right_block_among_many_unrelated_ones():
    edges = [_node("real_post", "on August 14, 2026.")]
    blocks = ["{}", '{"other": "stuff"}', _timeline_block(edges), '{"more": "noise"}']
    page = _FakePage(blocks)

    user_id, media = extract_embedded_timeline(page)

    assert user_id == "663398771"
    assert len(media) == 1


def test_playwright_error_during_extraction_returns_none_and_empty_list():
    page = _FakePage([], raise_error=True)

    user_id, media = extract_embedded_timeline(page)

    assert user_id is None
    assert media == []


def test_find_key_locates_a_deeply_nested_key():
    obj = {"a": {"b": [1, 2, {"c": {"target": "found it"}}]}}
    assert _find_key(obj, "target") == "found it"


def test_find_key_returns_none_when_absent():
    obj = {"a": {"b": [1, 2, 3]}}
    assert _find_key(obj, "nonexistent") is None
