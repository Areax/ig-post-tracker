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

Pacing defaults to ~10-15s/request, brought down from a much more
conservative ~60-75s (~1/min) that had itself been tightened from an
earlier 30-40s default after a real block was hit at that pace too, on a
fresh IP, mid-run. The 10-15s default cleared one full local run across
all 35 tracked handles with zero blocks (2026-09-13), but that was from a
residential IP with an already-established session - it has not yet been
confirmed safe from GitHub Actions' IP range, which is a materially
different environment. Watch the first few scheduled/cloud runs closely
after this change and be ready to raise MIN_REQUEST_INTERVAL_SECONDS /
REQUEST_JITTER_SECONDS back up if a block shows up there.

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
  MIN_REQUEST_INTERVAL_SECONDS - default 10
  REQUEST_JITTER_SECONDS       - default 5 (randomized on top of the min interval, so 10-15s)
  MAX_FEED_PAGES                - default 1 (the ~12 most recent posts, from web_profile_info alone -
                                  see resolve_identity). Set above 1 for deeper feed/user-paginated
                                  backfills (~12 more posts/page); the scheduled daily workflow pins
                                  this to 1 explicitly since a daily check only needs yesterday's post.
  COOLDOWN_SECONDS             - default 900 (15 min), only relevant if MAX_COOLDOWNS > 0.
  MAX_COOLDOWNS                - default 0: on a block, stop immediately - mark that handle as an
                                  error (the real message, visible in the UI) and the rest of the
                                  run as skipped, rather than waiting. Set > 0 to opt back into
                                  cooldown-and-resume (e.g. for an unattended backfill).
  HEADLESS                     - default "1" (headless browser). Set "0" to watch it run locally.
  PROXY_SERVER, PROXY_USERNAME, - unset by default (direct connection). Set all three together
  PROXY_PASSWORD                 to route the whole browser instance through a residential proxy
                                  (e.g. IPRoyal) - see resolve_proxy_config's docstring for why.
  IG_SESSION_STATE_JSON         - unset by default (fully anonymous, unchanged). Set to a real
                                  logged-in account's Playwright storage_state (JSON) to unlock
                                  pagination past each account's ~12 most recent posts on
                                  MAX_FEED_PAGES > 1 backfills - see resolve_authenticated_state's
                                  docstring for why anonymous access can't do this at all.
