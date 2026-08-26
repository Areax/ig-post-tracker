"""
Daily Instagram post checker (self-hosted, unauthenticated, real browser).

For every handle in handles.csv, navigates a real (headless) Chromium
browser to that handle's profile page, then fetches Instagram's internal
`web_profile_info` and `/api/v1/feed/user/<id>/` endpoints from *within*
that already-loaded page's own JS context (via page.evaluate + fetch) -
not as cold, standalone HTTP requests. Results are bucketed across every
day in the tracked window (HISTORY_DAYS days ending "yesterday" in
TRACKER_TIMEZONE) and written to docs/data/history.json for the static
tracker page.

WHY A REAL BROWSER: earlier versions used plain `requests`, then
`curl_cffi` (TLS/JA3 fingerprint spoofing) to work around a persistent
401 "please wait a few minutes" block. Both eventually failed even with
conservative pacing, persisted cookies, and realistic headers. The
breakthrough came from a simple real-world test: the same handle loaded
fine in the user's own browser - including in an *incognito* window,
ruling out "must be logged in" - while every anonymous HTTP-client
approach kept failing on the same IP. That isolates the difference to
actual JavaScript execution: curl_cffi can fake the TLS handshake and
headers, but it never runs a JS engine, so it can't satisfy any
client-side integrity/fingerprint check Instagram's page might run.
Confirmed empirically: a vanilla (no stealth patch needed) Playwright
Chromium browser, navigating to a currently-blocked handle's profile
page and then calling `fetch()` from inside that page, got web_profile_info
through when curl_cffi was failing on the identical handle/IP.

HONEST CAVEAT: this fixed web_profile_info, but NOT `/api/v1/feed/user/`
in the same test - that endpoint kept failing even from the real browser,
on two different handles including one barely touched by any of today's
testing. The working theory is that feed/user is separately throttled on
cumulative request volume (it's the endpoint called most often, 1-2x per
handle per run, across every experiment run today) - a real browser
doesn't fix a volume-based throttle, only a fingerprint/JS-based one.
Expect feed/user to keep failing for a while after a heavy testing
session even with this change; it should recover with time, independent
of which client makes the request.

SECOND HONEST CAVEAT, learned right after the first: it's tempting to
treat the numeric user id as a permanent, cacheable fact (it is) and skip
straight to `feed/user` on later runs once it's known. That was tried and
made things *worse* - it meant every run after the first hit `feed/user`
(the chronically-throttled endpoint) without ever calling the
reliable, real-browser-backed `web_profile_info` first, so a handle that
had worked cleanly the first time started failing on the very next run.
The fix: never skip `web_profile_info`. Every run resolves the handle
through it again (it's cheap and reliable), and its embedded post list
(~12 most recent posts, no pagination needed) is used directly as the
data source. For the "did they post in the last 24h" use case this is
completely sufficient - one page from a reliable endpoint beats several
pages from an unreliable one.

`feed/user` pagination is now purely additive, opt-in machinery for deep
backfills: it only runs when MAX_FEED_PAGES > 1, fetching extra pages on
top of (never instead of) the embedded posts already captured. Each page
returns ~12 posts regardless of the `count` query param requested
(confirmed empirically at count=24 and count=30, both returned exactly
12) - there's no way to get a bigger single page, only more pages. Pages
are NOT fetched in strict chronological order among themselves - Instagram
interleaves reels/clips non-chronologically with regular posts even
within one page (confirmed in production: a months-old reel appeared
mid-page alongside recent posts) - so pagination always uses the full
MAX_FEED_PAGES budget rather than stopping early based on the oldest item
seen in a page; only Instagram's own more_available/next_max_id absence is
trusted as a real "no more data" signal. If feed/user fails partway
through a backfill, that's treated as non-fatal: the embedded posts
already resolved are kept and the run continues, with a warning logged
rather than the whole handle being marked as errored. MAX_FEED_PAGES
defaults to 2 (good for backfills/manual runs starting from an empty
window); the scheduled daily workflow overrides it to 1, which means a
routine run touches `feed/user` not at all - only the reliable
`web_profile_info` call, once per handle.

The browser's full storage state (cookies + localStorage, not just a flat
cookie dict) persists across runs in the DB's `session_state` table,
reused on the next run instead of starting from a totally blank browser
profile every time.

Some accounts hit an Instagram-side bug where web_profile_info fails
outright (observed: a corrupted internal schema reference for that
specific account, unrelated to rate limiting - confirmed reproducible on
every attempt). For those, the numeric user id is instead scraped out of
the plain profile page fetched with a crawler user-agent via a plain HTTP
request (not the browser - this fallback never needed JS, just server-
rendered HTML), after which the same feed/user pagination is used.

A handle that errors this run never overwrites previously-good results for
days it already has data for - it only fills in gaps - so a transient
block or an Instagram-side error on one run doesn't erase history.

The same protection applies within a *successful* run: check_handle only
returns True/False for the specific dates it has real evidence for -
dates older than the oldest fetched post (unless Instagram's own
more_available/empty-page signal proves that IS the account's whole
history) are left out of the write entirely. This matters because a
routine MAX_FEED_PAGES=1 run only sees the ~12 most recent posts; for a
frequent poster that list can fail to reach back across the whole
HISTORY_DAYS window. Without this, a routine run would blindly write
"posted: false" for those older, un-fetched days and stomp a correct
"posted: true" that an earlier deeper backfill had already established -
this was observed in production (bry.trieu's history reverting after a
routine run) and is the reason this exists.

This is enforced in data/tracker.db (SQLite, the source of truth across runs);
docs/data/history.json is regenerated from it every run as a read-only
snapshot for the static site - see scripts/db.py.

This is unofficial and unsupported by Meta: no API key, no login, no
Facebook Page, no App Review. It can break if Instagram changes this
endpoint without notice, and is technically against Instagram's Terms of
Service (though a 2024 US court ruling found scraping logged-out public
data isn't a CFAA violation).

Pacing defaults to ~60-75s/request (~1/min), tightened from an earlier
30-40s default after a real block was hit at that pace too, on a fresh
IP, mid-run. Whether pacing matters at all once a real browser is in the
loop is untested - kept conservative pending evidence either way.

Optional env vars:
  TRACKER_TIMEZONE            - IANA tz name, default "America/Los_Angeles"
  TARGET_DATE                  - ISO date (YYYY-MM-DD) to check instead of "yesterday"
  WINDOW_START_DATE             - ISO date (YYYY-MM-DD): pin the HISTORY_DAYS window to start
                                  here instead of ending "yesterday" - e.g. to set up a clean
                                  reference window from a fixed date even if it's already
                                  partway elapsed, or extends into days that haven't happened
                                  yet (those are simply left with no data). Takes priority over
                                  TARGET_DATE if both are set.
  HANDLES_FILE                 - default "handles.csv"
  DB_FILE                      - default "data/tracker.db", the persistent store
  HISTORY_FILE                 - default "docs/data/history.json", generated snapshot
  HISTORY_DAYS                 - how many days the exported snapshot covers, default 14
  MIN_REQUEST_INTERVAL_SECONDS - default 60 (~1/min)
  REQUEST_JITTER_SECONDS       - default 15 (randomized on top of the min interval, so 60-75s)
  MAX_FEED_PAGES                - default 2 (~24 posts of coverage per handle, ~12/page). The
                                  scheduled daily workflow overrides this to 1 - see module docstring.
  COOLDOWN_SECONDS             - default 900 (15 min), only relevant if MAX_COOLDOWNS > 0.
  MAX_COOLDOWNS                - default 0: on a block, stop immediately - mark that handle as an
                                  error (the real message, visible in the UI) and the rest of the
                                  run as skipped, rather than waiting. Set > 0 to opt back into
                                  cooldown-and-resume (e.g. for an unattended backfill).
  HEADLESS                     - default "1" (headless browser). Set "0" to watch it run locally.
"""

