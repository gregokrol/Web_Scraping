"""A store whose SEARCH page says "429 too many requests" is not searched again until the pause ends, and its
next searches start from the word it stopped at - so every search word still gets its turn over the runs.

The pause grows with each 429 in a row: 15 min, 30, 60, 120, up to 180 - and a search that works starts it from
15 again. Why not one long pause: on 24.11.2026 iDigital, Kravitz, Home Center, Shilav and FOX HOME said 429 already
on the FIRST search of every run (12 h after the last one, searches 4 min apart) - it is not the number of
words, their search page simply refuses some requests. A 3 h pause at the first 429 would have left them with no
search results at all; after a 12 min break iDigital answered 4 searches in a row (AirPods among them). Short
pauses that grow get those, and still knock at most ~4 times a run on a store that keeps saying stop.

Why: a store limits its search page, not its product pages (iDigital, 24.11.2026: 115 of 136 searches failed,
every one on the search page, in bursts - each new search started "fresh" and knocked again ~every 30 s on a
store that had already said stop). A longer delay for everything would not fix that and would slow the whole run.

Works for every store, now and in the future - nothing to set up per store. Settings (automation.json):
``rate_limit.search_pause_minutes`` (the first pause, default 15) and ``rate_limit.search_pause_max_minutes``
(default 180: longer than a run, shorter than the 12 h between runs).
State lives in the ``meta`` table: ``search_pause:<domain>`` (until when, UTC ISO), ``search_fails:<domain>``
(429s in a row and when the last one was) and ``search_next:<domain>`` (the word this store starts from next time).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import Database
from .validation import _parse_iso

PAUSE_MINUTES = 15          # the first pause; each further 429 in a row doubles it ...
PAUSE_MAX_MINUTES = 180     # ... up to this
FORGET_HOURS = 6            # a 429 longer than this after the last one starts from the first pause again


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


def _minutes(cfg: dict, key: str, default: float) -> float:
    try:
        return max(1.0, float((cfg.get("rate_limit") or {}).get(key, default)))
    except (TypeError, ValueError):
        return float(default)


def pause_minutes(cfg: dict) -> float:
    """The first pause after a search 429."""
    return _minutes(cfg, "search_pause_minutes", PAUSE_MINUTES)


def pause_max_minutes(cfg: dict) -> float:
    return max(pause_minutes(cfg), _minutes(cfg, "search_pause_max_minutes", PAUSE_MAX_MINUTES))


def next_pause(db: Database, domain: str, minutes: float, max_minutes: float,
               now: Optional[datetime] = None) -> float:
    """Count one more search 429 in a row for this store and return how long to pause it: ``minutes`` doubled for
    each 429 before it in a row, at most ``max_minutes``. The count starts over FORGET_HOURS after the last 429."""
    now = now or datetime.now(timezone.utc)
    key = f"search_fails:{bare(domain)}"
    n, at = 0, None
    try:
        saved = json.loads(_get(db, key) or "{}")
        n, at = int(saved.get("n", 0)), _parse_iso(saved.get("at") or "")
    except (ValueError, TypeError, AttributeError):
        pass
    if not at or now - at > timedelta(hours=FORGET_HOURS):
        n = 0
    n += 1
    _set(db, key, json.dumps({"n": n, "at": now.replace(microsecond=0).isoformat()}))
    return min(minutes * 2 ** (n - 1), max_minutes)


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
                 minutes: float = PAUSE_MINUTES, now: Optional[datetime] = None,
                 max_minutes: float = PAUSE_MAX_MINUTES) -> dict[str, float]:
    """After one search word: a store whose search page said 429 is paused (see :func:`next_pause`) and starts
    from THIS word next time (it got no results for it). A store that searched it fine starts its next pause from
    the first one again and moves on to the next word (also when a run is cut off by its time budget), and after
    the last word starts from the first one again.
    Returns the stores paused now, with the minutes of each one's pause."""
    now = now or datetime.now(timezone.utc)
    hit = search_429(db, after_id)
    paused = {}
    for d in searched:
        key = bare(d)
        if key in hit:
            m = next_pause(db, key, minutes, max_minutes, now)
            _set(db, f"search_pause:{key}", (now + timedelta(minutes=m)).replace(microsecond=0).isoformat())
            if query in words:
                _set(db, f"search_next:{key}", query)
            paused[d] = m
            continue
        _del(db, f"search_fails:{key}")
        if query in words:
            i = words.index(query)
            if i == len(words) - 1:
                _del(db, f"search_next:{key}")
            else:
                _set(db, f"search_next:{key}", words[i + 1])
    return paused
