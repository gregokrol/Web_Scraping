"""Instagram carousel images from verified deals (affiliate_scraper/carousel.py)."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from affiliate_scraper.carousel import caption_text, render, slides_html
from affiliate_scraper.cli import main

NOW = datetime.now(timezone.utc)
DEALS = [{"title": "בקר DualSense <b>לבן</b>", "price": 249.0, "savings_pct": 17, "prev_price": 299.0, "store": "BUG",
          "checked_at": (NOW - timedelta(hours=3)).isoformat(), "lowest90": True, "image": "javascript:alert(1)"}]


def test_slides_say_only_the_deals_facts():
    pages = slides_html(DEALS, "https://t.me/mechiremet")
    assert len(pages) == 3                                             # cover, 1 deal, closing
    deal = pages[1]
    assert "₪249.00" in deal and "חיסכון 17%" in deal and "₪299.00" in deal and "הנמוך ב-90 יום" in deal
    assert "&lt;b&gt;לבן&lt;/b&gt;" in deal                            # text is escaped
    assert "javascript:" not in deal                                   # only https images are used
    assert "ייתכן שחלק מהקישורים" in pages[-1] and "t.me/mechiremet" in pages[-1]
    assert slides_html(DEALS, product_images=False)[1].count("<img") == 1   # just the logo


def test_caption_has_the_facts_disclosure_and_hebrew_prefix():
    cap = caption_text(DEALS, "https://t.me/mechiremet")
    assert "₪249.00 ב-BUG (חיסכון 17%)" in cap and "קישורי שותפים" in cap and "#מחיראמת" in cap


def test_render_makes_instagram_size_pngs(tmp_path):
    pytest.importorskip("playwright")
    if not os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):        # set by conftest when the browser is here
        pytest.skip("needs the local Chromium")
    from PIL import Image
    files = render(slides_html(DEALS, "https://t.me/mechiremet", product_images=False), tmp_path)
    assert [f.name for f in files] == ["slide_01.png", "slide_02.png", "slide_03.png"]
    assert Image.open(files[0]).size == (1080, 1350)


def test_carousel_command_needs_real_deals(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "automation.json").write_text(json.dumps({"db": str(tmp_path / "x.db")}), encoding="utf-8")
    assert main(["carousel"]) == 1 and "No deals for a carousel" in capsys.readouterr().err
    assert main(["carousel", "--count", "9"]) == 2


def test_screens_of_the_real_site(tmp_path, monkeypatch, capsys):
    pytest.importorskip("playwright")
    if not os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
        pytest.skip("needs the local Chromium")
    from PIL import Image

    from affiliate_scraper.db import Database
    from affiliate_scraper.site import SiteConfig, build_site
    monkeypatch.chdir(tmp_path)
    build_site(Database(tmp_path / "s.db"), SiteConfig(base_url="https://u.github.io/s/", out_dir=str(tmp_path / "site")))
    (tmp_path / "automation.json").write_text(json.dumps({"site": {"out_dir": str(tmp_path / "site")}, "out_dir": "out"}),
                                              encoding="utf-8")
    assert main(["screens", "--size", "1080x1920", "--pages", "index.html", "search.html?q=PS5"]) == 0
    shots = sorted((tmp_path / "out" / "screens").glob("*/*.png"))
    assert [s.name for s in shots] == ["index-html.png", "search-html-q-ps5.png"]
    assert Image.open(shots[0]).size == (1080, 1920)
    assert main(["screens", "--size", "big"]) == 2


def test_the_slides_do_not_promise_a_comparison_between_stores():
    """Every store has its own products, so a comparison between stores is rare (the owner, 24.11.2026):
    the cover and the closing slide must not promise one for every deal."""
    deal = {"title": "Acme Headset", "price": 199.0, "store": "באג", "checked_at": "2026-11-25T06:20:00+00:00",
            "savings_pct": 20, "prev_price": 249.0}
    pages = slides_html([deal], telegram_url="https://t.me/example", product_images=False)
    text = "".join(pages) + caption_text([deal], "https://t.me/example")
    assert "השוואה בין חנויות" not in text and "השווינו בין חנויות" not in text
    assert "היסטוריית המחיר" in pages[0] and "מועד בדיקה בכל עסקה" in pages[-1]