"""

from __future__ import annotations

import csv
import json
import os
import random
import re
import sqlite3
import string
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
    # Same class of failure, confirmed 2026-09-13: resolves fine locally
    # (id 79953093352, from a real local run's identities table) but every
    # one of resolve_identity's fallback tiers failed from GitHub Actions -
    # grid and embedded-timeline extraction both found nothing, and the
    # last-resort crawler-UA HTML fetch got rate-limited (401) outright.
    # This was also the account whose failure exposed the run-wide
    # circuit-breaker bug fixed via CONSECUTIVE_BLOCK_LIMIT above - a
    # single account this broken from CI used to abort the entire run.
    "lj.exist": "79953093352",
}

FEED_ITEM_COUNT = 30
# A real gap-in-a-single-fetch, confirmed twice in production on two
# different endpoints (feed/user's non-chronological interleaving; a
# manually-reordered profile grid changing edge_owner_to_timeline_media's
# display order), showed that fetching more pages doesn't reliably fix
# this class of bug - the gap isn't a "ran out of pages" problem, it's
# that a single fetch can't be trusted as a complete, gapless picture no
# matter how large. The actual fix is at the DB layer instead (see
# db.upsert_ok: a confirmed `posted: true` can never be downgraded back
# to false by a later write, so a day missed by one gappy fetch just
# self-heals the next time any run's fetch happens to include it) -
# deep, multi-page backfills are no longer a real use case this project
# needs, so this defaults to 1 everywhere, including the known-id
# fallback in check_handle.
MAX_FEED_PAGES = int(os.environ.get("MAX_FEED_PAGES", "1"))
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

TRACKER_TIMEZONE = os.environ.get("TRACKER_TIMEZONE", "America/Los_Angeles")
HANDLES_FILE = Path(os.environ.get("HANDLES_FILE", "handles.csv"))
DB_FILE = Path(os.environ.get("DB_FILE", "data/tracker.db"))
HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", "docs/data/history.json"))
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "14"))

# Optional residential proxy (e.g. IPRoyal) for the whole browser instance -
# added 2026-09-13 after GitHub Actions' shared runner IP got rate-limited
# by Instagram mid-session (3 handles in a row all 401'd - see
# CONSECUTIVE_BLOCK_LIMIT's comment for the incident this came from).
# Datacenter IPs are broadly blocked; a residential proxy makes GitHub
# Actions' traffic look like an ordinary residential connection instead,
# the same kind of connection that has run this script all day locally
# with zero real blocks. All three env vars must be set together for the
# proxy to be used at all - unset (the default), this changes nothing and
# every request goes out directly, exactly as before this existed.
#   PROXY_SERVER   - e.g. "http://geo.iproyal.com:12321"
#   PROXY_USERNAME - proxy account username
#   PROXY_PASSWORD - proxy account password, WITHOUT any session suffix -
#                    resolve_proxy_config appends its own per-rotation
#                    "_session-<id>_lifetime-<mins>m" (IPRoyal's syntax)
#                    when a session_id is given
# Originally one sticky session for the whole run, on the theory that
# switching IPs mid-run would undermine the single consistent identity
# (persisted cookies) the run already relies on. Reversed 2026-09-16: in
# production, that one sticky IP was instead getting hammered by every
# handle across every run, for days on end (47 handles x 2 runs/day),
# which burns an IP's reputation with Instagram just as fast as GitHub's
# own shared runner IP did - confirmed live the same day (a fresh browser
# session still got an immediate 401 through it). Real residential
# traffic doesn't sit behind one unchanging IP for days either, so
# rotating periodically while keeping the same cookies is not a
# consistency violation - see PROXY_ROTATE_EVERY below.
PROXY_SERVER = os.environ.get("PROXY_SERVER")
PROXY_USERNAME = os.environ.get("PROXY_USERNAME")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD")
# How many requests to make through one proxy session before rotating to
# a fresh IPRoyal session (and, with it, almost certainly a new exit IP -
# see resolve_proxy_config). Only meaningful when the proxy is configured
# at all.
PROXY_ROTATE_EVERY = int(os.environ.get("PROXY_ROTATE_EVERY", "5"))
# How long a single rotation's IPRoyal session is allowed to live before
# IPRoyal itself would cycle it - comfortably longer than
# PROXY_ROTATE_EVERY requests' worth of pacing so we're always the one
# deciding when to rotate, not IPRoyal timing out mid-batch.
PROXY_SESSION_LIFETIME_MINUTES = int(os.environ.get("PROXY_SESSION_LIFETIME_MINUTES", "10"))

# Optional authenticated session - the one thing confirmed, three
# independent ways on 2026-09-14, to actually remove the wall on getting
# more than each account's ~12 most recent posts while anonymous:
#   1. GraphQL: "Unauthorized logged out query." (error code 1675002) on
#      the pagination query a real browser fires on page load.
#   2. The legacy REST endpoint this project's own fetch_media_paginated
#      targets (/api/v1/feed/user/): consistent 401s regardless of fresh
#      session, fresh proxy IP, or matching headers.
#   3. Live network capture while actually scrolling a real profile grid:
#      the browser never once requests more of that account's posts -
#      not via feed/user, not via the anonymous-specific GraphQL
#      operation a public reference implementation documents
#      (PolarisLoggedOutDesktopWWWProfilePostsTabContentQuery_connection,
#      doc_id 7950326061742207 as of this research - Instagram rotates
#      doc_ids every 2-4 weeks, so treat it as a starting point, not a
#      permanent value). It fires suggested-account/experiment queries
#      instead.
# Unset (the default), this changes nothing - every request stays fully
# anonymous, exactly as it always has, and MAX_FEED_PAGES > 1 backfills
# stay capped at whatever the first page already gave. Set to a real
# logged-in account's Playwright storage_state (cookies + localStorage,
# as JSON - see https://playwright.dev/python/docs/auth#reuse-signed-in-state
# for how to produce one: log into a real, dedicated account once with a
# real browser under Playwright, then `context.storage_state(path=...)`)
# to use that identity for the whole browser context instead, which
# should - unverified here, since this needs a real account's
# credentials this script has no way to supply itself - unlock both the
# GraphQL pagination query and feed/user for real, since both walls
# above are specifically about being logged OUT, not about anything
# account- or client-specific.
IG_SESSION_STATE_JSON = os.environ.get("IG_SESSION_STATE_JSON")

# Confirmed in production, 2026-09-13: routing through IPRoyal's residential
# proxy, 39/47 handles failed with "Page.goto: Timeout 30000ms exceeded" -
# not an Instagram block at all (no rate-limit message, no 401 - the page
# load itself just didn't finish in time). A residential peer's own home
# connection adds real, variable latency on top of the extra proxy hop;
# 30s (fine and unchanged for a direct connection) isn't generous enough
# for that. Scales automatically with whether a proxy is configured, so
# direct-connection runs keep the tighter, already-proven default.
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "60000" if PROXY_SERVER else "30000"))

MIN_REQUEST_INTERVAL = float(os.environ.get("MIN_REQUEST_INTERVAL_SECONDS", "10"))
REQUEST_JITTER = float(os.environ.get("REQUEST_JITTER_SECONDS", "5"))
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 20
# A block on ONE handle has turned out, twice in production now (bry.trieu,
# then lj.exist - both resolve fine locally but fail every fallback tier
# from GitHub Actions' IP specifically), to be an account/environment-
# specific quirk rather than a sign the whole session is under a
# sustained block. At 1, a single such account used to abort the entire
# run and mark every OTHER handle as "skipped" too, for no reason related
# to them at all - confirmed in production on 2026-09-13, when lj.exist
# alone (handle #1 of 47) zeroed out that day's check for all 47 tracked
# people. 3 in a row (never reset by a successful check in between - see
# the main loop) is a much stronger, still-fast signal of an actual
# sustained block, while not being tripped by one flaky account.
CONSECUTIVE_BLOCK_LIMIT = 3
RATE_LIMIT_STATUS_CODES = (429, 401)
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "900"))
MAX_COOLDOWNS = int(os.environ.get("MAX_COOLDOWNS", "0"))
# A single handle's own real block (was_blocked - a 401/403/429 HTTP
# response, not a dead tunnel or an ambiguous "private" verdict) gets a
# fresh proxy session and an immediate retry, the same as
# MAX_PROXY_CONNECTION_RETRIES/MAX_PRIVATE_VERDICT_RETRIES already do for
# their own failure shapes - a block is Instagram flagging a specific
# IP's reputation, and rotating is the fix elsewhere in this file too.
# This replaced an earlier version (2026-09-24) that only rotated once
# CONSECUTIVE_BLOCK_LIMIT (3) DIFFERENT handles had already failed in a
# row - confirmed in production, 2026-09-26, that a run can go two
# isolated handles deep into a block (tt.talks+janeevanswalther,
# eddiebriant.re+nancyrhuang - both exactly 2, both surrounded by
# otherwise-successful handles) without ever reaching 3, so those never
# got a single retry attempt and were recorded as permanent errors on
# the very first hit. Retrying per-handle, immediately, catches both
# that case and the original 3-in-a-row one - a handle that exhausts its
# own retry budget still falls through into consecutive_blocks/
# CONSECUTIVE_BLOCK_LIMIT below as before, so a genuinely broad/sustained
# block (many different handles, each already rotated and still failing)
# still stops the run rather than burning through fresh sessions forever.
MAX_BLOCKED_RETRIES = int(os.environ.get("MAX_BLOCKED_RETRIES", "2"))
# How many times in a row a dead proxy session (is_proxy_connection_error)
# gets a fresh rotation + immediate retry on the SAME handle before giving
# up and recording it as a real error. Each rotation is a different exit
# IP, so this is really "how many different dead IPs in a row are we
# willing to write off as bad luck" - low, since a real widespread proxy
# outage should surface as an error quickly rather than retry forever.
MAX_PROXY_CONNECTION_RETRIES = 3
# A "private or no visible posts" verdict is only sometimes real (an
# actually-private account) - confirmed live, 2026-09-16, it's also what
# a soft-blocked exit IP produces for a genuinely public, active account
# (abourinaris, alexyee.ventures, Zhenginmotion all hit this before a
# fresh proxy session cleared it). Retry it a couple of times on a fresh
# session before accepting it - bounded low because a genuinely private
# account (there are several in the tracked list) pays this same small
# cost on every run, forever.
MAX_PRIVATE_VERDICT_RETRIES = 2
# How many DIFFERENT handles in a row can each exhaust their own
# MAX_PROXY_CONNECTION_RETRIES budget before the run concludes the proxy
# itself is comprehensively down (account suspended, quota/balance
# exhausted, or a real provider outage) rather than "a string of bad
# luck on individual rotated IPs". Confirmed in production, 2026-09-17:
# without this, a fully-dead proxy account burned through 35 handles x 4
# attempts each (~35+ minutes) writing nothing but errors, instead of
# failing fast - see stopped_early's PROXY_DOWN skip reason below.
PROXY_DOWN_HANDLE_LIMIT = 3

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


def random_session_id() -> str:
    """An 8-character alphanumeric id, IPRoyal's required shape for the
    `_session-<id>` password suffix (see resolve_proxy_config)."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def resolve_proxy_config(session_id: str | None = None) -> dict | None:
    """Playwright-shaped proxy config from PROXY_SERVER/USERNAME/PASSWORD,
    or None if they're not all set (the default - direct connection,
    unchanged from before this existed). Deliberately all-or-nothing: a
    partially-configured proxy (e.g. server set but no credentials) is
    almost certainly a mistake, not an intentional unauthenticated-proxy
    setup, so it's treated as "not configured" rather than guessed at.

    `session_id`, when given, is appended as IPRoyal's own
    `_session-<id>_lifetime-<minutes>m` PASSWORD suffix (confirmed live,
    2026-09-16: the same suffix on the USERNAME instead makes Chromium's
    proxy auth fail outright with ERR_PROXY_AUTH_UNSUPPORTED - IPRoyal's
    own docs example puts it on the password:
    "username123:password321_country-br_session-sgn34f3e_lifetime-10m").
    A fresh id resolves to a fresh (confirmed live: different every time)
    exit IP, sticky for PROXY_SESSION_LIFETIME_MINUTES. Omit it to get
    PROXY_PASSWORD as-is (IPRoyal's own default rotation behavior for
    that bare credential).
    """
    if not (PROXY_SERVER and PROXY_USERNAME and PROXY_PASSWORD):
        return None
    password = PROXY_PASSWORD
    if session_id:
        password = f"{password}_session-{session_id}_lifetime-{PROXY_SESSION_LIFETIME_MINUTES}m"
    return {"server": PROXY_SERVER, "username": PROXY_USERNAME, "password": password}


