"""A store whose SEARCH page says "429 too many requests" is not searched again until the pause ends, and its
next searches start from the word it stopped at - so every search word still gets its turn over the runs.

Why: a store limits its search page, not its product pages (iDigital, 24.11.2026: 115 of 136 searches failed,
every one on the search page, in bursts - each new search started "fresh" and knocked again ~every 30 s on a
store that had already said stop). A longer delay for everything would not fix that and would slow the whole run.

Works for every store, now and in the future - nothing to set up per store. Settings (automation.json):
``rate_limit.search_pause_minutes`` (default 180: longer than a run, shorter than the 12 h between runs).
State lives in the ``meta`` table: ``search_pause:<domain>`` (until when, UTC ISO) and ``search_next:<domain>``
(the search word this store starts from next time).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import Database
from .validation import _parse_iso

PAUSE_MINUTES = 180


def bare(domain: str) -> str:
    return (domain or "").lower().split(":")[0].removeprefix("www.")


def _get(db: Database, key: str) -> Optional[str]:
    row = db.query("SELECT value FROM meta WHERE key=?", (key,))
    return row[0]["value"] if row else None


def _set(db: Database, key: str, value: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def _del(db: Database, key: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM meta WHERE key=?", (key,))


def pause_minutes(cfg: dict) -> float:
    try:
        return float((cfg.get("rate_limit") or {}).get("search_pause_minutes", PAUSE_MINUTES))
    except (TypeError, ValueError):
        return float(PAUSE_MINUTES)


def paused_until(db: Database, domain: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """When the store's search pause ends - None when it is not paused."""
    now = now or datetime.now(timezone.utc)
    until = _parse_iso(_get(db, f"search_pause:{bare(domain)}") or "")
    return until if until and until > now else None


def skip_reason(db: Database, domain: str, query: str, words: list[str],
                now: Optional[datetime] = None) -> str:
    """'' = search this store for ``query`` now; else why not (for the log)."""
    from .content import israel_time
    until = paused_until(db, domain, now)
    if until:
        return f"paused until {israel_time(until.isoformat()).split(', ')[-1]} (its search said 429)"
    nxt = _get(db, f"search_next:{bare(domain)}")
    if nxt and query in words and nxt in words and words.index(query) < words.index(nxt):
        return f"its turn starts at {nxt!r} this time"
    return ""


def last_run_id(db: Database) -> int:
    """The newest crawl run now - the runs of a search are the ones after it (exact, even within one second)."""
    return int(db.query("SELECT COALESCE(MAX(id), 0) n FROM crawl_runs")[0]["n"])


def search_429(db: Database, after_id: int) -> set[str]:
    """The stores whose SEARCH page (the first page of a search run) said 429 in the runs after ``after_id``."""
    hit = set()
    for r in db.query("SELECT command, errors_json FROM crawl_runs WHERE id > ?", (after_id,)):
        first = next((p for p in (r["command"] or "").split() if "://" in p), "")
        for e in json.loads(r["errors_json"] or "[]"):
            if isinstance(e, dict) and e.get("kind") == "http_429" and first and e.get("url") == first:
                hit.add(bare(first.split("/")[2]))
    return hit


def after_search(db: Database, query: str, words: list[str], searched: list[str], after_id: int,
                 minutes: float, now: Optional[datetime] = None) -> list[str]:
    """After one search word: a store whose search page said 429 is paused and starts from THIS word next time
    (it got no results for it). A store that searched it fine moves on to the next word (also when a run is cut
    off by its time budget), and after the last word starts from the first one again.
    Returns the stores paused now."""
    now = now or datetime.now(timezone.utc)
    hit = search_429(db, after_id)
    paused = []
    for d in searched:
        key = bare(d)
        if key in hit:
            _set(db, f"search_pause:{key}", (now + timedelta(minutes=minutes)).replace(microsecond=0).isoformat())
            if query in words:
                _set(db, f"search_next:{key}", query)
            paused.append(d)
        elif query in words:
            i = words.index(query)
            if i == len(words) - 1:
                _del(db, f"search_next:{key}")
            else:
                _set(db, f"search_next:{key}", words[i + 1])
    return paused
