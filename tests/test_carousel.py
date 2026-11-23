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
