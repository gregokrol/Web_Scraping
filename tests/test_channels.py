"""Stores' public Telegram channels as leads (affiliate_scraper/channels.py)."""

import json
from types import SimpleNamespace

import pytest

from affiliate_scraper.channels import (channel_page, fetch_channel, leads_text, parse_channel_html, parse_post,
                                        save_claims, verified_matches)
from affiliate_scraper.cli import main
from affiliate_scraper.db import Database
from affiliate_scraper.models import Product
from affiliate_scraper.validation import validate_product


def post(pid, lines, links=(), when="2099-01-01T10:00:00+00:00"):
    body = "<br/>".join(lines) + "".join(f'<a href="{h}">link</a>' for h in links)
    return (f'<div class="tgme_widget_message" data-post="ShopIL/{pid}"><div class="tgme_widget_message_text">'
            f'{body}</div><time datetime="{when}"></time></div>')


PAGE = "<html><body>" + "".join([
    post(1, ["🎧", "סאונד חדש!", "אוזניות אלחוטיות Acme WH-500 עם סינון רעשים", "לרכישה מהירה ב-₪1,999", "במקום",
             "₪2,999", "»", "דגם: WH500-BK"], ["https://shop.example/link/wh500", "https://t.me/ShopIL"]),
    post(2, ["לדוגמא:", "מקלדת מכנית Acme K8", "ב-₪249", "במקום", "₪299", "עכבר גיימינג Acme M3", "ב-₪99",
             "במקום", "₪149"], ["https://shop.example/x?a=1&amp;b=2"]),
    post(3, ["מטהרי אוויר", "לכל הדגמים החל מ-₪892 במקום", "₪949"]),          # a range -> not one product
    post(4, ["יום הקפה!", "מכונת קפה ב-₪349"]),                                # lone price -> skipped
    post(5, ["מארז וייפ Acme V2", "ב-₪99", "במקום", "₪149"]),                   # forbidden product
]) + "</body></html>"


def test_parse_pairs_lists_ranges_and_models():
    posts = parse_channel_html(PAGE, "ShopIL")
    got = {p["post_id"]: (p["title"], p["price"], p["was_price"]) for p in posts}
    assert got["1"] == ("אוזניות אלחוטיות Acme WH-500 עם סינון רעשים WH500-BK", 1999.0, 2999.0)
    assert got["2"] == ("מקלדת מכנית Acme K8", 249.0, 299.0) and got["2#2"] == ("עכבר גיימינג Acme M3", 99.0, 149.0)
    assert "3" not in got and "4" not in got
    one = next(p for p in posts if p["post_id"] == "1")
    assert one["link"] == "https://shop.example/link/wh500" and one["post_url"] == "https://t.me/ShopIL/1"
    assert next(p for p in posts if p["post_id"] == "2")["link"] == "https://shop.example/x?a=1&b=2"
    assert parse_post("") == []


def test_claims_stored_once_without_forbidden_products(tmp_path):
    db = Database(tmp_path / "c.db")
    posts = parse_channel_html(PAGE, "ShopIL")
    assert save_claims(db, posts) == 3                       # the vape post is not stored
    assert save_claims(db, posts) == 0                       # the same posts again: nothing new
    titles = [r["title"] for r in db.query("SELECT title FROM channel_claims")]
    assert not any("וייפ" in t for t in titles)
    # claims never become verified offers
    assert db.query("SELECT COUNT(*) n FROM offers")[0]["n"] == 0


def test_leads_show_the_store_price_as_such_and_our_verified_price(tmp_path):
    db = Database(tmp_path / "c.db")
    p = validate_product(Product(url="https://bug.example/k8", name="Acme K8 מקלדת מכנית אלחוטית", price=239.0,
                                 currency="ILS", availability="InStock", source="json-ld", source_type="json-ld",
                                 store="Bug"))
    db.save_products([p])
    save_claims(db, parse_channel_html(PAGE, "ShopIL"))
    [m] = verified_matches(db, "מקלדת מכנית Acme K8")
    assert m["price"] == 239.0
    assert verified_matches(db, "עכבר גיימינג Acme M3") == []          # no shared model -> no false match
    text = leads_text(db)
    assert "לא נבדק בדף המוצר" in text and "(לפי החנות)" in text and "נבדק אצלנו: Bug ₪239.00" in text
    assert text.index("Acme M3") < text.index("Acme K8")                # bigger stated discount first


