"""'We checked this sale': facts only, from our checks or from the store's own channel posts (salecheck.py)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from affiliate_scraper.channels import parse_channel_html, save_claims
from affiliate_scraper.db import Database
from affiliate_scraper.models import Product
from affiliate_scraper.salecheck import (_FORBIDDEN_WORDS, buttons_for, candidates, channel_findings, offer_findings,
                                         record, rules_from)
from affiliate_scraper.validation import validate_product

from .test_channels import post

NOW = datetime.now(timezone.utc)


def offer(db, history, price, listed, url="https://shop.example/k", name="קומקום חשמלי Acme K5", store="Acme Shop"):
    """A product we check: ``history`` = [(days ago, price)], the current check at ``price`` with "was" ``listed``."""
    p = validate_product(Product(url=url, name=name, price=price, list_price=listed, currency="ILS",
                                 availability="InStock", source="json-ld", source_type="json-ld", store=store))
    db.save_products([p])
    oid = db.offer_id(url)
    db.conn.execute("DELETE FROM price_history WHERE offer_id=?", (oid,))
    rows = [(oid, (NOW - timedelta(days=d)).isoformat(), v, "ILS") for d, v in history]
    rows.append((oid, NOW.isoformat(), price, "ILS"))
    db.conn.executemany("INSERT INTO price_history(offer_id, observed_at, price, currency, price_verified) "
                        "VALUES (?,?,?,?,1)", rows)
    db.conn.execute("UPDATE offers SET last_fetched_at=? WHERE id=?", (NOW.isoformat(), oid))
    db.conn.commit()
    return oid


RULES = rules_from({})


def clean(text):
    return not any(w in text.lower() for w in _FORBIDDEN_WORDS)


def test_up_then_a_sale_back_to_the_old_price_is_told_with_dates(tmp_path):
    db = Database(tmp_path / "a.db")
    offer(db, [(30, 100), (20, 100), (12, 130), (5, 130)], price=100, listed=149)
    [f] = candidates(db, RULES)
    t = f["text"]
    assert t.startswith("🔍 בדקנו את המבצע: קומקום חשמלי Acme K5")
    assert '₪100 "במקום ₪149" (33% הנחה)' in t and "עלה ל-₪130" in t and "זהה למחיר" in t
    assert "את ₪149 לא ראינו שם אף פעם. הכי גבוה שראינו: ₪130" in t
    assert "בלי פרשנות" in t and clean(t)
    assert buttons_for(f) == [("לדף המוצר בחנות ›", "https://shop.example/k")]      # no affiliate link on a note


def test_a_was_price_never_seen_and_no_lower_price_now(tmp_path):
    db = Database(tmp_path / "a.db")
    offer(db, [(30, 100), (20, 100), (10, 100)], price=100, listed=199)        # flat ₪100 for a month
    [f] = offer_findings(db, RULES)
    assert f["pattern"] is None and f["never_listed"] and f["flat"]
    assert "המחיר שם היה ₪100 בכל הבדיקות" in candidates(db, RULES)[0]["text"]


@pytest.mark.parametrize("history,price,listed,why", [
    ([(30, 130), (20, 130), (10, 130)], 100, 130, "a real drop: cheaper than ever before"),
    ([(5, 100), (3, 130)], 100, 149, "too little history (5 days)"),
    ([(30, 100), (20, 130), (10, 130)], 100, 110, "the store claims only 9%"),
    ([(30, 140), (20, 150), (10, 150)], 100, 150, "the was price was real and the price is lower now"),
])
def test_what_is_not_a_finding(tmp_path, history, price, listed, why):
    db = Database(tmp_path / "a.db")
    offer(db, history, price, listed)
    assert offer_findings(db, RULES) == [], why


def channel_posts(db, items, channel="ShopIL"):
    """[(days ago, price, was)] of one product in a store's channel."""
    html = "".join(post(i + 1, ["שעון חכם Acme Runner 265", f"ב-₪{p:,}", "במקום", f"₪{w:,}"],
                        when=(NOW - timedelta(days=d, hours=1)).isoformat()) for i, (d, p, w) in enumerate(items))
    save_claims(db, parse_channel_html(f"<html><body>{html}</body></html>", channel))