from __future__ import annotations

import csv
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

import db

CRAWLER_UA = "facebookexternalhit/1.1"

PROFILE_INFO_ENDPOINT = "https://www.instagram.com/api/v1/users/web_profile_info/"
FEED_ENDPOINT_TEMPLATE = "https://www.instagram.com/api/v1/feed/user/{user_id}/"
PROFILE_INFO_HEADERS = {
    "X-IG-App-ID": "936619743392459",
    "X-Requested-With": "XMLHttpRequest",
    "X-Instagram-AJAX": "1",
    "Accept": "*/*",
}
FEED_HEADERS = {
    "X-IG-App-ID": "936619743392459",
    "Accept": "*/*",
}
USER_ID_PATTERN = re.compile(r'"profilePage_(\d+)"')

# The profile page's own server-rendered Relay/GraphQL preload data
# (see extract_embedded_timeline) - a completely different code path from
# the web_profile_info XHR call, useful when that call fails outright.
# Each post node's `accessibility_caption` ends with a human-readable
# date like "... on August 14, 2026." - empirically verified (cross-
# checked against known-good taken_at timestamps for the same exact
# posts, across two different accounts) to consistently be the calendar
# date in *America/Los_Angeles*, regardless of the viewer's own system
# timezone - almost certainly Instagram's own server default for
# anonymous/logged-out rendering (their infrastructure's home timezone),
# not something tied to the browser or to TRACKER_TIMEZONE. This only
# gives a calendar date, not a real time - fine for day-bucketing, and
# the only thing bucket_media_by_day needs, but the *timezone* dependency
# is real: if TRACKER_TIMEZONE is ever set to something other than
# America/Los_Angeles, dates recovered this way can be off by a day
# right around midnight Pacific, since the caption's date is generated in
# Pacific time no matter what TRACKER_TIMEZONE says.
ACCESSIBILITY_CAPTION_TZ = ZoneInfo("America/Los_Angeles")
ACCESSIBILITY_DATE_PATTERN = re.compile(r"on ([A-Za-z]+ \d{1,2}, \d{4})\.")
POST_CODE_PATTERN = re.compile(r"/(?:p|reel)/([^/?]+)")

# Handles whose numeric user id has been manually confirmed (e.g. via a
# local run's resolve_identity result) but whose *fresh* id-discovery is
# known to be unreliable from wherever this script actually runs in
# production - see check_handle's known-id fallback, used only after
# resolve_identity has already exhausted every live discovery method and
# still come up empty. A user id is effectively permanent for the life of
# an account, so this is safe to rely on once confirmed - but add an
# entry here only after confirming it yourself; a wrong value would
# silently pull another account's posts under this handle's name.
KNOWN_USER_IDS = {
    # web_profile_info is permanently broken for this specific account
    # (Instagram-side: "Asset asset://laser.provider/ig_business_category_
    # subvertical has been deleted"), and every one of resolve_identity's
    # live id-discovery fallbacks (crawler-UA HTML scrape, rendered DOM
    # grid, embedded Relay JSON) has been confirmed to fail specifically
    # from GitHub Actions - profile-page navigation gets redirected to a
    # login wall and rate-limited there (302 -> /accounts/login/ -> 429),
    # even though the exact same account resolves fine from a residential
    # connection. feed/user itself is unaffected by any of this: it's a
    # plain in-page fetch() call, the same mechanism the primary
    # web_profile_info call uses successfully for every other tracked
    # handle - it just needs a user id to call, which this supplies.
    "bry.trieu": "663398771",
}