# Chromium network-level error codes that mean the connection itself
# never got established - the underlying TCP tunnel to (or through) the
# proxy failed, as opposed to a normal HTTP response (even a blocked
# one, like a 401) that at least proves the tunnel works. Confirmed live,
# 2026-09-16: one rotated IPRoyal session landed on a dead exit node and
# every one of the 5 handles in that rotation batch failed with
# ERR_TUNNEL_CONNECTION_FAILED before the next scheduled rotation - not
# an Instagram-side block at all, so retrying the same dead session (or
# worse, moving on to the next handle still using it) just wastes the
# whole batch. See is_proxy_connection_error and its call site in main().
PROXY_CONNECTION_ERROR_CODES = (
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_TIMED_OUT",
    # Distinct from ERR_CONNECTION_TIMED_OUT (the TCP handshake itself
    # timing out) - this is a stall further along, e.g. a tunnel that
    # connected but then never got a response. Missed live in production,
    # 2026-09-17 (alexyee.ventures): doesn't contain the substring
    # "Timeout" either, so goto_profile's own single retry never caught
    # it, and it wasn't in this list yet, so main()'s forced-rotation
    # retry didn't either - one un-retried attempt straight to error.
    "ERR_TIMED_OUT",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_SOCKS_CONNECTION_HOST_UNREACHABLE",
    "ERR_EMPTY_RESPONSE",
)