def test_channel_names_are_checked():
    assert channel_page("t.me/KSPcoil") == "https://t.me/s/KSPcoil"
    assert channel_page("https://t.me/payngoil") == "https://t.me/s/payngoil"
    with pytest.raises(ValueError):
        channel_page("../../etc")


def test_fetch_reads_the_public_page_only():
    calls = []
    fake = SimpleNamespace(get=lambda url: calls.append(url) or SimpleNamespace(status=200, text=PAGE))
    assert len(fetch_channel(fake, "ShopIL")) == 4 and calls == ["https://t.me/s/ShopIL"]


def test_channels_command_add_list_remove(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "automation.json").write_text("{}", encoding="utf-8")
    assert main(["channels", "add", "t.me/KSPcoil", "payngoil", "bad name!"]) == 0
    assert json.loads((tmp_path / "automation.json").read_text(encoding="utf-8"))["telegram_channels"] == \
        ["KSPcoil", "payngoil"]
    assert main(["channels", "list"]) == 0 and "https://t.me/KSPcoil" in capsys.readouterr().out
    assert main(["channels", "remove", "payngoil"]) == 0
    assert json.loads((tmp_path / "automation.json").read_text(encoding="utf-8"))["telegram_channels"] == ["KSPcoil"]


def test_single_line_only_price_but_not_ranges_or_discounts():
    assert parse_post("מפזר חום קרמי רק 149.90₪?!\nמבצעי פברואר") == \
        [{"title": "מפזר חום קרמי", "price": 149.9, "was_price": None}]
    assert parse_post("טלוויזיה חדשה במחיר מפתיע החל מ-359.90₪!") == []
    assert parse_post("עד 500 ₪ הנחה בקניית סמארטפונים נבחרים") == []


def test_brand_title_beats_a_slogan_and_leads_stay_in_the_niche(tmp_path):
    items = parse_post("פאוץ קומפקטי למברגה מבית Acme\nכל מי שעובד עם מברגה יודע כמה זה קריטי\n"
                       "לרכישה ב-₪49\nבמקום\n₪85")
    assert items == [{"title": "פאוץ קומפקטי למברגה מבית Acme", "price": 49.0, "was_price": 85.0}]
    db = Database(tmp_path / "c.db")
    page = "<html><body>" + "".join([
        post(1, ["גרבי חורף פליז Acme", "ב-₪25", "במקום", "₪39"]),             # not in the niche
        post(2, ["אוזניות אלחוטיות Acme TWS-5", "ב-₪154", "במקום", "₪219"]),
    ]) + "</body></html>"
    save_claims(db, parse_channel_html(page, "ShopIL"))
    text = leads_text(db)
    assert "TWS-5" in text and "גרבי" not in text
    assert "גרבי" in leads_text(db, niche_only=False)


def test_a_later_cheaper_post_is_a_drop_told_once(tmp_path):
    from affiliate_scraper.channels import mark_drops_told, new_drops
    db = Database(tmp_path / "c.db")
    first = "<html><body>" + post(10, ["שעון חכם Acme Runner 265", "ב-₪1,490", "במקום", "₪1,829"],
                                  when="2099-01-01T10:00:00+00:00") + "</body></html>"
    later = "<html><body>" + post(20, ["שעון ריצה חכם Acme Runner 265 GPS", "ב-₪1,290", "במקום", "₪1,829"],
                                  when="2099-01-05T10:00:00+00:00") + "</body></html>"
    save_claims(db, parse_channel_html(first, "ShopIL"))
    assert new_drops(db) == []
    save_claims(db, parse_channel_html(later, "ShopIL"))
    [d] = new_drops(db)
    assert (d["price"], d["prev_price"]) == (1290.0, 1490.0)          # the same product (model "265"), cheaper
    from affiliate_scraper.channels import drop_text
    text = drop_text(db, d)
    assert "ירד שוב" in text and "₪1,290.00" in text and "₪1,490.00" in text
    mark_drops_told(db, [d["id"]])
    assert new_drops(db) == []                                        # told once