FEED_ITEM_COUNT = 30
MAX_FEED_PAGES = int(os.environ.get("MAX_FEED_PAGES", "2"))
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

TRACKER_TIMEZONE = os.environ.get("TRACKER_TIMEZONE", "America/Los_Angeles")
HANDLES_FILE = Path(os.environ.get("HANDLES_FILE", "handles.csv"))
DB_FILE = Path(os.environ.get("DB_FILE", "data/tracker.db"))
HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", "docs/data/history.json"))
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "14"))

MIN_REQUEST_INTERVAL = float(os.environ.get("MIN_REQUEST_INTERVAL_SECONDS", "60"))
REQUEST_JITTER = float(os.environ.get("REQUEST_JITTER_SECONDS", "15"))
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 20
CONSECUTIVE_BLOCK_LIMIT = 1
RATE_LIMIT_STATUS_CODES = (429, 401)
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "900"))
MAX_COOLDOWNS = int(os.environ.get("MAX_COOLDOWNS", "0"))

DESKTOP_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"

# Runs inside the already-loaded page, so the fetch carries the page's own
# real cookies/session and the browser's own (unforgeable) Referer/Origin -
# we don't set those manually; JS can't override them via fetch() anyway
# (they're spec-forbidden headers), and the real ones are more authentic
# than anything we'd fake.
FETCH_JS = """
async ({ url, headers }) => {
    try {
        const res = await fetch(url, { headers, credentials: 'include' });
        const text = await res.text();
        return { ok: true, status: res.status, text };
    } catch (e) {
        return { ok: false, error: String(e) };
    }
}
"""


def load_handles(path: Path) -> list[str]:
    if not path.exists():
        print(f"handles file not found: {path}", file=sys.stderr)
        return []
    handles = []
    with path.open(newline="") as f:
        for row in csv.reader(f):
            if not row:
                continue
            handle = row[0].strip().lstrip("@")
            if not handle or handle.lower() == "handle":
                continue
            handles.append(handle)
    return handles


def resolve_window(tz: ZoneInfo) -> list[date]:
    """Oldest-to-newest list of dates the run should have results for.

    Normally a rolling HISTORY_DAYS-day window ending "yesterday" (or
    TARGET_DATE, if set). WINDOW_START_DATE instead pins the window to a
    fixed start, e.g. for setting up a clean HISTORY_DAYS-day reference
    window from a specific date even if some of it is already in the past
    - the days before "today" just won't have a check run for them, and
    today itself plus anything after it hasn't fully happened yet. Either
    way, check_handle never writes results for today or a later date -
    see bucket_media_by_day's own `today` guard.
    """
    start_override = os.environ.get("WINDOW_START_DATE")
    if start_override:
        start = date.fromisoformat(start_override)
        return [start + timedelta(days=i) for i in range(HISTORY_DAYS)]
    override = os.environ.get("TARGET_DATE")
    end = date.fromisoformat(override) if override else (datetime.now(tz) - timedelta(days=1)).date()
    return [end - timedelta(days=i) for i in range(HISTORY_DAYS - 1, -1, -1)]


def is_blocking_error(status_code: int) -> bool:
    return status_code in (401, 403, 429)


def csrf_header(page: Page) -> dict:
    for cookie in page.context.cookies():
        if cookie.get("name") == "csrftoken":
            return {"X-CSRFToken": cookie["value"]}
    return {}


def fetch_in_page(
    page: Page, url: str, params: dict, headers: dict, parse_json: bool = True
) -> tuple[dict | str | None, str | None, bool]:
    """Runs fetch() from inside the already-navigated page. Returns
    (body, error_message, was_blocked)."""
    full_url = f"{url}?{urlencode(params)}" if params else url
    all_headers = {**headers, **csrf_header(page)}

    last_error = "unknown error"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = page.evaluate(FETCH_JS, {"url": full_url, "headers": all_headers})
        except PlaywrightError as exc:
            last_error = str(exc)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if not result.get("ok"):
            last_error = result.get("error", "in-page fetch failed")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        status = result["status"]
        text = result["text"]

        if status == 200:
            if not parse_json:
                return text, None, False
            try:
                return json.loads(text), None, False
            except ValueError:
                return None, "200 response was not valid JSON", False

        if status in RATE_LIMIT_STATUS_CODES:
            # No short in-request backoff - a 401/429 here has, in
            # practice, meant a sustained block rather than something a
            # quick retry clears. Return immediately and let the caller's
            # cooldown handle it.
            try:
                body_message = json.loads(text).get("message")
            except ValueError:
                body_message = None
            last_error = f"rate limited (HTTP {status}" + (f": {body_message})" if body_message else ")")
            return None, last_error, True

        try:
            last_error = json.loads(text).get("message", f"HTTP {status}")
        except ValueError:
            last_error = f"HTTP {status}"

        if status in (500, 502, 503, 504) and attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        return None, last_error, is_blocking_error(status)

    return None, last_error, True


def goto_profile(page: Page, handle: str) -> str | None:
    """Navigates to the handle's profile page - a real page load with real
    JS execution, which is what actually made web_profile_info work in
    testing (vs. a cold API call with no page behind it). Returns an error
    message on failure, None on success."""
    url = f"https://www.instagram.com/{handle}/"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)  # let the page's own JS settle
        return None
    except PlaywrightError as exc:
        return str(exc)