def is_proxy_connection_error(error: str | None) -> bool:
    """Whether `error` (a resolve_identity/check_handle error message,
    which wraps goto_profile's raw Playwright error text) indicates the
    proxy's own connection failed outright, rather than Instagram
    returning any response at all - see PROXY_CONNECTION_ERROR_CODES."""
    if not error:
        return False
    return any(code in error for code in PROXY_CONNECTION_ERROR_CODES)


def resolve_authenticated_state() -> dict | None:
    """A real logged-in account's Playwright storage_state, parsed from
    IG_SESSION_STATE_JSON - see that env var's own comment for what this
    unlocks and why. None (the default, and whenever the JSON is missing
    or malformed) means stay fully anonymous, exactly as before this
    existed - a broken/malformed value is treated as "not configured"
    rather than crashing the run, since going deeper is optional, opt-in
    machinery, never the core daily check itself.
    """
    if not IG_SESSION_STATE_JSON:
        return None
    try:
        state = json.loads(IG_SESSION_STATE_JSON)
    except ValueError:
        return None
    if not isinstance(state, dict):
        return None
    return state


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
    message on failure, None on success.

    One retry on a timeout specifically (not other navigation errors) -
    unlike fetch_in_page's retried API calls, this had none at all, and a
    proxy's variable per-connection latency makes a single slow load a
    poor reason to give up on the whole handle."""
    url = f"https://www.instagram.com/{handle}/"
    last_error = None
    for attempt in range(2):
        if attempt > 0:
            time.sleep(RETRY_BACKOFF_SECONDS)  # give a saturated link a moment, not just an instant re-hit
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(2000)  # let the page's own JS settle
            return None
        except PlaywrightError as exc:
            last_error = str(exc)
            if "Timeout" not in last_error:
                return last_error
    return last_error


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


def extract_reels_tab_codes(page: Page, handle: str, max_attempts: int = 4, retry_interval_ms: int = 1500) -> list[str]:
    """Recovers shortcodes from the account's own /reels/ tab (a separate
    connection, `polaris_clips_connection`, embedded in that tab's own
    server-rendered page - not the same data as the main grid's
    `polaris_ordered_timeline_connection`). Discovered 2026-09-14: this
    connection's edges only overlap PARTIALLY with the main grid's ~12 -
    confirmed live against torch_boy, 6 of 12 overlapped and 6 were
    genuinely new, giving 18 distinct posts total instead of 12, entirely
    through page navigation (goto_profile-shaped, no XHR/API call at all).
    That matters specifically because it's unaffected by web_profile_info
    rate-limiting, which has (confirmed the same day) nothing to do with
    plain page loads.

    Deliberately returns codes only, not dated media: this connection's
    nodes carry no date field whatsoever (no taken_at, no
    accessibility_caption) - see fetch_post_date, which recovers a date
    for one code at a time from that code's own page, the same way
    extract_embedded_timeline already does for the main grid.
    """
    url = f"https://www.instagram.com/{handle}/reels/"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except PlaywrightError as exc:
        print(f"    reels-tab fallback: navigation failed: {exc}", file=sys.stderr)
        return []

    blocks: list[str] = []
    marker_blocks: list[str] = []
    for attempt in range(1, max_attempts + 1):
        page.wait_for_timeout(retry_interval_ms if attempt > 1 else 2000)
        try:
            blocks = page.eval_on_selector_all(
                "script[type='application/json']", "els => els.map(e => e.textContent)"
            )
        except PlaywrightError as exc:
            print(f"    reels-tab fallback: eval_on_selector_all failed: {exc}", file=sys.stderr)
            return []
        marker_blocks = [b for b in blocks if "polaris_clips_connection" in b]
        if marker_blocks:
            break

    if not marker_blocks:
        print(
            f"    reels-tab fallback: none of {len(blocks)} application/json blocks "
            f"contained the expected marker after {max_attempts} attempts",
            file=sys.stderr,
        )
        return []

    for raw in marker_blocks:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        conn = _find_key(data, "polaris_clips_connection")
        edges = (conn or {}).get("edges")
        if not edges:
            continue
        codes = [edge.get("node", {}).get("code") for edge in edges]
        return [c for c in codes if c]

    return []


def fetch_post_date(page: Page, code: str) -> int | None:
    """Recovers one post/reel's taken_at (unix timestamp) from its own
    dedicated page, via the same accessibility_caption mechanism
    extract_embedded_timeline already uses for the main grid - that field
    isn't present on polaris_clips_connection's own nodes (see
    extract_reels_tab_codes), but confirmed live, 2026-09-14, that it IS
    present on the individual post/reel page for the same code. `/p/{code}/`
    works for both regular posts and reels; no need to know which type
    code is ahead of time."""
    url = f"https://www.instagram.com/p/{code}/"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(2000)
    except PlaywrightError as exc:
        print(f"    fetch_post_date({code}): navigation failed: {exc}", file=sys.stderr)
        return None

    html = page.content()
    match = ACCESSIBILITY_DATE_PATTERN.search(html)
    if not match:
        return None
    try:
        post_date = datetime.strptime(match.group(1), "%B %d, %Y").date()
    except ValueError:
        return None
    return int(
        datetime(post_date.year, post_date.month, post_date.day, 12, tzinfo=ACCESSIBILITY_CAPTION_TZ).timestamp()
    )


def fetch_additional_reels(page: Page, handle: str, known_codes: set[str]) -> list[dict]:
    """Combines extract_reels_tab_codes + fetch_post_date into dated media
    entries for whatever the reels tab has that `known_codes` (the main
    grid's own codes) doesn't already cover - see extract_reels_tab_codes'
    docstring for why this is worth doing at all (a real, confirmed gap
    between the two connections) and fetch_post_date's for where the date
    for each new code actually comes from. Paced the same as every other
    per-request call in this module (MIN_REQUEST_INTERVAL/REQUEST_JITTER)
    since each new code costs one additional real page load."""
    reel_codes = extract_reels_tab_codes(page, handle)
    new_codes = [c for c in reel_codes if c not in known_codes]
    media = []
    for code in new_codes:
        time.sleep(MIN_REQUEST_INTERVAL + random.uniform(0, REQUEST_JITTER))
        taken_at = fetch_post_date(page, code)
        if taken_at is None:
            continue
        media.append({"taken_at": taken_at, "code": code, "media_type": None, "product_type": "clips"})
    return media


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


# Shared between resolve_identity and check_handle - both conclude this
# after exhausting every recovery path available to them. Also matched
# by main()'s own retry-on-suspect-private logic (see
# MAX_PRIVATE_VERDICT_RETRIES) - the two paths that produce it can't be
# told apart from the message alone (an explicit is_private:true vs. a
# fully-exhausted set of fallbacks), so both get the same small, bounded
# number of fresh-proxy-session retries before this verdict is accepted.
PRIVATE_OR_NO_POSTS_MESSAGE = "account is private or has no visible posts"


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
            count = timeline.get("count")
            # IG's own `is_private` flag is the one signal trustworthy
            # enough to act on immediately - a hard, explicit assertion
            # from IG itself.
            if user.get("is_private"):
                return None, None, None, PRIVATE_OR_NO_POSTS_MESSAGE, False, None
            if edges or count is not None:
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
            # No edges AND no count, without an explicit is_private flag,
            # used to be trusted outright as "private" - wrong in
            # production, confirmed live 2026-09-16: web_profile_info
            # returned this exact empty-but-200 shape for abourinaris,
            # alexyee.ventures and Zhenginmotion, all demonstrably public
            # and actively posting (same session that minute also got an
            # explicit 401 on a fresh request for abourinaris - this looks
            # like the same soft-block wearing a "looks like private"
            # disguise, not a real signal about the account). Don't return
            # here - fall through to the grid/embedded/HTML fallbacks below
            # (used for an outright API failure), which read the
            # already-rendered page directly instead of trusting this one
            # XHR. Only if those also come up empty does
            # check_handle's own "not media and total_post_count is None"
            # guard call it private, the same as it always has for the
            # id-only fallback paths.
            error = PRIVATE_OR_NO_POSTS_MESSAGE
        else:
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
    page: Page, user_id: str, max_pages: int | None = None
) -> tuple[list[dict] | None, str | None, str | None, bool, bool]:
    """Paginates /api/v1/feed/user/<id>/ for up to `max_pages` pages
    (defaults to MAX_FEED_PAGES if not given; stopping earlier only if
    Instagram itself reports no more data via more_available/next_max_id).
    Returns (media, profile_pic_url, error_message, was_blocked, exhausted).

    `exhausted` is True only when Instagram itself signalled "no more
    data" (empty page, or more_available/cursor absent) - i.e. we know
    for certain there are no posts older than what we fetched. It's False
    when we stopped because the page budget ran out while more_available
    was still true, or because a later page errored - in both cases there
    could be older posts we never saw, so the caller must not treat
    silence past the oldest fetched post as "didn't post" evidence.

    A single page alone can still have an internal gap, not just at the
    boundary - confirmed in production: a single-page (12 item) fetch for
    a real account returned dates in the order Aug 14, 13, 12, then
    jumped straight to Aug 25, 24, 23... - a real post on Aug 17 was
    genuinely absent from that page despite older *and* newer dates being
    present on both sides of it, silently marking a real posted day as a
    false miss (bucket_media_by_day's coverage_start logic only tracks
    the overall oldest date seen, not gaps within that range). A second
    page happened to backfill that particular gap, but fetching more
    pages isn't a real fix - the same class of gap was independently
    confirmed on web_profile_info's embedded post list too (a manually-
    reordered profile grid, nothing to do with pagination at all), so
    there's no page count that reliably guarantees a gapless fetch. The
    actual fix lives in db.upsert_ok instead: a confirmed `posted: true`
    can never be downgraded back to false by a later write, so any gap a
    single fetch has just self-heals whenever some future run's fetch
    happens to include that day. MAX_FEED_PAGES defaults to 1 everywhere
    (including check_handle's known-id fallback) on that basis - deep,
    multi-page backfills aren't a real use case this project needs.
    """
    max_pages = MAX_FEED_PAGES if max_pages is None else max_pages
    all_media: list[dict] = []
    profile_pic_url = None
    cursor = None
    exhausted = False

    for page_num in range(1, max_pages + 1):
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

    # A real IG block/rate-limit always surfaces as `error` above (a
    # distinct HTTP-level failure, e.g. "rate limited (HTTP 401...)").
    # This is a different, quieter case: the id-only fallback paths
    # (extract_grid_timeline / extract_embedded_timeline, used when
    # web_profile_info itself failed) can still succeed with an id but
    # come back with zero posts and no count metadata - the same signal
    # resolve_identity's own is_private/no-visible-posts check already
    # catches on its primary path (see resolve_identity), just reached a
    # different way here. Left alone this would render as a plain "no
    # data yet" dash forever, indistinguishable from "haven't checked
    # yet" - flag it explicitly instead, with the identical message, so
    # it shows as "couldn't check" with a real reason regardless of which
    # path found the id.
    if not used_known_id_fallback and not media and total_post_count is None:
        return None, PRIVATE_OR_NO_POSTS_MESSAGE, was_blocked, None

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
        # Deliberately NOT folded into `was_blocked`: this call is asking
        # for MORE history than we already have (older posts beyond the
        # first page), not the core per-handle check itself - a 401 here
        # just means "can't get further back today," not "we're under a
        # sustained block." Letting it trip the same consecutive-block
        # circuit breaker as a real primary-resolution failure would mean
        # one account's optional deeper backfill stalling could abort the
        # whole run for every other handle, for no real gain (see
        # test_feed_user_failure_is_nonfatal_and_keeps_embedded_posts).
        if feed_error:
            # Don't fail the whole handle over this either - we still have
            # the embedded posts from web_profile_info above, which is
            # real, usable data even if the deeper backfill couldn't
            # complete. Just skip it and move on, no retry.
            print(f"    feed/user pagination failed (keeping embedded posts): {feed_error}", file=sys.stderr)
            feed_exhausted = False
        else:
            media = media + (extra_media or [])
            if profile_pic_url is None:
                profile_pic_url = feed_profile_pic_url

        # feed/user pagination above has turned out (confirmed live,
        # 2026-09-14 - "Unauthorized logged out query" on the GraphQL
        # equivalent, consistent 401s here, and a real anonymous client
        # never even attempts either one) to not work at all for a
        # logged-out session, regardless of retries or backoff - it isn't
        # a rate-limit that clears, it's Instagram not offering deeper
        # pagination to anonymous viewers at all. The reels tab's own
        # embedded connection is a separate, genuinely additional source
        # that IS reachable anonymously (plain page loads only, no XHR/API
        # call - see extract_reels_tab_codes) - not a replacement for
        # feed/user's intended depth, but real, confirmed-working extra
        # coverage in the meantime.
        known_codes = {n.get("code") for n in media if n.get("code")}
        extra_reels = fetch_additional_reels(page, handle, known_codes)
        if extra_reels:
            print(f"    reels tab: found {len(extra_reels)} post(s) not in the main grid", file=sys.stderr)
            media = media + extra_reels

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


def open_context(browser, storage_state: dict | None, proxy_cfg: dict | None):
    """New context + page, carrying `storage_state` (cookies) forward -
    used both for the initial context and every proxy rotation
    afterwards, so the same identity survives switching exit IPs. Falls
    back to a state-less context on a bad `storage_state` (e.g. stale
    shape from a prior architecture) rather than crashing the run."""
    kwargs = dict(user_agent=DESKTOP_UA, viewport={"width": 1280, "height": 800})
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg
    try:
        context = browser.new_context(storage_state=storage_state, **kwargs)
    except Exception as exc:
        print(f"persisted browser state was invalid, starting fresh: {exc}", file=sys.stderr)
        context = browser.new_context(**kwargs)
    return context, context.new_page()


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

    authenticated_state = resolve_authenticated_state()
    if authenticated_state:
        persisted_state = authenticated_state
        print("using a provided authenticated session - see IG_SESSION_STATE_JSON's comment")
    else:
        persisted_state = db.load_session_cookies(conn)
        if persisted_state:
            print("loaded persisted browser state from a prior run")
        else:
            print("no persisted browser state yet - this run will establish and save a fresh one")

    proxy_config = resolve_proxy_config()
    if proxy_config:
        print(f"using proxy {PROXY_SERVER}, rotating IP every {PROXY_ROTATE_EVERY} request(s)")
    else:
        print("no proxy configured - connecting directly")

    with sync_playwright() as p:
        # No proxy at browser-launch time even when one is configured -
        # every context below gets its own per-rotation proxy instead (a
        # per-context proxy overrides the browser-level one for Chromium),
        # so the very first context is never an exception to the rotation.
        browser = p.chromium.launch(headless=HEADLESS)
        context, page = open_context(
            browser, persisted_state, resolve_proxy_config(random_session_id()) if proxy_config else None,
        )

        def rotate_proxy_session(reason: str) -> None:
            nonlocal context, page
            session_id = random_session_id()
            print(f"  rotating proxy session ({reason}) -> {session_id}", file=sys.stderr)
            state_now = context.storage_state()
            page.close()
            context.close()
            context, page = open_context(browser, state_now, resolve_proxy_config(session_id))

        consecutive_blocks = 0
        consecutive_proxy_errors = 0
        consecutive_private_verdicts = 0
        consecutive_blocked_retries = 0
        consecutive_dead_proxy_handles = 0
        cooldowns_used = 0
        stopped_early = False
        stopped_early_reason = "skipped: stopped early after repeated blocking from Instagram this run"
        request_count = 0

        i = 0
        while i < len(handles):
            handle = handles[i]
            display_i = i + 1
            checked_at = datetime.now(tz).isoformat()

            if stopped_early:
                db.fill_gaps_with_error(conn, handle, window, stopped_early_reason, checked_at, today)
                i += 1
                continue

            if request_count > 0:
                time.sleep(MIN_REQUEST_INTERVAL + random.uniform(0, REQUEST_JITTER))

            if proxy_config and request_count > 0 and request_count % PROXY_ROTATE_EVERY == 0:
                rotate_proxy_session(f"{request_count} requests on the last one")

            request_count += 1

            results_by_date, error, was_blocked, profile_pic_url = check_handle(handle, window, tz, page, conn)

            # A dead proxy tunnel (is_proxy_connection_error) means the
            # CONNECTION never worked - nothing about this handle, and
            # nothing Instagram-side, caused it (was_blocked is always
            # False on this path - see resolve_identity's nav_error
            # return). Retrying the same handle on the same dead session
            # would just fail again identically, so force an immediate
            # rotation instead of waiting for the next scheduled one -
            # confirmed live, 2026-09-16: without this, one dead rotated
            # session took out its entire 5-handle batch before the next
            # scheduled rotation finally moved past it.
            if proxy_config and is_proxy_connection_error(error):
                consecutive_proxy_errors += 1
                if consecutive_proxy_errors <= MAX_PROXY_CONNECTION_RETRIES:
                    print(
                        f"  proxy connection failed on {handle} "
                        f"(attempt {consecutive_proxy_errors}/{MAX_PROXY_CONNECTION_RETRIES}): {error}",
                        file=sys.stderr,
                    )
                    rotate_proxy_session(f"previous session dead: {error}")
                    continue  # retry the same handle on the new session, don't advance i or record an error
                print(
                    f"  giving up on {handle} after {MAX_PROXY_CONNECTION_RETRIES} dead proxy "
                    "session(s) in a row - recording as error",
                    file=sys.stderr,
                )
                consecutive_dead_proxy_handles += 1
                if consecutive_dead_proxy_handles >= PROXY_DOWN_HANDLE_LIMIT:
                    print(
                        f"  {consecutive_dead_proxy_handles} handles in a row each exhausted their own "
                        "fresh-session retries - the proxy itself looks comprehensively down (account "
                        "suspended, quota/balance exhausted, or a provider outage), not just unlucky "
                        "individual IPs. Stopping early instead of burning through the rest of the list.",
                        file=sys.stderr,
                    )
                    stopped_early = True
                    stopped_early_reason = "skipped: proxy appeared to be comprehensively down this run"
            else:
                consecutive_dead_proxy_handles = 0

            # A "private or no visible posts" verdict is the same kind of
            # suspect result as a dead proxy tunnel above - confirmed
            # live, 2026-09-16, a soft-blocked exit IP produces this
            # exact verdict for genuinely public, active accounts (see
            # PRIVATE_OR_NO_POSTS_MESSAGE and MAX_PRIVATE_VERDICT_RETRIES).
            # A handful of tracked accounts genuinely are private, so
            # this can't be trusted as an infinite retry signal the way a
            # connection failure is - bounded low instead, and always
            # accepted eventually.
            if proxy_config and error == PRIVATE_OR_NO_POSTS_MESSAGE:
                consecutive_private_verdicts += 1
                if consecutive_private_verdicts <= MAX_PRIVATE_VERDICT_RETRIES:
                    print(
                        f"  {handle} came back private/no-visible-posts "
                        f"(attempt {consecutive_private_verdicts}/{MAX_PRIVATE_VERDICT_RETRIES}) "
                        "- rotating proxy session and retrying before trusting it",
                        file=sys.stderr,
                    )
                    rotate_proxy_session(f"suspect private verdict for {handle}")
                    continue  # retry the same handle on the new session, don't advance i or record an error
                print(
                    f"  {handle} still private/no-visible-posts after {MAX_PRIVATE_VERDICT_RETRIES} "
                    "retries on fresh sessions - accepting the verdict",
                    file=sys.stderr,
                )

            # A real block (was_blocked - a 401/403/429 HTTP response)
            # gets its own immediate fresh-session retry first, exactly
            # like a dead tunnel or a suspect "private" verdict above -
            # see MAX_BLOCKED_RETRIES's comment for the production
            # incident (two isolated 2-in-a-row blocks, neither reaching
            # the 3-in-a-row threshold below) that this per-handle retry
            # is specifically for. Only a handle that exhausts its own
            # retry budget (or has no proxy to rotate to) counts toward
            # consecutive_blocks - the broader signal that several
            # *different* handles, each already having tried a fresh
            # session on their own, are still failing: a real
            # sustained/broad block, not one bad IP.
            if was_blocked:
                consecutive_blocked_retries += 1
                if proxy_config and consecutive_blocked_retries <= MAX_BLOCKED_RETRIES:
                    print(
                        f"  {handle} blocked (attempt {consecutive_blocked_retries}/{MAX_BLOCKED_RETRIES}) "
                        "- rotating proxy session and retrying",
                        file=sys.stderr,
                    )
                    rotate_proxy_session(f"blocked on {handle}")
                    continue  # retry the same handle on the new session, don't advance i or record an error
                consecutive_blocks += 1
            else:
                consecutive_blocks = 0

            if consecutive_blocks >= CONSECUTIVE_BLOCK_LIMIT:
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

            # About to move to a genuinely different handle (success or
            # an accepted error, not a same-handle retry `continue`
            # above) - all three retry budgets are per-handle, not a
            # run-wide streak, so they reset here regardless of whether
            # this handle exhausted any of them.
            consecutive_proxy_errors = 0
            consecutive_private_verdicts = 0
            consecutive_blocked_retries = 0
            i += 1

        # Never persist an authenticated session into the anonymous
        # cookie jar - they're different identities on purpose, and
        # saving one over the other would silently corrupt whichever
        # baseline every future anonymous run relies on.
        if authenticated_state:
            print("authenticated session used for this run only - not saved (anonymous baseline left untouched)")
        else:
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