def test_old_table_on_the_owners_computer_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE channel_claims (id INTEGER PRIMARY KEY, channel TEXT NOT NULL, post_id TEXT NOT NULL, "
                "post_url TEXT, posted_at TEXT, title TEXT, price REAL, was_price REAL, currency TEXT DEFAULT 'ILS', "
                "link TEXT, fetched_at TEXT, UNIQUE(channel, post_id))")
    con.execute("INSERT INTO channel_claims(channel, post_id, title, price) VALUES ('ShopIL','1','מקלדת Acme K8',249)")
    con.commit()
    con.close()
    from affiliate_scraper.channels import ensure_schema
    db = Database(path)
    ensure_schema(db)
    [row] = db.query("SELECT item_key, drop_notified, auto_posted_at, published_at FROM channel_claims")
    assert row["item_key"] and row["drop_notified"] == 0 and row["auto_posted_at"] is None and row["published_at"] is None


def test_channel_store_on_the_website_is_marked_as_the_stores_price(tmp_path):
    from pathlib import Path

    from affiliate_scraper.site import SiteConfig, build_site
    db = Database(tmp_path / "c.db")
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    page = "<html><body>" + post(1, ["מקלדת מכנית גיימינג Acme K8", "ב-₪249", "במקום", "₪299"], when=now) + \
        "</body></html>"
    save_claims(db, parse_channel_html(page, "KSPcoil"))
    cfg = SiteConfig(base_url="https://u.github.io/s/", out_dir=str(tmp_path / "site"))
    build_site(db, cfg)
    out = Path(cfg.out_dir)
    assert "מוצרים לפי ערוץ החנות" in (out / "stores" / "index.html").read_text(encoding="utf-8")
    cat = (out / "stores" / "tg-kspcoil" / "gaming.html").read_text(encoding="utf-8")
    assert "KSP" in cat and "לא נבדק אצלנו בדף המוצר" in cat and "ההנחות הגדולות (לפי החנות)" in cat
    assert "עסקה שנבדקה" not in cat
    # the description in Google / link previews must not say "checked" either
    import re
    for f in (out / "stores" / "tg-kspcoil.html", out / "stores" / "tg-kspcoil" / "gaming.html"):
        desc = re.search(r'<meta name="description" content="([^"]*)"', f.read_text(encoding="utf-8")).group(1)
        assert "לא נבדקו אצלנו" in desc and "במחירים שנבדקו" not in desc, desc
        page = f.read_text(encoding="utf-8")
        assert "נבדקו במועד המצוין" not in page                  # the footer line does not contradict it
    assert "גיימינג ב-KSP" in cat and "בKSP" not in cat            # a hyphen before a Latin name


def test_publish_claim_posts_the_stores_price_as_such(tmp_path):
    from affiliate_scraper.commands import publish_claim
    db = Database(tmp_path / "c.db")
    save_claims(db, parse_channel_html(PAGE, "KSPcoil"))
    sent = []
    tg = SimpleNamespace(channel="@deals", admin_chat="42",
                         send_menu=lambda chat, text, buttons: sent.append((chat, text, buttons)))
    cid = db.query("SELECT id FROM channel_claims WHERE post_id='1'")[0]["id"]
    assert publish_claim(db, cid, tg, publish=False) == "תצוגה מקדימה נשלחה אליך."
    assert publish_claim(db, cid, tg, publish=True).startswith("✅ פורסם בערוץ")
    (c1, preview, _), (c2, public, buttons) = sent
    assert c1 == "42" and c2 == "@deals"
    assert "לפי ערוץ KSP" in public and "לא נבדק אצלנו" in public and "גילוי נאות" in public
    assert buttons == [("לפרטים ורכישה ›", "https://shop.example/link/wh500")]