def _find_key(obj, key: str):
    """Recursively searches a nested dict/list structure for the first
    occurrence of `key`, returning its value (or None if not found).
    Instagram's embedded Relay preload payloads bury the data several
    layers deep in a shape that shifts around; walking for the key by
    name is far less brittle than hardcoding the exact path."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def extract_grid_timeline(
    page: Page, max_attempts: int = 4, retry_interval_ms: int = 1500
) -> tuple[str | None, list[dict]]:
    """Recovers (user_id, media) from the *visible* rendered page - the
    same content a human looking at the profile actually sees - rather
    than from an internal data blob. Two independent pieces, both reused
    from the already-loaded page (see goto_profile), no extra request:

      - The numeric user id via USER_ID_PATTERN, searched against the
        page's own full HTML (page.content()) instead of a separate
        crawler-UA HTTP request - see resolve_identity's final fallback
        tier, which makes that separate request and can fail on its own
        for reasons unrelated to this one (confirmed: it failed
        independently of this method in production for the same account).
      - Post dates from each grid thumbnail's own <img alt="..."> text -
        the same "... on August 14, 2026." pattern as
        extract_embedded_timeline's accessibility_caption (same
        Pacific-time caveat applies, see ACCESSIBILITY_CAPTION_TZ), just
        read off the actual rendered `<img>` tag instead of a nested
        Relay JSON payload. Deliberately simpler and structurally
        different from extract_embedded_timeline - the point of having
        both is that a rendering quirk that empties out one data path
        doesn't necessarily empty out the other.

    Retries like extract_embedded_timeline, for the same reason: no
    guarantee the grid has finished rendering the instant goto_profile's
    settle wait ends.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            html = page.content()
            items = page.eval_on_selector_all(
                "a[href*='/p/'], a[href*='/reel/']",
                "els => els.map(e => ({href: e.getAttribute('href'), "
                "alt: (e.querySelector('img') || {}).alt || ''}))",
            )
        except PlaywrightError as exc:
            print(f"    grid-timeline fallback: page read failed: {exc}", file=sys.stderr)
            return None, []

        id_match = USER_ID_PATTERN.search(html)
        user_id = id_match.group(1) if id_match else None

        media = []
        for item in items:
            code_match = POST_CODE_PATTERN.search(item.get("href") or "")
            if not code_match:
                continue
            date_match = ACCESSIBILITY_DATE_PATTERN.search(item.get("alt") or "")
            if not date_match:
                continue
            try:
                post_date = datetime.strptime(date_match.group(1), "%B %d, %Y").date()
            except ValueError:
                continue
            taken_at = int(
                datetime(post_date.year, post_date.month, post_date.day, 12, tzinfo=ACCESSIBILITY_CAPTION_TZ)
                .timestamp()
            )
            media.append({
                "taken_at": taken_at,
                "code": code_match.group(1),
                "media_type": None,
                "product_type": None,
            })

        if user_id or media:
            return user_id, media

        if attempt < max_attempts:
            page.wait_for_timeout(retry_interval_ms)

    print(
        f"    grid-timeline fallback: no user id or dated posts found in the rendered "
        f"page after {max_attempts} attempts",
        file=sys.stderr,
    )
    return None, []


def extract_embedded_timeline(
    page: Page, max_attempts: int = 4, retry_interval_ms: int = 1500
) -> tuple[str | None, list[dict]]:
    """Recovers (user_id, media) from the profile page's own embedded
    Relay/GraphQL preload data (a `polaris_ordered_timeline_connection`
    inside one of the page's `<script type="application/json">` blocks) -
    a completely different, server-rendered code path from the
    web_profile_info XHR call, useful precisely when that call fails
    outright. Costs no extra request: reads whatever `page` already
    loaded as part of its normal navigation (see goto_profile).

    Retries a few times with a short pause between attempts: this data
    isn't guaranteed to be present the instant goto_profile's fixed
    2-second settle wait ends - confirmed in production, where a run
    found zero application/json blocks at all (not just missing the
    marker - the DOM query itself came back empty) on one connection,
    while the identical account succeeded immediately on another. Only
    costs extra time on this already-rare fallback path; the normal happy
    path (web_profile_info succeeding) never reaches this function.

    Post dates come from each item's accessibility_caption rather than a
    raw timestamp - see ACCESSIBILITY_DATE_PATTERN's comment for the
    Pacific-time caveat. Posts whose caption doesn't match the expected
    pattern are silently skipped rather than guessed at.

    Returns (None, []) if the expected structure never showed up (e.g.
    Instagram changes this internal format, or the account genuinely
    couldn't be loaded) - callers should treat that the same as any other
    failed recovery attempt.
    """
    marker_blocks: list[str] = []
    blocks: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            blocks = page.eval_on_selector_all(
                "script[type='application/json']", "els => els.map(e => e.textContent)"
            )
        except PlaywrightError as exc:
            print(f"    embedded-timeline fallback: eval_on_selector_all failed: {exc}", file=sys.stderr)
            return None, []

        marker_blocks = [b for b in blocks if "polaris_ordered_timeline_connection" in b]
        if marker_blocks:
            break
        if attempt < max_attempts:
            page.wait_for_timeout(retry_interval_ms)

    if not marker_blocks:
        print(
            f"    embedded-timeline fallback: none of {len(blocks)} application/json blocks "
            f"contained the expected marker after {max_attempts} attempts",
            file=sys.stderr,
        )
        return None, []

    for raw in marker_blocks:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            print(f"    embedded-timeline fallback: marker block wasn't valid JSON: {exc}", file=sys.stderr)
            continue

        conn = _find_key(data, "polaris_ordered_timeline_connection")
        edges = (conn or {}).get("edges")
        if not edges:
            print(
                "    embedded-timeline fallback: marker block had no usable "
                "polaris_ordered_timeline_connection.edges",
                file=sys.stderr,
            )
            continue

        user_id = None
        media = []
        for edge in edges:
            node = edge.get("node") or {}
            code = node.get("code")
            if not code:
                continue
            if user_id is None:
                user_id = (node.get("user") or {}).get("pk")

            match = ACCESSIBILITY_DATE_PATTERN.search(node.get("accessibility_caption") or "")
            if not match:
                continue
            try:
                post_date = datetime.strptime(match.group(1), "%B %d, %Y").date()
            except ValueError:
                continue
            taken_at = int(
                datetime(post_date.year, post_date.month, post_date.day, 12, tzinfo=ACCESSIBILITY_CAPTION_TZ)
                .timestamp()
            )
            media.append({
                "taken_at": taken_at,
                "code": code,
                "media_type": node.get("media_type"),
                "product_type": node.get("product_type"),
            })

        if user_id or media:
            return user_id, media
        print(
            "    embedded-timeline fallback: found edges but recovered neither a user id nor any dated post",
            file=sys.stderr,
        )

    return None, []


