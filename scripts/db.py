"""SQLite persistence for check_posts.py results.

data/tracker.db is the source of truth across runs - GitHub Actions
checkouts are ephemeral, so this file is committed to the repo the same
way docs/data/history.json already was. docs/data/history.json becomes a
generated snapshot for the static site (just the tracked window, for the
handles currently in handles.csv) - it's derived from the DB every run,
never hand-edited, never read back in as input.

Keeping full history in the DB (rather than pruning it like the JSON
snapshot) means you can inspect posting history beyond the 14-day window
locally, e.g.:
    sqlite3 data/tracker.db "select * from checks where handle = 'torch_boy' order by check_date"
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    handle TEXT NOT NULL,
    check_date TEXT NOT NULL,
    status TEXT NOT NULL,
    posted INTEGER,
    permalink TEXT,
    message TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (handle, check_date)
);

CREATE TABLE IF NOT EXISTS profiles (
    handle TEXT PRIMARY KEY,
    avatar BLOB NOT NULL,
    content_type TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identities (
    handle TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    handle TEXT NOT NULL,
    code TEXT NOT NULL,
    taken_at INTEGER,
    posted_at TEXT,
    media_type INTEGER,
    product_type TEXT,
    permalink TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (handle, code)
);

CREATE TABLE IF NOT EXISTS session_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cookies_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

EXT_BY_CONTENT_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def load_session_cookies(conn: sqlite3.Connection) -> dict | None:
    """Returns the persisted browser state (Playwright's storage_state
    format: {"cookies": [...], "origins": [...]}, not just a flat cookie
    dict) from the last run, or None if there isn't one yet. Reusing state
    across runs - rather than starting a fresh browser profile every day -
    is what makes this look like a returning device to Instagram instead
    of a new one appearing daily. The column name (cookies_json) predates
    the switch to full storage_state; it holds whatever JSON-serializable
    dict is passed in, format-agnostic.
    """
    row = conn.execute("SELECT cookies_json FROM session_state WHERE id = 1").fetchone()
    return json.loads(row[0]) if row else None


def save_session_cookies(conn: sqlite3.Connection, cookies: dict, updated_at: str) -> None:
    conn.execute(
        """
        INSERT INTO session_state (id, cookies_json, updated_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            cookies_json = excluded.cookies_json,
            updated_at = excluded.updated_at
        """,
        (json.dumps(cookies), updated_at),
    )
    conn.commit()


def save_posts(conn: sqlite3.Connection, handle: str, posts: list[dict], fetched_at: str) -> None:
    """Persists every raw fetched post (not just the one-per-day summary
    `checks` keeps) - the actual time posted, media type, etc. Deduped by
    (handle, code) so re-fetching the same post across runs just refreshes
    fetched_at rather than creating duplicates. This is what lets you
    answer questions like "what time did they post" or "did they post
    more than once that day" later, without needing a fresh API call.
    """
    rows = [
        (
            handle,
            p["code"],
            p.get("taken_at"),
            p.get("posted_at"),
            p.get("media_type"),
            p.get("product_type"),
            p.get("permalink"),
            fetched_at,
        )
        for p in posts
        if p.get("code")
    ]
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO posts (handle, code, taken_at, posted_at, media_type, product_type, permalink, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(handle, code) DO UPDATE SET
            taken_at = excluded.taken_at,
            posted_at = excluded.posted_at,
            media_type = excluded.media_type,
            product_type = excluded.product_type,
            permalink = excluded.permalink,
            fetched_at = excluded.fetched_at
        """,
        rows,
    )
    conn.commit()


def get_user_id(conn: sqlite3.Connection, handle: str) -> str | None:
    row = conn.execute("SELECT user_id FROM identities WHERE handle = ?", (handle,)).fetchone()
    return row[0] if row else None


def save_user_id(conn: sqlite3.Connection, handle: str, user_id: str, resolved_at: str) -> None:
    conn.execute(
        """
        INSERT INTO identities (handle, user_id, resolved_at)
        VALUES (?, ?, ?)
        ON CONFLICT(handle) DO UPDATE SET
            user_id = excluded.user_id,
            resolved_at = excluded.resolved_at
        """,
        (handle, user_id, resolved_at),
    )
    conn.commit()