def test_same_product_across_posts():
    from affiliate_scraper.channels import same_product
    assert same_product("שעון חכם Garmin Forerunner 265", "שעון ריצה Garmin Forerunner 265 Music GPS")
    assert not same_product("שעון חכם Garmin Forerunner 265", "שעון חכם Garmin Forerunner 165")
    assert not same_product("אוזניות Philips TAH60", "אוזניות Philips TAK4200")
    assert not same_product("מכונת קפה", "מכונת קפה")                   # no brand/model words: never guessed


def test_hebrew_prefix_before_a_name():
    from affiliate_scraper.site import heb
    assert heb("ב", "KSP") == "ב-KSP" and heb("ב", "באג") == "בבאג" and heb("ש", "iDigital") == "ש-iDigital"


def test_post_photo_is_kept_and_old_rows_are_refreshed(tmp_path):
    photo = ('<div class="tgme_widget_message" data-post="ShopIL/7"><a class="tgme_widget_message_photo_wrap" '
             'style="width:100%;background-image:url(\'https://cdn4.telesco.pe/file/abc.jpg\')"></a>'
             '<div class="tgme_widget_message_text">פאוץ קומפקטי למברגה<br/>ב-₪49<br/>במקום<br/>₪85</div>'
             '<time datetime="2099-01-01T10:00:00+00:00"></time></div>')
    db = Database(tmp_path / "c.db")
    save_claims(db, parse_channel_html(f"<html><body>{photo}</body></html>", "ShopIL"))
    better = photo.replace("פאוץ קומפקטי למברגה", "פאוץ למברגה מבית Acme")
    assert save_claims(db, parse_channel_html(f"<html><body>{better}</body></html>", "ShopIL")) == 0   # not new...
    [row] = db.query("SELECT title, image FROM channel_claims")
    assert row["title"] == "פאוץ למברגה מבית Acme"                       # ...but refreshed
    assert row["image"] == "https://cdn4.telesco.pe/file/abc.jpg"


# ---------------------------------------------------------------- channels_auto: big drops posted by themselves
def _drop(db, pid, title, old, new, hours_ago=2, channel="ShopIL", prev_days=5):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    first = post(pid, [title, f"ב-₪{old:,}", "במקום", f"₪{old * 2:,}"],
                 when=(now - timedelta(days=prev_days)).isoformat())
    later = post(pid + 1000, [title, f"ב-₪{new:,}", "במקום", f"₪{old * 2:,}"],
                 when=(now - timedelta(hours=hours_ago)).isoformat())
    save_claims(db, parse_channel_html(f"<html><body>{first}</body></html>", channel))
    save_claims(db, parse_channel_html(f"<html><body>{later}</body></html>", channel))


class FakeChannelTG:
    def __init__(self, channel="@deals_il"):
        self.channel, self.admin_chat, self.posts, self.notes = channel, "42", [], []

    def send_menu(self, chat_id, text, buttons, **kw):
        self.posts.append((chat_id, text))

    def notify_admin(self, text):
        self.notes.append(text)


SMART = {"auto_publish": {"enabled": True}, "channels_auto": {"enabled": True}}


def test_big_niche_drop_is_posted_by_itself_once_a_day(tmp_path):
    from affiliate_scraper.channels import new_drops
    from affiliate_scraper.commands import auto_channel_posts
    db = Database(tmp_path / "c.db")
    _drop(db, 1, "מקלדת מכנית Acme K8", 250, 190)            # -24%, computers
    _drop(db, 2, "עכבר גיימינג Acme M3", 200, 150)            # -25%, gaming - but only 1 a day
    tg = FakeChannelTG()
    left = auto_channel_posts(SMART, db, tg, new_drops(db))
    assert len(tg.posts) == 1 and tg.posts[0][0] == "@deals_il"
    assert "לא נבדק אצלנו" in tg.posts[0][1]                  # the store's price, said so in the post
    assert len(left) == 1 and tg.notes and "פורסם בערוץ לבד" in tg.notes[0]
    assert auto_channel_posts(SMART, db, FakeChannelTG(), left) == left        # the day's quota is used