def resolve_identity(
    handle: str, page: Page
) -> tuple[str | None, str | None, list[dict] | None, str | None, bool, int | None]:
    """Resolves a handle via web_profile_info - the endpoint confirmed
    reliable through a real browser (see module docstring). Returns
    (user_id, profile_pic_url, embedded_media, error_message, was_blocked,
    total_post_count).

    `embedded_media` is web_profile_info's own ~12-most-recent-posts list
    (already fetched as part of this same call, no extra request) - enough
    on its own to answer "did they post in the last day or two" without
    ever touching /api/v1/feed/user/, which has looked separately
    throttled by cumulative volume regardless of client (see docstring).
    Only used as the *sole* post source when MAX_FEED_PAGES == 1; deeper
    backfills still paginate feed/user on top of this.

    `total_post_count` is the account's all-time post count, straight from
    edge_owner_to_timeline_media.count. When it's <= len(embedded_media),
    the embedded list IS the account's entire history, not just a capped
    recent slice - a stronger coverage signal than the id-only fallback
    path below can ever give, which is why it's threaded all the way out
    to check_handle rather than discarded.

    For accounts where web_profile_info fails outright (see module
    docstring for why - it's a real, per-account failure mode, not just
    rate limiting), falls back in three tiers, each independent enough
    that one failing doesn't imply the others will too (confirmed in
    production: tiers 1 and 2 have each failed on their own, on different
    runs, for the same account):
      1. extract_grid_timeline - reads the id and dated posts straight
         off the rendered page a human actually sees. No extra request.
      2. extract_embedded_timeline - scrapes the id *and* real post dates
         out of the same already-loaded page's own embedded Relay preload
         JSON instead. No extra request.
      3. A plain HTTP request (crawler UA, not the browser - never needed
         JS) scraping just the numeric id out of the rendered HTML. Last
         resort - can't recover any post data, only the id, and is itself
         a wholly separate request that can fail independently of 1 and 2.
    """
    nav_error = goto_profile(page, handle)
    if nav_error:
        return None, None, None, f"navigation to profile page failed: {nav_error}", False, None

    data, error, was_blocked = fetch_in_page(page, PROFILE_INFO_ENDPOINT, {"username": handle}, PROFILE_INFO_HEADERS)
    if not error:
        user = (data or {}).get("data", {}).get("user")
        if user and user.get("id"):
            timeline = user.get("edge_owner_to_timeline_media", {})
            edges = timeline.get("edges", [])
            media = [
                {
                    "taken_at": e.get("node", {}).get("taken_at_timestamp"),
                    "code": e.get("node", {}).get("shortcode"),
                    "media_type": None,
                    "product_type": None,
                }
                for e in edges
            ]
            return user["id"], user.get("profile_pic_url"), media, None, False, timeline.get("count")
        error = "no user data returned (account may not exist)"

    # web_profile_info failed outright - try recovering both the id and
    # real post data from the same already-loaded page (no extra request
    # either way), in two independent, structurally different ways before
    # falling further back to a separate id-only HTTP request below. Grid
    # first: it reads the actual rendered page a human sees, which has
    # proven more resilient in practice than the deeply-nested Relay
    # preload JSON extract_embedded_timeline depends on (both target the
    # same "... on August 14, 2026." style dates - see
    # ACCESSIBILITY_CAPTION_TZ for the shared Pacific-time caveat).
    grid_user_id, grid_media = extract_grid_timeline(page)
    if grid_user_id:
        return grid_user_id, None, grid_media, None, False, None

    embedded_user_id, embedded_media = extract_embedded_timeline(page)
    if embedded_user_id:
        return embedded_user_id, None, embedded_media, None, False, None

    time.sleep(MIN_REQUEST_INTERVAL + random.uniform(0, REQUEST_JITTER))
    try:
        resp = page.context.request.get(
            f"https://www.instagram.com/{handle}/",
            headers={"User-Agent": CRAWLER_UA},
            timeout=20000,
        )
        html = resp.text()
        html_error = None
    except PlaywrightError as exc:
        html = None
        html_error = str(exc)

    if html_error:
        return None, None, None, f"{error}; HTML fallback (id lookup) failed: {html_error}", was_blocked, None

    match = USER_ID_PATTERN.search(html or "")
    if not match:
        return None, None, None, f"{error}; HTML fallback failed: could not find numeric user id on profile page", was_blocked, None

    return match.group(1), None, None, None, False, None


