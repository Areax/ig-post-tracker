# Instagram Post Tracker

Checks a list of public Instagram handles once a day and shows a 14-day
tracker (posted / didn't post, linking to the post) as a static site on
GitHub Pages.

## How it checks posts

No login, no API key, no Facebook Page, no Meta Developer app, no App
Review - just undocumented endpoints, the same ones the
actively-maintained [instaloader](https://instaloader.github.io/) project
uses for anonymous access. Fetched with a **real headless Chromium
browser** (Playwright), not a plain HTTP client - see "Why a real
browser" below for why that distinction turned out to matter a lot.

1. **Navigate to the handle's profile page**, then **resolve it to a
   numeric user id** by calling `web_profile_info` from *inside* that
   already-loaded page (via `page.evaluate` + `fetch()`, not a standalone
   HTTP request - also gets the profile picture in the same call). Some
   accounts hit an Instagram-side bug where this fails outright - observed
   in production: a specific account's request consistently 400'd with
   `"Asset asset://laser.provider/ig_business_category_subvertical has
   been deleted"`, an internal Instagram schema error unrelated to rate
   limiting. For those, the id is instead scraped out of the plain
   profile page fetched with a crawler user-agent via a plain HTTP request
   (this fallback never needed JS - it's just parsing server-rendered
   HTML, so it doesn't go through the browser).
2. **Paginate `/api/v1/feed/user/<id>/`** for the actual post list, always
   using the full `MAX_FEED_PAGES` budget - never stopping early based on
   the oldest post seen in a page, since Instagram interleaves reels/clips
   non-chronologically with regular posts even within a single page
   (confirmed in production: a months-old reel appeared mid-page next to
   recent posts, which fooled an earlier "we've seen an old post, must be
   done" heuristic into missing real in-window posts on a later page).
   Only `more_available`/`next_max_id` being absent (Instagram itself
   saying there's no more data) is trusted to stop early. Each page
   returns ~12 posts regardless of the `count` param requested (confirmed
   empirically at count=24 and count=30, both returned exactly 12) -
   there's no way to request a bigger page, only more pages.
   `web_profile_info`'s own embedded post list is capped at that same
   ~12-post page with no pagination at all, so it's only used for
   id/avatar resolution, never as the post-coverage source.

   `MAX_FEED_PAGES` defaults to **2** (good for backfills/manual runs
   covering a full window from scratch), but the scheduled daily workflow
   overrides it to **1** - a routine daily run only needs to catch
   yesterday's post, which the single most recent page almost always
   covers, and the DB already holds prior days' results from earlier runs.
   Override via the `MAX_FEED_PAGES` repo variable if a tracked account
   posts frequently enough (12+ times/day) that 1 page risks missing
   yesterday's post.

Both steps were cross-checked against ground truth (posts an account
owner confirmed were live that the tracker had missed) and now correctly
recover them.

### Why a real browser

Earlier versions used plain `requests`, then `curl_cffi` (fakes a real
Chrome TLS/JA3 handshake). Both eventually hit a persistent `401`
"please wait a few minutes" block that no amount of pacing, persisted
cookies, or realistic headers fixed. The actual diagnosis came from a
simple real-world test: the same blocked handle loaded fine in a real
browser - including in an **incognito** window, which ruled out "must be
logged in" - while every anonymous HTTP-client approach kept failing on
the identical IP. That isolates the difference to **actual JavaScript
execution**: `curl_cffi` fakes the network-layer handshake, but it never
runs a JS engine, so it can't satisfy any client-side integrity check
Instagram's page might run before/alongside its data calls. Confirmed
empirically: a vanilla (no stealth patch needed) Playwright Chromium
browser, navigating to a currently-blocked handle's profile page and
calling `fetch()` from inside that page, got `web_profile_info` through
when `curl_cffi` was failing on the identical handle/IP.

**Honest caveat**: this fixed `web_profile_info` but not
`/api/v1/feed/user/` in the same test - that endpoint kept failing even
from the real browser, on two different handles, including one barely
touched by that session's testing. The working theory is that feed/user
gets separately throttled on cumulative request volume (it's the
endpoint called most often - 1-2x per handle per run), which a real
browser doesn't fix, only a fingerprint/JS-based block does. If you hit
this, it should recover with time regardless of client.

The browser's full storage state (cookies + localStorage) persists across
runs (stored in the DB's `session_state` table, reloaded and reused next
time) instead of starting from a blank browser profile every day - same
"looks like a returning device" reasoning as before, now backed by an
actual browser profile rather than a bare cookie dict. Because that table
holds real Instagram session cookies and this repo is public,
`data/tracker.db` itself is never committed (see "How it works" below) - in
CI it's persisted as a GitHub Actions cache entry instead, so the browser
session still carries over between scheduled runs without ever touching
git history.

**Read this before relying on it:**
- This is **not an official API**. Meta doesn't publish or support this
  endpoint, and has changed/broken similar endpoints before without notice.
  It can stop working at any time and may need updating.
- It's **against Instagram's Terms of Service** to scrape it, even though
  a 2024 US court ruling (the Bright Data case) found that scraping
  logged-out public data isn't a CFAA violation. Practical enforcement risk
  for this low-volume, read-only use case appears to be low, but it's not
  zero.
- **No pace can be promised to never get blocked**, and a real browser
  doesn't change that for the endpoint that matters most for coverage
  (see "Why a real browser" above - it fixed `web_profile_info` but not
  `feed/user`, which looks separately throttled by volume). Requests
  default to ~1 every 60-75 seconds (~1/min, well under instaloader's
  stated ~6.8/min anonymous budget) - tightened from an earlier 30-40s
  default after a real block was hit at that pace too, mid-run, on a
  fresh IP with no manual-testing history. That block returned **HTTP 401**
  with `require_login: true`, not 429 - worth knowing if you're debugging,
  since a plain rate-limit check might miss it. `check_posts.py` treats
  401 the same as 429. By default (`MAX_COOLDOWNS=0`) a block stops the
  run immediately - marks that handle's real error message (visible in
  the UI, tap/click the `!`) and the rest of the day's handles as
  "couldn't check," rather than failing the whole run. This isn't just
  laziness: a real block was tested to persist across 2 full 15-minute
  cooldown-and-retry cycles before finally clearing on a 3rd attempt -
  what resolved it looked like elapsed time, not the retry itself - so
  waiting by default isn't worth the cost. Set `MAX_COOLDOWNS` > 0 (and
  `COOLDOWN_SECONDS` if you want a different wait) to opt back into
  patient cooldown-and-resume, e.g. for an unattended backfill where
  eventually finishing matters more than finishing fast.
- Individual accounts can still fail even after the fallback (e.g. if
  Instagram also blocks the crawler-UA HTML fetch, or the account is
  private/deleted). These show up as `!` on the tracker - tap/click it to
  see the reason.

## 1. Push this repo to GitHub

```bash
gh repo create ig-post-tracker --public --source=. --remote=origin
git push -u origin main
```

Then enable **GitHub Pages**: repo Settings → Pages → Source: "Deploy from a
branch" → Branch: `main`, folder: `/docs`.

No secrets or GitHub Actions variables are required to get started —
`TRACKER_TIMEZONE` (below) is the only one, and it has a sensible default.
The workflow installs Playwright's Chromium binary itself
(`playwright install --with-deps chromium`) - nothing to set up manually
in the repo. For local runs: `pip install -r scripts/requirements.txt`
then `python3 -m playwright install chromium` once.

## 2. Add/remove handles

Edit `handles.csv`, one handle per line (with or without `@`), and push.
Next daily run picks it up automatically.

## 3. Changing the schedule

The daily job fires twice in UTC (covering both PST and PDT) and a guard
step only lets the real work run within ~20 minutes of `TARGET_LOCAL_TIME`
in `TRACKER_TIMEZONE` (repo variables, both optional — default to `00:30`
and `America/Los_Angeles`). To move it to a very different time of day,
also update the two `cron:` lines in `.github/workflows/daily-check.yml` so
one of them still lands near your new target — GitHub Actions cron values
can't reference repo variables directly.

You can also trigger a run manually any time from the Actions tab
("Daily Instagram post check" → Run workflow).

## How it works

- `data/tracker.db` — SQLite, the **source of truth** across runs. Kept out
  of git (see `.gitignore`) since it holds real Instagram session cookies
  in `session_state` and this repo is public; GitHub Actions checkouts are
  otherwise ephemeral, so CI persists it as a `actions/cache` entry between
  scheduled runs instead of a commit (see `.github/workflows/daily-check.yml`).
  Locally it's just a normal file in `data/` that sticks around between
  runs on your machine. Keeps full history (never pruned), so you can query
  beyond the 14-day window locally. Five tables:
  - `checks` — one row per (handle, day): posted true/false + a single
    permalink, `checked_at`. This is what the static site's snapshot is
    exported from.
    `sqlite3 data/tracker.db "select * from checks where handle = 'torch_boy' order by check_date"`
  - `posts` — every individual post ever fetched, deduped by
    (handle, code): exact `taken_at`/`posted_at` timestamp, `media_type`,
    `product_type`, permalink. Richer than `checks` (which only keeps one
    permalink per day) - this is what you want for debugging "did they
    really not post that day" or "what time did they post."
    `sqlite3 data/tracker.db "select posted_at, product_type, permalink from posts where handle = 'torch_boy' order by taken_at desc"`
  - `identities` — cached handle → numeric user id mapping (see "How it
    checks posts").
  - `profiles` — cached avatar image bytes.
  - `session_state` — the persisted cookie jar reused across runs (see
    "How it checks posts" for why: a fresh cookie-less session every run
    looks more bot-like to Instagram than a returning one with history).

  Writing to `checks` is what makes results self-healing — a failed run
  only fills gaps (`INSERT ... ON CONFLICT DO NOTHING`) rather than
  overwriting previously-good rows. A *successful* run has the same
  protection for dates outside what it actually fetched: a routine
  check only sees the ~12 most recent posts, so for a frequent poster
  that can fall short of the full tracked window — days older than the
  oldest fetched post are left untouched rather than written as a false
  "didn't post" (see `check_handle`'s coverage tracking in
  `scripts/check_posts.py`).
- `scripts/check_posts.py` — reads `handles.csv`, resolves each handle to
  a numeric id then paginates its post feed (see "How it checks posts"),
  paced with retries/backoff/cooldowns, buckets each account's recent
  posts across the whole tracked window in `TRACKER_TIMEZONE`, and writes
  into the DB via `scripts/db.py`.
- `docs/data/history.json` — a **generated snapshot**, exported fresh from
  the DB at the end of every run (just the tracked window, for handles
  currently in `handles.csv`). Never hand-edited, never read back in as
  input — the DB is what matters.
- `docs/index.html` — static page, fetches `docs/data/history.json`
  client-side and renders the tracker grid. No build step, and it never
  hits Instagram or the DB directly — rendering is decoupled from checking.
- **Avatars**: profile photo bytes are cached in the `profiles` table
  (fetch-once, not re-downloaded once present — they rarely change), then
  exported to `docs/data/avatars/<handle>.<ext>` each run. They're stored
  as actual bytes rather than the CDN URL because Instagram's profile pic
  URLs are signed and expire in days (confirmed: a captured URL expired
  ~4 days out) — hotlinking them would silently break the tracker. A
  handle with no cached avatar yet (or a broken image load) falls back to
  an initial-letter badge in the UI.

## Scaling to ~100 handles

Pacing (`MIN_REQUEST_INTERVAL_SECONDS=60` + jitter) targets roughly
1/minute per request — deliberately well under instaloader's stated
anonymous budget, see "Read this before relying on it" above for why.
Each handle costs a fixed `1 + MAX_FEED_PAGES` requests (id resolution +
feed pages) - not "up to", since pagination always uses the full page
budget rather than stopping early (see "How it checks posts" for why).
Add one more request per handle on its first run only, for the one-time
avatar fetch. On top of the pacing sleep, each handle also costs a real
browser page navigation (a few seconds) since v2 - see "Why a real
browser" above.

- **Scheduled daily runs** (`MAX_FEED_PAGES=1`): 2 requests/handle, ~100
  handles takes **roughly 3-3.5 hours** end to end assuming no blocks.
- **Backfills/manual runs** (`MAX_FEED_PAGES=2`, the code default): 3
  requests/handle, ~100 handles takes **roughly 4.5-5 hours**.

These are noticeably longer than an HTTP-only approach would take -
that's the real cost of the real-browser fix. Worth timing an actual run
rather than assuming, especially for a backfill close to GitHub Actions'
6-hour job limit (see below).

The workflow's guard step only gates *when the run starts*, not how long
the check itself takes, so either fits within a single scheduled run.
If Instagram blocks the runner, the default behavior (`MAX_COOLDOWNS=0`) is to stop immediately
rather than wait - so a blocked run actually finishes *faster*, just with
some handles showing as errors instead of results. Set `MAX_COOLDOWNS` > 0
if you'd rather it wait through cooldowns instead (up to
`MAX_COOLDOWNS` × `COOLDOWN_SECONDS` extra).

## Known limitations

- Post coverage is capped at `MAX_FEED_PAGES` (default 2) pages of ~12
  posts each (Instagram's fixed page size - the `count` query param
  doesn't appear to change it) - up to ~24 posts per handle per run. An
  extremely prolific poster (24+ posts in the tracked window) could still
  have their earliest days missed; raise `MAX_FEED_PAGES` if that's a
  real concern for someone you're tracking.
- If a handle is removed from `handles.csv`, its past history stays in
  `history.json` but it drops off the tracker's handle list going forward.
- This relies on undocumented Instagram endpoints. If they stop working,
  `check_posts.py` is the file to look at first — the fix is usually a
  matching update in [instaloader](https://github.com/instaloader/instaloader)
  you can mirror here.
- Resolving a handle now costs a fixed 3 requests (id lookup + 2 feed
  pages), plus 1 more on its first-ever run for the avatar fetch, instead
  of the earlier single request - more accurate, but correspondingly
  slower. See "Scaling to ~100 handles" below.