@pytest.mark.parametrize("title,old,new,hours,why", [
    ("מקלדת מכנית Acme K8", 250, 230, 2, "only -8%"),
    ("שמיכת תינוק Acme Soft", 250, 150, 2, "not the niche"),
    ("מקלדת מכנית Acme K8", 250, 150, 72, "an old post"),
    ("מארז וייפ גיימינג Acme V2", 250, 150, 2, "forbidden product"),
])
def test_what_is_not_posted_by_itself(tmp_path, title, old, new, hours, why):
    from affiliate_scraper.channels import new_drops
    from affiliate_scraper.commands import auto_channel_posts
    db = Database(tmp_path / "c.db")
    _drop(db, 1, title, old, new, hours_ago=hours)
    drops = new_drops(db)
    tg = FakeChannelTG()
    assert auto_channel_posts(SMART, db, tg, drops) == drops and tg.posts == [], why


def test_manual_approval_or_rule_off_means_buttons_only(tmp_path):
    from affiliate_scraper.channels import new_drops
    from affiliate_scraper.commands import auto_channel_posts
    db = Database(tmp_path / "c.db")
    _drop(db, 1, "מקלדת מכנית Acme K8", 250, 150)
    drops = new_drops(db)
    for cfg in ({}, {"require_approval": True}, {**SMART, "channels_auto": {"enabled": False}},
                {"auto_publish": {"enabled": True}}):             # smart approval alone does NOT switch it on
        tg = FakeChannelTG()
        assert auto_channel_posts(cfg, db, tg, drops) == drops and tg.posts == []
    tg = FakeChannelTG()
    assert auto_channel_posts({"require_approval": False, "channels_auto": {"enabled": True}}, db, tg, drops) == []
    assert len(tg.posts) == 1


def test_auto_post_is_careful(tmp_path, monkeypatch):
    from affiliate_scraper.channels import new_drops
    from affiliate_scraper.commands import auto_channel_posts, publish_claim
    db = Database(tmp_path / "c.db")
    # the earlier post is 3 months old -> "-40%" against it would mislead
    _drop(db, 1, "מקלדת מכנית Acme K8", 250, 150, prev_days=90)
    tg = FakeChannelTG()
    assert auto_channel_posts(SMART, db, tg, new_drops(db)) and tg.posts == []
    # the agent manager runs -> never posts
    db2 = Database(tmp_path / "d.db")
    _drop(db2, 1, "עכבר גיימינג Acme M3", 200, 150)
    monkeypatch.setenv("MECHIR_AGENT_RUN", "1")
    tg = FakeChannelTG()
    assert auto_channel_posts(SMART, db2, tg, new_drops(db2)) and tg.posts == []
    monkeypatch.delenv("MECHIR_AGENT_RUN")
    # you posted it yourself today (button) -> not again by itself this week
    [d] = new_drops(db2)
    publish_claim(db2, d["id"], FakeChannelTG(), publish=True)
    tg = FakeChannelTG()
    assert auto_channel_posts(SMART, db2, tg, new_drops(db2)) and tg.posts == []


def test_auto_post_text_is_calm_and_claims_nothing_unchecked(tmp_path):
    from affiliate_scraper.channels import new_drops
    from affiliate_scraper.commands import auto_channel_posts
    db = Database(tmp_path / "c.db")
    p = validate_product(Product(url="https://shop.example/k8", name="מקלדת מכנית Acme K8", price=199.0,
                                 currency="ILS", availability="InStock", source="json-ld", source_type="json-ld"))
    db.save_products([p])                                     # a word-match a person never looked at
    _drop(db, 1, "מקלדת מכנית Acme K8", 250, 150)
    from affiliate_scraper.channels import post_text
    drops = new_drops(db)
    assert "נבדק אצלנו:" in post_text(db, drops[0], "x")               # the match is real; only auto hides it
    tg = FakeChannelTG()
    auto_channel_posts(SMART, db, tg, drops)
    [(_, text)] = tg.posts
    assert text.startswith("📉") and "נבדק אצלנו:" not in text and "לא נבדק אצלנו" in text and "גילוי נאות" in text