def fetch_media_paginated(
    page: Page, user_id: str
) -> tuple[list[dict] | None, str | None, str | None, bool, bool]:
    """Paginates /api/v1/feed/user/<id>/ for up to MAX_FEED_PAGES pages
    (stopping earlier only if Instagram itself reports no more data via
    more_available/next_max_id). Returns
    (media, profile_pic_url, error_message, was_blocked, exhausted).

    `exhausted` is True only when Instagram itself signalled "no more
    data" (empty page, or more_available/cursor absent) - i.e. we know
    for certain there are no posts older than what we fetched. It's False
    when we stopped because MAX_FEED_PAGES ran out while more_available
    was still true, or because a later page errored - in both cases there
    could be older posts we never saw, so the caller must not treat
    silence past the oldest fetched post as "didn't post" evidence.
    """
    all_media: list[dict] = []
    profile_pic_url = None
    cursor = None
    exhausted = False

    for page_num in range(1, MAX_FEED_PAGES + 1):
        if page_num > 1:
            time.sleep(MIN_REQUEST_INTERVAL + random.uniform(0, REQUEST_JITTER))

        params = {"count": FEED_ITEM_COUNT}
        if cursor:
            params["max_id"] = cursor

        feed, error, was_blocked = fetch_in_page(
            page, FEED_ENDPOINT_TEMPLATE.format(user_id=user_id), params, FEED_HEADERS
        )
        if error:
            if all_media:
                break  # keep whatever we already have rather than discarding it (exhausted stays False)
            return None, None, error, was_blocked, False

        items = (feed or {}).get("items", [])
        if not items:
            exhausted = True
            break

        if profile_pic_url is None:
            profile_pic_url = (items[0].get("user") or {}).get("profile_pic_url")

        for it in items:
            all_media.append({
                "taken_at": it.get("taken_at"),
                "code": it.get("code"),
                "media_type": it.get("media_type"),
                "product_type": it.get("product_type"),
            })

        cursor = feed.get("next_max_id")
        more_available = feed.get("more_available")
        if not more_available or not cursor:
            exhausted = True
            break
        # Deliberately not stopping early based on the oldest item seen:
        # Instagram interleaves reels/clips non-chronologically with
        # regular posts within a single page (confirmed in production - a
        # months-old reel appeared mid-page alongside recent posts), so
        # "oldest item in this page is past the window" is not a reliable
        # signal that later pages won't still contain in-window posts.
        # Only more_available/cursor absence is trusted to mean "no more
        # data" - otherwise we always use the full MAX_FEED_PAGES budget.

    return all_media, profile_pic_url, None, False, exhausted


def bucket_media_by_day(
    media: list[dict],
    window: list[date],
    tz: ZoneInfo,
    today: date,
    total_post_count: int | None = None,
    feed_exhausted: bool = False,
) -> dict[str, dict]:
    """Buckets fetched posts into a per-day results dict, writing an entry
    only for dates within actual fetch coverage.

    This is the core data-integrity rule for the whole tracker: a date
    with no matching post is only "didn't post" if we know our fetch
    actually reached that far back. Otherwise it's just unknown, and gets
    no entry at all - the caller (check_handle -> db.upsert_ok) then
    leaves whatever was already stored for that date untouched, rather
    than overwriting a previously-confirmed "posted" day with a false
    "didn't post" just because this run's fetch didn't reach back that
    far. This exact failure mode hit production once already (see
    check_posts.py's module docstring / README's "How it works").

    Coverage is "full" (every window day gets an entry, oldest to newest)
    when either:
      - `feed_exhausted` is True - Instagram itself gave a real "no more
        data" signal during feed/user pagination (see
        fetch_media_paginated's own `exhausted`), or
      - `total_post_count` (the account's all-time post count, from
        web_profile_info) is <= len(media) - the fetched list already IS
        the account's entire history, regardless of pagination.
    Otherwise coverage only extends back to the oldest fetched post's
    date - `media` is assumed to be a contiguous, no-gaps list back to
    that point (true for web_profile_info's embedded list and for
    feed/user pagination, per their own docstrings).

    `today` itself never gets an entry either, same as a date after it -
    it's still in progress, and a snapshot taken partway through the day
    would read as a false "didn't post" for anyone who simply hasn't
    posted *yet*. The tracker only ever asserted "yesterday" in its
    original rolling-window design for exactly this reason; a
    WINDOW_START_DATE window that reaches up to the present day inherits
    the same rule rather than a special case. Only a fully-elapsed day
    (strictly before `today`) is safe to call "didn't post." Pass `today`
    explicitly (rather than computing it here) so this function stays a
    pure, deterministic unit to test.
    """
    media = media or []
    full_history_reached = feed_exhausted or (total_post_count is not None and total_post_count <= len(media))

    window_set = set(window)
    permalink_by_date: dict[date, str | None] = {}
    dates_with_data: list[date] = []
    for node in media:
        ts = node.get("taken_at")
        if ts is None:
            continue
        posted_date = datetime.fromtimestamp(ts, tz=tz).date()
        dates_with_data.append(posted_date)
        if posted_date in window_set and posted_date not in permalink_by_date:
            permalink_by_date[posted_date] = node.get("permalink")

    if full_history_reached:
        coverage_start = window[0] if window else None
    elif dates_with_data:
        coverage_start = min(dates_with_data)
    else:
        coverage_start = None  # no usable evidence at all - don't touch any existing rows

    results = {}
    for d in window:
        if d >= today:
            continue  # today's not over yet, and later dates haven't happened at all - nothing to assert
        if coverage_start is None or d < coverage_start:
            continue
        if d in permalink_by_date:
            results[d.isoformat()] = {"status": "ok", "posted": True, "permalink": permalink_by_date[d]}
        else:
            results[d.isoformat()] = {"status": "ok", "posted": False}
    return results


