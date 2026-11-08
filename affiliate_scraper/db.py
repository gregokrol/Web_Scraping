"""SQLite storage: products, offers, price history, sources, crawl runs, affiliate programs.

One file (default ``data/web_scraping.db``), no server needed. Every price is stored
with the time it was actually observed (``fetched_at`` - cache aware) and its source,
so history and "lowest price in 90 days" claims can always be traced.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .attributes import extract_attributes
from .models import Product, now_iso

DEFAULT_DB = "data/web_scraping.db"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    domain TEXT UNIQUE NOT NULL,
    name TEXT,
    type TEXT DEFAULT 'web',
    trust_score REAL,
    shipping_cost REAL,
    warranty_months INTEGER,
    config_json TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    match_key TEXT UNIQUE NOT NULL,
    name TEXT, brand TEXT, model TEXT, gtin TEXT, category TEXT, image TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_products_gtin ON products(gtin);

CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    product_id INTEGER REFERENCES products(id),
    source_id INTEGER REFERENCES sources(id),
    url TEXT UNIQUE NOT NULL,
    domain TEXT, store TEXT, store_url TEXT, sku TEXT, title TEXT,
    price REAL, list_price REAL, currency TEXT, shipping_cost REAL, tax REAL,
    availability TEXT, condition TEXT, color TEXT, size TEXT, warranty_months INTEGER,
    rating REAL, review_count INTEGER, image TEXT, category TEXT,
    affiliate_url TEXT, price_verified INTEGER, validation_status TEXT, validation_issues TEXT,
    source TEXT, source_type TEXT, group_id TEXT, match_confidence REAL,
    deal_score REAL, affiliate_score REAL, deal_status TEXT, affiliate_status TEXT, deal_notes TEXT,
    first_seen TEXT, last_seen TEXT, last_fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_offers_product ON offers(product_id);
CREATE INDEX IF NOT EXISTS ix_offers_domain ON offers(domain);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    command TEXT,
    targets INTEGER, products INTEGER, errors INTEGER,
    status TEXT,
    errors_json TEXT,
    stats_json TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    crawl_run_id INTEGER REFERENCES crawl_runs(id),
    observed_at TEXT NOT NULL,
    price REAL, list_price REAL, currency TEXT, shipping_cost REAL,
    availability TEXT, price_verified INTEGER, source TEXT,
    UNIQUE(offer_id, observed_at)
);
CREATE INDEX IF NOT EXISTS ix_history_offer_time ON price_history(offer_id, observed_at);

CREATE TABLE IF NOT EXISTS affiliate_programs (
    id INTEGER PRIMARY KEY,
    domain TEXT UNIQUE NOT NULL,
    network TEXT,
    params_json TEXT,
    template TEXT,
    commission_rate REAL,              -- fraction, e.g. 0.04 = 4%
    commission_verified INTEGER DEFAULT 0,
    commission_source TEXT,            -- where you verified it (dashboard, contract, date)
    cookie_days INTEGER,
    notes TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS performance (
    id INTEGER PRIMARY KEY,
    url TEXT, domain TEXT,
    clicks INTEGER, conversions INTEGER, revenue REAL, currency TEXT,
    period_start TEXT, period_end TEXT, source TEXT, imported_at TEXT
);

CREATE TABLE IF NOT EXISTS deal_alerts (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                -- new_deal | price_drop | lowest_90d
    fingerprint TEXT UNIQUE NOT NULL,  -- prevents duplicate alerts / posts
    price REAL, currency TEXT,
    deal_score REAL, affiliate_score REAL,
    title TEXT, content_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | published | failed
    created_at TEXT, decided_at TEXT, published_at TEXT, notified_at TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS ix_alerts_offer ON deal_alerts(offer_id, created_at);
"""

OFFER_COLUMNS = ("domain", "store", "store_url", "sku", "price", "list_price", "currency", "shipping_cost", "tax",
                 "availability", "condition", "color", "size", "warranty_months", "rating", "review_count",
                 "image", "category", "affiliate_url", "price_verified", "validation_status", "source",
                 "source_type", "group_id", "match_confidence", "deal_score", "affiliate_score", "deal_notes")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9֐-׿]", "", (s or "").lower())


def product_match_key(p: Product) -> str:
    """Stable identity of a *product* (not an offer). Variants get different keys."""
    if p.gtin:
        key = f"gtin:{p.gtin}"
    elif p.brand and p.model:
        key = f"bm:{_norm(p.brand)}|{_norm(p.model)}"
    else:
        return f"offer:{p.url}"
    a = extract_attributes(p)
    variant = "|".join(filter(None, [a.color, a.condition if a.condition not in ("", "new") else "",
                                     ",".join(f"{k}={v}" for k, v in sorted(a.quantities.items())),
                                     f"pack={a.pack}" if a.pack else ""]))
    return f"{key}#{variant}" if variant else key


