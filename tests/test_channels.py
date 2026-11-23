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