def has_avatar(conn: sqlite3.Connection, handle: str) -> bool:
    row = conn.execute("SELECT 1 FROM profiles WHERE handle = ?", (handle,)).fetchone()
    return row is not None


def save_avatar(conn: sqlite3.Connection, handle: str, content: bytes, content_type: str, fetched_at: str) -> None:
    conn.execute(
        """
        INSERT INTO profiles (handle, avatar, content_type, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(handle) DO UPDATE SET
            avatar = excluded.avatar,
            content_type = excluded.content_type,
            fetched_at = excluded.fetched_at
        """,
        (handle, content, content_type, fetched_at),
    )
    conn.commit()


def export_avatars(conn: sqlite3.Connection, handles: list[str], output_dir: Path) -> dict[str, str]:
    """Writes cached avatar bytes to output_dir/<handle>.<ext> for each handle that has one.

    Returns {handle: filename} for handles that got a file written - used to
    tell the frontend which handles actually have an avatar available.
    """
    if not handles:
        return {}

    output_dir.mkdir(parents=True, exist_ok=True)
    placeholders = ",".join("?" * len(handles))
    rows = conn.execute(
        f"SELECT handle, avatar, content_type FROM profiles WHERE handle IN ({placeholders})",
        handles,
    ).fetchall()

    written = {}
    for handle, avatar, content_type in rows:
        ext = EXT_BY_CONTENT_TYPE.get(content_type, "jpg")
        filename = f"{handle}.{ext}"
        (output_dir / filename).write_bytes(avatar)
        written[handle] = filename
    return written


def upsert_ok(conn: sqlite3.Connection, handle: str, results_by_date: dict, checked_at: str) -> None:
    """Authoritative write: replaces any existing row for these dates."""
    rows = [
        (handle, d, r["status"], int(r["posted"]), r.get("permalink"), None, checked_at)
        for d, r in results_by_date.items()
    ]
    conn.executemany(
        """
        INSERT INTO checks (handle, check_date, status, posted, permalink, message, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(handle, check_date) DO UPDATE SET
            status = excluded.status,
            posted = excluded.posted,
            permalink = excluded.permalink,
            message = excluded.message,
            checked_at = excluded.checked_at
        """,
        rows,
    )
    conn.commit()


def fill_gaps_with_error(conn: sqlite3.Connection, handle: str, window: list[date], message: str, checked_at: str) -> None:
    """Only fills dates with no existing row - never overwrites previously-good data."""
    rows = [(handle, d.isoformat(), "error", None, None, message, checked_at) for d in window]
    conn.executemany(
        """
        INSERT INTO checks (handle, check_date, status, posted, permalink, message, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(handle, check_date) DO NOTHING
        """,
        rows,
    )
    conn.commit()


def export_window(conn: sqlite3.Connection, handles: list[str], window: list[date]) -> dict:
    """Builds the same {handles, days} shape the static site's history.json has always had."""
    window_isos = [d.isoformat() for d in window]
    days: dict[str, dict] = {d: {} for d in window_isos}

    if not handles:
        return {"handles": handles, "days": days}

    placeholders_h = ",".join("?" * len(handles))
    placeholders_d = ",".join("?" * len(window_isos))
    rows = conn.execute(
        f"""
        SELECT handle, check_date, status, posted, permalink, message
        FROM checks
        WHERE handle IN ({placeholders_h}) AND check_date IN ({placeholders_d})
        """,
        (*handles, *window_isos),
    ).fetchall()

    for handle, check_date, status, posted, permalink, message in rows:
        if status == "error":
            days[check_date][handle] = {"status": "error", "message": message}
        else:
            entry = {"status": "ok", "posted": bool(posted)}
            if posted and permalink:
                entry["permalink"] = permalink
            days[check_date][handle] = entry

    return {"handles": handles, "days": days}