def test_channels_auto_switch(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "automation.json"
    cfg.write_text(json.dumps({"telegram_channels": ["ShopIL"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["channels", "auto", "status"]) == 0 and "OFF" in capsys.readouterr().out
    assert main(["channels", "auto", "on"]) == 0
    out = capsys.readouterr().out
    assert "ON" in out and "MANUAL" in out                    # approval is manual -> says nothing will happen yet
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["channels_auto"]["enabled"] is True and saved["telegram_channels"] == ["ShopIL"]
    assert main(["channels", "auto", "off"]) == 0
    assert json.loads(cfg.read_text(encoding="utf-8"))["channels_auto"]["enabled"] is False


@pytest.mark.parametrize("text,expected", [
    ("אוזניות Acme\nב-₪199\nבמקום ₪299\nקוד קופון TG50 - ₪50 הנחה בקנייה מעל ₪500!", "קוד קופון TG50 - ₪50 הנחה בקנייה מעל ₪500"),
    ("מקלדת\n• בקוד KSP10 קבלו 10% הנחה", "בקוד KSP10 קבלו 10% הנחה"),
    ("קופונים חדשים בקרוב, הישארו מעודכנים", ""),          # no code and no amount -> not a coupon
    ("קוד קופון TG50 רק היום! מהרו לפני שייגמר", "קוד קופון TG50"),      # the store's pressure is not repeated
    ("קופון ₪50 עד 27.11 🔥 נשארו רק 3", "קופון ₪50 עד 27.11"),          # a real date stays
    ("קופון בלעדי לחברי ערוץ KSP", ""), ("קופון USB חדש", ""),            # no code, no amount
    ("קוד קופון TG50 " + "מילה " * 40, "קוד קופון TG50 " + " ".join(["מילה"] * 25) + "…"),
    ("מקלדת Acme K8\nב-₪249\nבמקום ₪299", ""),
])
def test_coupon_is_read_word_for_word(text, expected):
    from affiliate_scraper.channels import coupon_of
    assert coupon_of(text) == expected


def test_coupon_reaches_the_post_the_owner_and_the_website(tmp_path):
    from affiliate_scraper.channels import drop_text, new_drops, post_text
    db = Database(tmp_path / "c.db")
    page = "<html><body>" + post(7, ["מקלדת מכנית Acme K8", "ב-₪249", "במקום", "₪299",
                                     "קוד קופון TG50 - ₪50 הנחה ברכישה מהטלגרם"]) + "</body></html>"
    save_claims(db, parse_channel_html(page, "ShopIL"))
    [r] = [dict(x) for x in db.query("SELECT * FROM channel_claims")]
    assert r["coupon"] == "קוד קופון TG50 - ₪50 הנחה ברכישה מהטלגרם"
    text = post_text(db, r, "גילוי נאות")
    assert "🎟️ קופון לפי ערוץ" in text and "TG50" in text and "התנאים לפי החנות" in text
    assert "₪199" not in text                                  # no "price after coupon" is worked out
    _drop(db, 100, "עכבר גיימינג Acme M3", 200, 150)
    assert "קופון" not in drop_text(db, new_drops(db)[0])      # no coupon in that post -> nothing said
    from affiliate_scraper.site import _claimed_row
    row = _claimed_row({**r, "url": r["link"] or r["post_url"], "store": "ShopIL", "currency": "ILS", "matches": []})
    assert "TG50" in row and "התנאים לפי החנות" in row


def test_coupon_line_is_not_the_product_name_and_a_removed_coupon_goes(tmp_path):
    db = Database(tmp_path / "c.db")
    first = post(8, ["קוד קופון TG50 - ₪50 הנחה", "מקלדת מכנית Acme K8", "ב-₪249", "במקום", "₪299"])
    save_claims(db, parse_channel_html(f"<html><body>{first}</body></html>", "ShopIL"))
    [r] = db.query("SELECT title, coupon FROM channel_claims")
    assert "קופון" not in r["title"] and r["coupon"].startswith("קוד קופון TG50")
    edited = post(8, ["מקלדת מכנית Acme K8", "ב-₪249", "במקום", "₪299"])     # the store took the coupon out
    save_claims(db, parse_channel_html(f"<html><body>{edited}</body></html>", "ShopIL"))
    [r] = db.query("SELECT coupon FROM channel_claims")
    assert r["coupon"] == ""