def check_handle(
    handle: str, window: list[date], tz: ZoneInfo, page: Page, conn: sqlite3.Connection
) -> tuple[dict[str, dict] | None, str | None, bool, str | None]:
    """Returns (results_by_iso_date, error_message, was_blocked, profile_pic_url).

    On success, only dates we actually have evidence for get an entry
    (posted True/False) - see bucket_media_by_day for the coverage rule.
    On failure, returns (None, error_message, was_blocked, None) and the
    caller decides how to handle gaps.
    """
    # Always resolve via web_profile_info - the endpoint confirmed
    # reliable through a real browser - rather than skipping it when the
    # id is already cached. It's cheap (one request) and its embedded
    # ~12-post list alone answers "did they post in the last day or two"
    # without ever touching feed/user, which has looked separately
    # throttled regardless of client (see module docstring). The id cache
    # still saves us the crawler-UA fallback path staying necessary.
    user_id, profile_pic_url, media, error, was_blocked, total_post_count = resolve_identity(handle, page)
    media = media or []
    feed_exhausted = False
    used_known_id_fallback = False

    if error:
        # Last resort: an account whose id can never be freshly
        # rediscovered from this environment (see KNOWN_USER_IDS) isn't
        # necessarily unrecoverable - feed/user uses the same in-page
        # fetch() mechanism the primary web_profile_info call does, which
        # has proven reliable even where resolve_identity's own
        # DOM-reading fallbacks fail (confirmed for bry.trieu: every
        # fresh id-discovery method fails from GitHub Actions, but
        # feed/user succeeds fine once you already have the id).
        known_user_id = KNOWN_USER_IDS.get(handle)
        if known_user_id:
            print(
                f"    resolve_identity failed ({error}) - trying feed/user with known user_id {known_user_id}",
                file=sys.stderr,
            )
            time.sleep(MIN_REQUEST_INTERVAL + random.uniform(0, REQUEST_JITTER))
            media, profile_pic_url, feed_error, feed_blocked, feed_exhausted = fetch_media_paginated(
                page, known_user_id
            )
            was_blocked = was_blocked or feed_blocked
            if feed_error:
                return None, f"{error}; feed/user with known user_id also failed: {feed_error}", was_blocked, None
            user_id = known_user_id
            media = media or []
            total_post_count = None
            used_known_id_fallback = True
            error = None
        if error:
            return None, error, was_blocked, None

    db.save_user_id(conn, handle, user_id, datetime.now(tz).isoformat())

    # Only paginate feed/user further when explicitly asked for deeper
    # coverage than we already have (backfills). Daily checks
    # (MAX_FEED_PAGES=1) never touch feed/user at all on the normal path;
    # the known-id fallback above already paginated up to MAX_FEED_PAGES
    # on its own, so skip repeating that here.
    if MAX_FEED_PAGES > 1 and not used_known_id_fallback:
        time.sleep(MIN_REQUEST_INTERVAL + random.uniform(0, REQUEST_JITTER))
        extra_media, feed_profile_pic_url, feed_error, feed_blocked, feed_exhausted = fetch_media_paginated(
            page, user_id
        )
        was_blocked = was_blocked or feed_blocked
        if feed_error:
            # Don't fail the whole handle over this - we still have the
            # embedded posts from web_profile_info above, which is real,
            # usable data even if the deeper backfill couldn't complete.
            print(f"    feed/user pagination failed (keeping embedded posts): {feed_error}", file=sys.stderr)
            feed_exhausted = False
        else:
            media = media + (extra_media or [])
            if profile_pic_url is None:
                profile_pic_url = feed_profile_pic_url

    # Enrich with permalink/posted_at and persist every raw fetched post
    # (not just the one-per-day summary below) for later debugging/
    # inspection - see db.save_posts.
    for node in media:
        code = node.get("code")
        node["permalink"] = f"https://www.instagram.com/p/{code}/" if code else None
        ts = node.get("taken_at")
        node["posted_at"] = datetime.fromtimestamp(ts, tz=tz).isoformat() if ts is not None else None
    if media:
        db.save_posts(conn, handle, media, datetime.now(tz).isoformat())

    today = datetime.now(tz).date()
    results = bucket_media_by_day(media, window, tz, today, total_post_count, feed_exhausted)
    return results, None, was_blocked, profile_pic_url


def fetch_avatar_bytes(page: Page, url: str) -> tuple[bytes | None, str | None, str | None]:
    """Returns (content, content_type, error_message)."""
    try:
        resp = page.context.request.get(url, timeout=20000)
    except PlaywrightError as exc:
        return None, None, str(exc)
    if resp.status != 200:
        return None, None, f"HTTP {resp.status}"
    content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    return resp.body(), content_type, None