class Database:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        if not self.conn.execute("SELECT 1 FROM schema_info").fetchone():
            self.conn.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------ runs
    def start_run(self, command: str, targets: int) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO crawl_runs(started_at, command, targets, status) VALUES (?,?,?,?)",
                            (now_iso(), command, targets, "running"))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, products: int, errors: list, stats: Optional[dict] = None,
                   status: str = "ok") -> None:
        with self.tx() as c:
            c.execute("UPDATE crawl_runs SET finished_at=?, products=?, errors=?, status=?, errors_json=?, stats_json=? "
                      "WHERE id=?", (now_iso(), products, len(errors), status,
                                     json.dumps([e.to_dict() if hasattr(e, "to_dict") else e for e in errors][:500],
                                                ensure_ascii=False), json.dumps(stats or {}), run_id))

    # ------------------------------------------------------------------ sources / programs
    def upsert_source(self, domain: str, **fields) -> int:
        now = now_iso()
        allowed = {"name", "type", "trust_score", "shipping_cost", "warranty_months", "config_json", "enabled"}
        data = {k: v for k, v in fields.items() if k in allowed}
        with self.tx() as c:
            row = c.execute("SELECT id FROM sources WHERE domain=?", (domain,)).fetchone()
            if row:
                if data:
                    sets = ", ".join(f"{k}=?" for k in data)
                    c.execute(f"UPDATE sources SET {sets}, updated_at=? WHERE id=?", (*data.values(), now, row["id"]))
                return int(row["id"])
            cols = ["domain", *data, "created_at", "updated_at"]
            cur = c.execute(f"INSERT INTO sources({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            (domain, *data.values(), now, now))
            return int(cur.lastrowid)

    def sync_stores(self, stores) -> None:
        for s in stores:
            self.upsert_source(s.domain, name=s.name or s.domain, type=s.type, trust_score=s.trust_score,
                               shipping_cost=s.shipping_cost, warranty_months=s.warranty_months,
                               config_json=json.dumps(s.__dict__, ensure_ascii=False), enabled=int(s.enabled))

    def source_trust(self, domain: str) -> Optional[float]:
        row = self.conn.execute("SELECT trust_score FROM sources WHERE ? = domain OR ? LIKE '%.' || domain "
                                "ORDER BY length(domain) DESC LIMIT 1", (domain, domain)).fetchone()
        return row["trust_score"] if row and row["trust_score"] is not None else None

    def upsert_program(self, domain: str, **fields) -> None:
        allowed = {"network", "params_json", "template", "commission_rate", "commission_verified",
                   "commission_source", "cookie_days", "notes"}
        data = {k: v for k, v in fields.items() if k in allowed}
        with self.tx() as c:
            row = c.execute("SELECT id FROM affiliate_programs WHERE domain=?", (domain,)).fetchone()
            if row:
                if data:
                    sets = ", ".join(f"{k}=?" for k in data)
                    c.execute(f"UPDATE affiliate_programs SET {sets}, updated_at=? WHERE id=?",
                              (*data.values(), now_iso(), row["id"]))
            else:
                cols = ["domain", *data, "updated_at"]
                c.execute(f"INSERT INTO affiliate_programs({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          (domain, *data.values(), now_iso()))

    def sync_programs(self, config) -> None:
        """Copy programs from affiliate_config.json. Only keys present in the file are overwritten,
        so commission data entered in the dashboard is kept."""
        for domain, prog in config.programs.items():
            fields: dict = {"network": prog.get("network", ""), "params_json": json.dumps(prog.get("params", {})),
                            "template": prog.get("template", "")}
            if "commission_rate" in prog:
                rate = prog.get("commission_rate")
                fields["commission_rate"] = float(rate) if rate is not None else None
                fields["commission_verified"] = int(bool(prog.get("commission_verified")) and rate is not None)
            for k in ("commission_source", "cookie_days"):
                if k in prog:
                    fields[k] = prog[k]
            self.upsert_program(domain, **fields)

    def programs_as_config(self) -> dict[str, dict]:
        """Programs managed in the dashboard, in affiliate_config format (for link building)."""
        out = {}
        for r in self.query("SELECT * FROM affiliate_programs"):
            prog: dict = {}
            if r["template"]:
                prog["template"] = r["template"]
            params = json.loads(r["params_json"] or "{}")
            if params:
                prog["params"] = params
            if prog:
                prog.update(commission_rate=r["commission_rate"], commission_verified=bool(r["commission_verified"]))
                out[r["domain"]] = prog
        return out

    def program_for(self, domain: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM affiliate_programs WHERE ? = domain OR ? LIKE '%.' || domain "
                                 "ORDER BY length(domain) DESC LIMIT 1", (domain, domain)).fetchone()

    # ------------------------------------------------------------------ products / offers / history
    def _upsert_product(self, c: sqlite3.Connection, p: Product) -> int:
        key = product_match_key(p)
        now = now_iso()
        row = c.execute("SELECT id FROM products WHERE match_key=?", (key,)).fetchone()
        if row:
            c.execute("UPDATE products SET name=COALESCE(NULLIF(?,''),name), brand=COALESCE(NULLIF(?,''),brand), "
                      "model=COALESCE(NULLIF(?,''),model), category=COALESCE(NULLIF(?,''),category), "
                      "image=COALESCE(NULLIF(?,''),image), updated_at=? WHERE id=?",
                      (p.name, p.brand, p.model, p.category, p.image, now, row["id"]))
            return int(row["id"])
        cur = c.execute("INSERT INTO products(match_key,name,brand,model,gtin,category,image,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (key, p.name, p.brand, p.model, p.gtin, p.category, p.image, now, now))
        return int(cur.lastrowid)

    def save_products(self, products: list[Product], run_id: Optional[int] = None) -> dict[str, int]:
        """Upsert offers and append a price observation. Returns {url: offer_id}."""
        ids: dict[str, int] = {}
        now = now_iso()
        with self.tx() as c:
            for p in products:
                product_id = self._upsert_product(c, p)
                src = c.execute("SELECT id FROM sources WHERE ? = domain OR ? LIKE '%.' || domain "
                                "ORDER BY length(domain) DESC LIMIT 1", (p.domain, p.domain)).fetchone()
                values = {col: getattr(p, col) if col != "domain" else p.domain for col in OFFER_COLUMNS}
                values["price_verified"] = int(p.price_verified)
                values.update(product_id=product_id, source_id=src["id"] if src else None, title=p.name,
                              validation_issues=";".join(p.validation_issues), last_seen=now,
                              last_fetched_at=p.fetched_at,
                              deal_status=getattr(p, "deal_status", ""), affiliate_status=getattr(p, "affiliate_status", ""))
                row = c.execute("SELECT id FROM offers WHERE url=?", (p.url,)).fetchone()
                if row:
                    sets = ", ".join(f"{k}=?" for k in values)
                    c.execute(f"UPDATE offers SET {sets} WHERE id=?", (*values.values(), row["id"]))
                    offer_id = int(row["id"])
                else:
                    cols = ["url", *values, "first_seen"]
                    cur = c.execute(f"INSERT INTO offers({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                    (p.url, *values.values(), now))
                    offer_id = int(cur.lastrowid)
                ids[p.url] = offer_id
                if p.price is not None:
                    c.execute("INSERT OR IGNORE INTO price_history(offer_id, crawl_run_id, observed_at, price, list_price, "
                              "currency, shipping_cost, availability, price_verified, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (offer_id, run_id, p.fetched_at, p.price, p.list_price, p.currency, p.shipping_cost,
                               p.availability, int(p.price_verified), p.source))
        return ids

    def update_scores(self, products: list[Product]) -> None:
        with self.tx() as c:
            for p in products:
                c.execute("UPDATE offers SET deal_score=?, affiliate_score=?, deal_status=?, affiliate_status=?, "
                          "deal_notes=?, group_id=?, match_confidence=? WHERE url=?",
                          (p.deal_score, p.affiliate_score, p.deal_status, p.affiliate_status, p.deal_notes,
                           p.group_id, p.match_confidence, p.url))

    def offer_id(self, url: str) -> Optional[int]:
        row = self.conn.execute("SELECT id FROM offers WHERE url=?", (url,)).fetchone()
        return int(row["id"]) if row else None

    def history(self, offer_id: int, since: Optional[str] = None, verified_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM price_history WHERE offer_id=?"
        params: list = [offer_id]
        if since:
            sql += " AND observed_at >= ?"
            params.append(since)
        if verified_only:
            sql += " AND price_verified=1"
        return self.query(sql + " ORDER BY observed_at", params)

    # ------------------------------------------------------------------ performance data
    def import_performance(self, rows: list[dict], source: str) -> int:
        n = 0
        with self.tx() as c:
            for r in rows:
                c.execute("INSERT INTO performance(url, domain, clicks, conversions, revenue, currency, period_start, "
                          "period_end, source, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (r.get("url", ""), r.get("domain", ""), r.get("clicks"), r.get("conversions"),
                           r.get("revenue"), r.get("currency", ""), r.get("period_start", ""), r.get("period_end", ""),
                           source, now_iso()))
                n += 1
        return n

    def performance_for(self, url: str, domain: str) -> Optional[dict]:
        """Aggregate real click/conversion data for an offer URL (preferred) or its domain."""
        for col, value in (("url", url), ("domain", domain)):
            row = self.conn.execute(f"SELECT SUM(clicks) clicks, SUM(conversions) conversions, SUM(revenue) revenue "
                                    f"FROM performance WHERE {col}=? AND {col}<>''", (value,)).fetchone()
            if row and row["clicks"]:
                return {"clicks": row["clicks"], "conversions": row["conversions"] or 0,
                        "revenue": row["revenue"], "level": col}
        return None

    def stats(self) -> dict:
        tables = ("sources", "products", "offers", "price_history", "crawl_runs", "affiliate_programs",
                  "performance", "deal_alerts")
        return {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
