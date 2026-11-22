"""TikTok checking (check_tiktok_handle and its helpers). The source is
the creator embed page: plain HTML carrying the account's recent videos
as JSON, pinned videos first, with no flag saying which are pinned and no
timestamp field - the upload time is the top 32 bits of the video id.
"""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import check_posts
from check_posts import (
    check_tiktok_handle,
    load_platforms,
    parse_tiktok_embed,
    split_tiktok_pinned,
    tiktok_id_to_timestamp,
)

UTC = ZoneInfo("UTC")


def _video_id(day: date, hour: int = 12) -> str:
    return str(int(datetime(day.year, day.month, day.day, hour, tzinfo=UTC).timestamp()) << 32)


def _embed_html(handle: str, video_ids: list[str], **page_fields) -> str:
    page = {
        "userInfo": {"uniqueId": handle, "privateAccount": False, "avatarThumbUrl": "https://avatar/"},
        "videoList": [{"id": v, "privateItem": False} for v in video_ids],
        "isError": False,
        **page_fields,
    }
    state = {"source": {"data": {f"/embed/@{handle}": page, "strategy": {}}}}
    return f'<html><script id="__FRONTITY_CONNECT_STATE__" type="application/json">{json.dumps(state)}</script></html>'


class _FakeResponse:
    def __init__(self, html, status=200):
        self._html = html
        self.status = status

    def text(self):
        return self._html


class _FakePage:
    def __init__(self, html, status=200):
        outer = self

        class _Request:
            def get(self, url, headers=None, timeout=None):
                outer.requested = url
                return _FakeResponse(html, status)

        class _Context:
            request = _Request()

        self.context = _Context()


def _media(*days: date) -> list[dict]:
    return [{"taken_at": tiktok_id_to_timestamp(_video_id(d)), "code": _video_id(d)} for d in days]


def test_video_id_decodes_to_its_upload_time():
    """Real id from a live embed page, 2026-11-22."""
    ts = tiktok_id_to_timestamp("7708922383049985293")
    assert datetime.fromtimestamp(ts, UTC).isoformat() == "2026-11-16T23:56:00+00:00"


def test_parse_embed_returns_videos_with_permalinks():
    vid = _video_id(date(2026, 11, 16))
    media, avatar, error = parse_tiktok_embed(_embed_html("megh", [vid]), "megh")

    assert error is None
    assert avatar == "https://avatar/"
    assert media[0]["code"] == vid
    assert media[0]["permalink"] == f"https://www.tiktok.com/@megh/video/{vid}"


def test_parse_embed_reports_a_missing_account():
    html = _embed_html("nobody", [], isError=True, errorCode=10221)
    media, _, error = parse_tiktok_embed(html, "nobody")

    assert media is None
    assert "10221" in error


def test_parse_embed_reports_a_private_account():
    html = _embed_html("megh", [], userInfo={"uniqueId": "megh", "privateAccount": True})
    _, _, error = parse_tiktok_embed(html, "megh")

    assert error == check_posts.PRIVATE_OR_NO_POSTS_MESSAGE


def test_parse_embed_reports_a_page_with_no_data():
    _, _, error = parse_tiktok_embed("<html>captcha</html>", "megh")

    assert error == check_posts.TIKTOK_UNREADABLE_MESSAGE


def test_split_pinned_strips_out_of_order_leading_videos():
    """The live shape, 2026-11-22: two old pinned videos, then newest to oldest."""
    media = _media(date(2026, 10, 11), date(2026, 8, 24), date(2026, 11, 16), date(2026, 11, 5), date(2026, 11, 3))

    pinned, chronological = split_tiktok_pinned(media)

    assert len(pinned) == 2
    assert [m["code"] for m in chronological] == [m["code"] for m in media[2:]]


def test_split_pinned_leaves_an_already_ordered_list_alone():
    media = _media(date(2026, 11, 16), date(2026, 11, 5), date(2026, 11, 3))

    pinned, chronological = split_tiktok_pinned(media)

    assert pinned == []
    assert chronological == media


def test_check_tiktok_handle_buckets_posts_by_day(monkeypatch, conn):
    monkeypatch.setattr(check_posts, "TIKTOK_EMBED_FULL_PAGE", 0)
    window = [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
    html = _embed_html("megh", [_video_id(date(2026, 11, 5)), _video_id(date(2026, 11, 3))])
    page = _FakePage(html)

    results, error, was_blocked, avatar = check_tiktok_handle("megh", window, UTC, page, conn)

    assert error is None and was_blocked is False
    assert page.requested == "https://www.tiktok.com/embed/@megh"
    assert results["2026-11-03"]["posted"] is True
    assert results["2026-11-04"] == {"status": "ok", "posted": False}
    assert results["2026-11-05"]["permalink"].endswith(_video_id(date(2026, 11, 5)))
    assert avatar == "https://avatar/"


def test_old_pinned_video_does_not_stretch_coverage_back(monkeypatch, conn):
    """The days between an old pinned video and the oldest chronological
    one were never fetched - they must stay unknown, not "didn't post".
    The pinned video's own day is still a confirmed post."""
    monkeypatch.setattr(check_posts, "TIKTOK_EMBED_FULL_PAGE", 0)
    window = [date(2026, 11, d) for d in range(1, 8)]
    html = _embed_html("megh", [
        _video_id(date(2026, 11, 2)),  # pinned, old
        _video_id(date(2026, 11, 7)),
        _video_id(date(2026, 11, 6)),
    ])

    results, _, _, _ = check_tiktok_handle("megh", window, UTC, _FakePage(html), conn)

    assert results["2026-11-02"]["posted"] is True
    assert "2026-11-03" not in results and "2026-11-05" not in results
    assert results["2026-11-06"]["posted"] is True
    assert "2026-11-01" not in results


def test_a_short_list_is_the_whole_history(conn):
    """Fewer videos than a full embed page means there is nothing older:
    every elapsed window day is known, including ones before the first video."""
    window = [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]
    html = _embed_html("megh", [_video_id(date(2026, 11, 3))])

    results, _, _, _ = check_tiktok_handle("megh", window, UTC, _FakePage(html), conn)

    assert results["2026-11-01"] == {"status": "ok", "posted": False}
    assert results["2026-11-03"]["posted"] is True


def test_blocked_response_is_reported_as_blocked(conn):
    results, error, was_blocked, _ = check_tiktok_handle("megh", [date(2026, 11, 1)], UTC, _FakePage("", 429), conn)

    assert results is None
    assert was_blocked is True
    assert "429" in error


def test_unreadable_page_is_reported_as_blocked(conn):
    """No data script at all is what a bot wall looks like - worth a
    fresh proxy session, unlike a clean "account not found"."""
    _, error, was_blocked, _ = check_tiktok_handle("megh", [date(2026, 11, 1)], UTC, _FakePage("<html></html>"), conn)

    assert error == check_posts.TIKTOK_UNREADABLE_MESSAGE
    assert was_blocked is True


def test_load_platforms_reads_the_second_column(tmp_path):
    csv_path = tmp_path / "handles.csv"
    csv_path.write_text("ig_person\n@megh, TikTok\nother,instagram\n")

    assert load_platforms(csv_path) == {"megh": "tiktok"}
    assert check_posts.load_handles(csv_path) == ["ig_person", "megh", "other"]


def test_load_platforms_rejects_an_unknown_platform(tmp_path):
    csv_path = tmp_path / "handles.csv"
    csv_path.write_text("megh,tiktock\n")

    with pytest.raises(ValueError, match="tiktock"):
        load_platforms(csv_path)