def main() -> None:
    handles = load_handles(HANDLES_FILE)
    if not handles:
        print("no handles to check, exiting", file=sys.stderr)
        sys.exit(1)

    tz = ZoneInfo(TRACKER_TIMEZONE)
    window = resolve_window(tz)
    # 1 request/handle for web_profile_info, + (MAX_FEED_PAGES - 1) more
    # if deep feed/user pagination is enabled, +1 more for a first-time
    # avatar fetch. This is a rough upper-bound estimate, not exact, and
    # doesn't count real browser page-load overhead (a few seconds per
    # navigation) on top of the pacing sleep.
    est_minutes = round(len(handles) * MAX_FEED_PAGES * MIN_REQUEST_INTERVAL / 60, 1)
    print(
        f"checking {len(handles)} handle(s) for posts across "
        f"{window[0].isoformat()}..{window[-1].isoformat()} ({TRACKER_TIMEZONE}), "
        f"~{est_minutes}+ min at current pacing (assuming no blocks; excludes browser page-load overhead)"
    )

    conn = db.connect(DB_FILE)

    today = datetime.now(tz).date()
    purged = db.purge_future_dates(conn, today)
    if purged:
        print(f"purged {purged} stale row(s) for today or later ({today.isoformat()})", file=sys.stderr)

    persisted_state = db.load_session_cookies(conn)
    if persisted_state:
        print("loaded persisted browser state from a prior run")
    else:
        print("no persisted browser state yet - this run will establish and save a fresh one")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        try:
            context = browser.new_context(
                storage_state=persisted_state,
                user_agent=DESKTOP_UA,
                viewport={"width": 1280, "height": 800},
            )
        except Exception as exc:
            # Guards against stale state from a prior architecture (e.g. a
            # flat cookie dict from the pre-Playwright curl_cffi version,
            # which isn't valid storage_state shape) - fall back to a
            # fresh context rather than crashing the whole run.
            print(f"persisted browser state was invalid, starting fresh: {exc}", file=sys.stderr)
            context = browser.new_context(user_agent=DESKTOP_UA, viewport={"width": 1280, "height": 800})
        page = context.new_page()

        consecutive_blocks = 0
        cooldowns_used = 0
        stopped_early = False
        request_count = 0

        i = 0
        while i < len(handles):
            handle = handles[i]
            display_i = i + 1
            checked_at = datetime.now(tz).isoformat()

            if stopped_early:
                db.fill_gaps_with_error(
                    conn, handle, window,
                    "skipped: stopped early after repeated blocking from Instagram this run",
                    checked_at, today,
                )
                i += 1
                continue

            if request_count > 0:
                time.sleep(MIN_REQUEST_INTERVAL + random.uniform(0, REQUEST_JITTER))
            request_count += 1

            results_by_date, error, was_blocked, profile_pic_url = check_handle(handle, window, tz, page, conn)
            consecutive_blocks = consecutive_blocks + 1 if was_blocked else 0

            if was_blocked and consecutive_blocks >= CONSECUTIVE_BLOCK_LIMIT:
                if cooldowns_used < MAX_COOLDOWNS:
                    cooldowns_used += 1
                    print(
                        f"  blocked on {handle} - cooling down {COOLDOWN_SECONDS:.0f}s "
                        f"(cooldown {cooldowns_used}/{MAX_COOLDOWNS}) before retrying",
                        file=sys.stderr,
                    )
                    time.sleep(COOLDOWN_SECONDS)
                    consecutive_blocks = 0
                    continue  # retry the same handle, don't advance i or record an error yet
                stopped_early = True

            if error:
                db.fill_gaps_with_error(conn, handle, window, error, checked_at, today)
                print(f"  [{display_i}/{len(handles)}] {handle}: error - {error}")
            else:
                posted_count = sum(1 for r in results_by_date.values() if r.get("posted"))
                db.upsert_ok(conn, handle, results_by_date, checked_at)
                coverage_note = (
                    "" if len(results_by_date) == len(window)
                    else f" ({len(results_by_date)}/{len(window)} days had fresh evidence, rest left as-is)"
                )
                print(
                    f"  [{display_i}/{len(handles)}] {handle}: posted {posted_count}/{len(results_by_date)} "
                    f"of covered days{coverage_note}"
                )

                # Avatars rarely change - fetch once and cache, rather than
                # re-downloading on every run.
                if profile_pic_url and not db.has_avatar(conn, handle):
                    time.sleep(MIN_REQUEST_INTERVAL + random.uniform(0, REQUEST_JITTER))
                    content, content_type, avatar_error = fetch_avatar_bytes(page, profile_pic_url)
                    if content:
                        db.save_avatar(conn, handle, content, content_type, checked_at)
                        print(f"    cached avatar ({len(content)} bytes)")
                    else:
                        print(f"    avatar fetch failed: {avatar_error}", file=sys.stderr)

            if stopped_early:
                reason = (
                    "stopping immediately on block (MAX_COOLDOWNS=0)"
                    if MAX_COOLDOWNS == 0
                    else f"giving up after {MAX_COOLDOWNS} cooldown(s)"
                )
                print(f"  {reason} - marking remaining handles as skipped", file=sys.stderr)

            i += 1

        state_now = context.storage_state()
        db.save_session_cookies(conn, state_now, datetime.now(tz).isoformat())
        print("saved browser state for next run")

        browser.close()

    snapshot = db.export_window(conn, handles, window)
    snapshot["avatars"] = db.export_avatars(conn, handles, HISTORY_FILE.parent / "avatars")
    snapshot["updated_at"] = datetime.now(tz).isoformat()
    conn.close()

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    print(f"wrote {HISTORY_FILE} and {DB_FILE}")


if __name__ == "__main__":
    main()