def test_a_channel_rise_then_sale_is_told_from_the_stores_own_posts(tmp_path):
    db = Database(tmp_path / "a.db")
    channel_posts(db, [(25, 1000, 1499), (12, 1300, 1499), (0, 1000, 1499)])
    [f] = channel_findings(db, RULES)
    [c] = candidates(db, RULES)
    t = c["text"]
    assert "לפי הפוסטים של" in t and "לא בדקנו אותם בדף המוצר" in t and clean(t)
    assert '₪1,000 "במקום ₪1,499"' in t and "₪1,300" in t and "זהה למחיר" in t
    labels = [b for b, _ in buttons_for(c)]
    assert labels[0] == "הפוסט של היום ›" and len(labels) == 3      # the evidence: the store's three posts


def test_a_channel_price_that_only_went_down_is_no_finding(tmp_path):
    db = Database(tmp_path / "a.db")
    channel_posts(db, [(25, 1300, 1499), (12, 1200, 1499), (0, 1000, 1499)])
    assert channel_findings(db, RULES) == []


def test_limits_per_day_per_store_and_per_product(tmp_path):
    db = Database(tmp_path / "a.db")
    hist = [(30, 100), (20, 100), (12, 130), (5, 130)]
    offer(db, hist, 100, 149, url="https://a.example/1", name="קומקום Acme K1", store="A")
    offer(db, hist, 100, 149, url="https://a.example/2", name="מטחנה Acme G2", store="A")     # same store
    offer(db, hist, 100, 149, url="https://b.example/3", name="טוסטר Acme T3", store="B")
    offer(db, hist, 100, 149, url="https://c.example/4", name="מיקסר Acme M4", store="C")
    todo = candidates(db, RULES)
    assert [f["store"] for f in todo] == ["A", "B"]                  # 2 a day, one store once
    for f in todo:
        record(db, f, published=True)
    assert candidates(db, RULES) == []                               # the day's 2 are used
    later = NOW + timedelta(hours=25)
    assert [f["store"] for f in candidates(db, RULES, now=later)] == ["C"]   # A and B: not again this week


class FakeTG:
    def __init__(self):
        self.channel, self.admin_chat, self.sent, self.notes = "@deals_il", "42", [], []

    def send_menu(self, chat_id, text, buttons, **kw):
        self.sent.append((chat_id, text, buttons))
        return {"message_id": 700 + len(self.sent)}

    def notify_admin(self, text):
        self.notes.append(text)


def test_the_run_publishes_by_itself_and_tells_you(tmp_path, monkeypatch):
    from affiliate_scraper.commands import sale_checks
    db = Database(tmp_path / "a.db")
    offer(db, [(30, 100), (20, 100), (12, 130), (5, 130)], 100, 149)
    for cfg in ({"price_warnings": {"enabled": False}},):
        tg = FakeTG()
        assert sale_checks(cfg, db, tg) == [] and tg.sent == []
    monkeypatch.setenv("MECHIR_AGENT_RUN", "1")
    tg = FakeTG()
    assert sale_checks({}, db, tg) == [] and tg.sent == []
    monkeypatch.delenv("MECHIR_AGENT_RUN")
    tg = FakeTG()
    [f] = sale_checks({}, db, tg)
    assert tg.sent[0][0] == "@deals_il" and tg.sent[0][1] == f["text"]
    assert len(tg.notes) == 1 and "פורסמה בערוץ לבד" in tg.notes[0] and "מחק את הפוסט" in tg.notes[0]
    [row] = db.query("SELECT published_at, published_ref FROM price_warnings")
    assert row["published_at"] and json.loads(row["published_ref"])["telegram"]["message_id"] == 701
    tg = FakeTG()
    assert sale_checks({}, db, tg) == [] and tg.sent == []           # once


def test_with_auto_off_only_you_see_it(tmp_path):
    from affiliate_scraper.commands import sale_checks
    db = Database(tmp_path / "a.db")
    offer(db, [(30, 100), (20, 100), (12, 130), (5, 130)], 100, 149)
    tg = FakeTG()
    [f] = sale_checks({"price_warnings": {"auto": False}}, db, tg)
    assert tg.sent == [] and len(tg.notes) == 1 and f["text"] in tg.notes[0] and "לא פורסמה" in tg.notes[0]


def test_a_product_named_fake_fur_is_fine_but_no_word_about_intent(tmp_path):
    from affiliate_scraper.salecheck import text_for
    db = Database(tmp_path / "a.db")
    offer(db, [(30, 100), (20, 100), (12, 130), (5, 130)], 100, 149, name="Fake fur blanket Acme F1")
    [f] = candidates(db, RULES)
    assert f["text"].startswith("🔍 בדקנו את המבצע: Fake fur")
    with pytest.raises(ValueError):
        text_for({**f, "store": "חנות מזויפת"})


def test_sale_check_command_lists_only(tmp_path, monkeypatch, capsys):
    from affiliate_scraper.cli import main
    db = Database(tmp_path / "a.db")
    offer(db, [(30, 100), (20, 100), (12, 130), (5, 130)], 100, 149)
    db.close()
    (tmp_path / "automation.json").write_text(json.dumps({"db": str(tmp_path / "a.db")}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["sale-check"]) == 0
    out = capsys.readouterr().out
    assert "בדקנו את המבצע" in out and "1 note(s) for the next run (published by themselves" in out
    assert Database(tmp_path / "a.db").query("SELECT COUNT(*) n FROM price_warnings")[0]["n"] == 0


def test_a_rise_and_back_is_never_also_posted_as_a_deal(tmp_path):
    """The same product may not get a deal post AND a sale-check note: the deal side skips it."""
    from affiliate_scraper.alerts import DealRules, detect_deals
    db = Database(tmp_path / "a.db")
    offer(db, [(30, 100), (25, 100), (20, 130), (12, 130), (5, 130)], 100, 149)
    assert detect_deals(db, DealRules(min_savings_pct=5)) == []
    assert len(candidates(db, RULES)) == 1


def test_back_to_a_recent_price_is_no_drop_even_without_a_was_price(tmp_path):
    """100 -> 130 -> 100 with no "was" price shown: no note (nothing claimed) and no "price drop" deal."""
    from affiliate_scraper.alerts import DealRules, detect_deals
    db = Database(tmp_path / "a.db")
    offer(db, [(30, 100), (25, 100), (20, 130), (12, 130), (5, 130)], 100, None)
    assert detect_deals(db, DealRules(min_savings_pct=5)) == [] and offer_findings(db, RULES) == []


def test_a_real_drop_is_still_a_deal(tmp_path):
    from affiliate_scraper.alerts import DealRules, detect_deals
    db = Database(tmp_path / "a.db")
    offer(db, [(30, 130), (25, 130), (20, 130), (12, 130), (5, 130)], 100, 130)
    [d] = detect_deals(db, DealRules(min_savings_pct=5))
    assert d["kind"] == "price_drop" and offer_findings(db, RULES) == []


def test_a_deal_we_already_posted_gets_no_note(tmp_path):
    db = Database(tmp_path / "a.db")
    oid = offer(db, [(30, 100), (20, 100), (12, 130), (5, 130)], 100, 149)
    db.conn.execute("INSERT INTO deal_alerts(offer_id, kind, fingerprint, price, currency, title, status, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)", (oid, "top_deal", "x", 100, "ILS", "t", "published", NOW.isoformat()))
    db.conn.commit()
    assert offer_findings(db, RULES) == []


def test_a_channel_rise_and_back_is_not_auto_posted_as_a_deal(tmp_path):
    from affiliate_scraper.channels import AUTO_DEFAULTS, auto_new_post_candidates, auto_post_candidates, new_drops
    db = Database(tmp_path / "a.db")
    channel_posts(db, [(25, 1000, 1499), (12, 1300, 1499), (0, 1000, 1499)])
    rules = {**AUTO_DEFAULTS, "enabled": True, "niches": []}
    assert auto_post_candidates(db, new_drops(db), rules, NOW) == [] and auto_new_post_candidates(db, rules, NOW) == []
    assert len(channel_findings(db, RULES)) == 1
