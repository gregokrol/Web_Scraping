"""The website built from approved deals, and its link with Telegram."""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from affiliate_scraper.alerts import DealRules, decide, detect_deals, publish_approved
from affiliate_scraper.cli import main
from affiliate_scraper.db import Database
from affiliate_scraper.site import SiteConfig, build_site, page_url, price_chart_svg, publish_site

from .test_p2_automation import seed_db

BASE = "https://user.github.io/mechiremet/"


def cfg(tmp_path, **kw):
    return SiteConfig(base_url=BASE, telegram_url="https://t.me/mechiremet", out_dir=str(tmp_path / "site"), **kw)


def approved_deal(tmp_path, title="Acme Kettle"):
    db, oid = seed_db(tmp_path)
    db.conn.execute("UPDATE offers SET title=?, image=? WHERE id=?", (title, "https://cdn.example/k.jpg", oid))
    db.conn.commit()
    [a] = detect_deals(db, DealRules())
    return db, oid, a


def test_only_approved_deals_are_public(tmp_path):
    db, oid, a = approved_deal(tmp_path)
    info = build_site(db, cfg(tmp_path))
    assert info["deals"] == 0                                   # pending -> not on the site
    assert "עסקאות חדשות יופיעו כאן בקרוב" in (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    decide(db, [a["id"]], True)
    info = build_site(db, cfg(tmp_path))
    assert info["deals"] == 1 and (tmp_path / "site" / f"deals/{a['id']}.html").exists()


def test_deal_page_content_seo_and_safety(tmp_path):
    db, oid, a = approved_deal(tmp_path, title='Kettle <script>alert(1)</script>')
    decide(db, [a["id"]], True)
    build_site(db, cfg(tmp_path))
    page = (tmp_path / "site" / f"deals/{a['id']}.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page    # escaped
    assert 'rel="sponsored nofollow noopener"' in page and "?ref=me" in page        # affiliate link, marked
    assert "גילוי נאות" in page and "https://t.me/mechiremet" in page              # disclosure + channel CTA
    assert f'<link rel="canonical" href="{BASE}deals/{a["id"]}.html">' in page
    assert '<svg class="chart"' in page                                            # price history chart
    ld = json.loads(page.split('<script type="application/ld+json">')[1].split("</script>")[0])
    assert ld["@type"] == "Product" and ld["offers"]["@type"] == "AggregateOffer"
    index = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert f'href="deals/{a["id"]}.html"' in index and 'dir="rtl"' in index
    sitemap = (tmp_path / "site" / "sitemap.xml").read_text(encoding="utf-8")
    assert f"{BASE}deals/{a['id']}.html" in sitemap
    assert "Sitemap: " + BASE + "sitemap.xml" in (tmp_path / "site" / "robots.txt").read_text(encoding="utf-8")
    assert (tmp_path / "site" / "feed.xml").exists() and (tmp_path / "site" / "logo.png").exists()


def test_stale_price_is_flagged(tmp_path):
    db, oid, a = approved_deal(tmp_path)
    decide(db, [a["id"]], True)
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    db.conn.execute("UPDATE offers SET last_fetched_at=? WHERE id=?", (old, oid))
    db.conn.commit()
    build_site(db, cfg(tmp_path))
    assert "לפני יותר מ-48 שעות" in (tmp_path / "site" / f"deals/{a['id']}.html").read_text(encoding="utf-8")
    assert "המחיר עשוי להשתנות" in (tmp_path / "site" / "index.html").read_text(encoding="utf-8")


def test_chart_needs_two_points():
    assert price_chart_svg([("2026-11-01T00:00:00+00:00", 10)], "ILS") == ""
    svg = price_chart_svg([("2026-11-01T00:00:00+00:00", 10), ("2026-11-05T00:00:00+00:00", 8)], "ILS")
    assert svg.startswith('<svg class="chart"') and "₪10.00" in svg


def test_refuses_to_wipe_the_project(tmp_path):
    project = Path(__file__).resolve().parents[1]
    db, _, _ = approved_deal(tmp_path)
    with pytest.raises(RuntimeError, match="project folder"):
        build_site(db, SiteConfig(out_dir=str(project)))
    with pytest.raises(RuntimeError, match="project folder"):
        publish_site(SiteConfig(out_dir=str(tmp_path / "site"), publish_dir=str(project)))
    assert (project / "main.py").exists()


def test_publish_commits_and_pushes_to_pages_repo(tmp_path):
    db, oid, a = approved_deal(tmp_path)
    decide(db, [a["id"]], True)
    bare = tmp_path / "pages.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    clone = tmp_path / "pages"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(clone), "config", k, v], check=True)
    (clone / "CNAME").write_text("deals.example.co.il", encoding="utf-8")       # kept across publishes
    c = cfg(tmp_path, publish_dir=str(clone))
    build_site(db, c)
    assert publish_site(c) == "published"
    assert publish_site(c) == "no changes"
    files = subprocess.run(["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main"],
                           capture_output=True, text=True, check=True).stdout.split()
    assert {"index.html", f"deals/{a['id']}.html", "CNAME", ".nojekyll", "sitemap.xml"} <= set(files)


class Bot:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None, data=None, files=None):
        self.calls.append(json if json is not None else data)

        class R:
            status_code = 200

            def json(self):
                return {"ok": True, "result": {}}
        return R()


def test_telegram_post_links_to_the_deal_page(tmp_path):
    from affiliate_scraper.telegram import TelegramPublisher
    db, oid, a = approved_deal(tmp_path)
    decide(db, [a["id"]], True)
    bot = Bot()
    tg = TelegramPublisher(token="7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw", channel="@c", session=bot,
                           min_interval=0, env={}, watermark="0")
    c = cfg(tmp_path)
    publish_approved(db, tg.send_event, page_url_for=lambda aid: page_url(c, aid))
    msg = bot.calls[-1]
    url = f"{BASE}deals/{a['id']}.html"
    assert url in (msg.get("caption") or msg.get("text"))
    buttons = msg["reply_markup"]["inline_keyboard"][0]
    assert [b["url"] for b in buttons][-1] == url and len(buttons) == 2


def test_automation_run_builds_site_before_posting(tmp_path, local_site, monkeypatch):
    from .conftest import product_html
    local_site.add("/products/k", product_html("Acme Kettle", "90", currency="ILS", gtin="4006381333931"))
    auto = {"db": str(tmp_path / "r.db"), "targets": [local_site.base + "/products/k"], "delay": 0, "no_cache": True,
            "allow_private_hosts": True, "out_dir": str(tmp_path / "out"), "webhook_url": local_site.base + "/n8n",
            "deals": {"min_deal_score": 0}, "site": {"enabled": True, "base_url": BASE, "out_dir": str(tmp_path / "site")}}
    (tmp_path / "automation.json").write_text(json.dumps(auto), encoding="utf-8")
    assert main(["run"]) == 0
    db = Database(tmp_path / "r.db")
    oid = db.offer_id(local_site.base + "/products/k")
    now = datetime.now(timezone.utc)
    db.conn.executemany("INSERT INTO price_history(offer_id, observed_at, price, currency, price_verified) VALUES (?,?,?,?,1)",
                        [(oid, (now - timedelta(days=d)).isoformat(), 120, "ILS") for d in (28, 18, 9)])
    db.conn.commit()
    db.close()
    assert main(["run"]) == 0
    db = Database(tmp_path / "r.db")
    [alert] = db.query("SELECT id FROM deal_alerts")
    db.close()
    assert main(["approve", str(alert["id"]), "--db", str(tmp_path / "r.db")]) == 0
    assert main(["run"]) == 0
    assert (tmp_path / "site" / f"deals/{alert['id']}.html").exists()
    [ev] = local_site.events("deal.publish")
    assert ev["data"]["page_url"] == f"{BASE}deals/{alert['id']}.html"


def test_site_command_build(tmp_path, capsys):
    db, oid, a = approved_deal(tmp_path)
    decide(db, [a["id"]], True)
    db.close()
    (tmp_path / "automation.json").write_text(json.dumps({"db": str(tmp_path / "a.db"),
                                                          "site": {"out_dir": str(tmp_path / "web")}}), encoding="utf-8")
    assert main(["site", "build"]) == 0
    assert "Built 3 page(s), 1 deal(s)" in capsys.readouterr().out


def test_listing_shows_each_product_once_with_its_best_offer():
    from affiliate_scraper.site import best_per_product
    deals = [{"id": 3, "group_id": "g1", "offer_id": 1, "ended": False, "price": 120.0},
             {"id": 2, "group_id": "g1", "offer_id": 2, "ended": False, "price": 99.0},
             {"id": 1, "group_id": "g1", "offer_id": 3, "ended": True, "price": 80.0},
             {"id": 4, "group_id": "", "offer_id": 9, "ended": False, "price": 10.0}]
    assert [d["id"] for d in best_per_product(deals)] == [2, 4]


def test_relative_time_hebrew():
    from affiliate_scraper.site import _ago
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    ago = lambda **kw: _ago((now - timedelta(**kw)).isoformat(), now)  # noqa: E731
    assert (ago(minutes=1), ago(minutes=5), ago(hours=2), ago(hours=5), ago(days=1), ago(days=2), ago(days=9)) == \
           ("לפני דקה", "לפני 5 דקות", "לפני שעתיים", "לפני 5 שעות", "אתמול", "לפני יומיים", "לפני 9 ימים")


def test_withdrawn_deal_leaves_the_site(tmp_path):
    from affiliate_scraper.alerts import withdraw
    db, oid, a = approved_deal(tmp_path)
    decide(db, [a["id"]], True)
    assert build_site(db, cfg(tmp_path))["deals"] == 1
    calls = []
    [res] = withdraw(db, [a["id"]], lambda chat, msg: calls.append((chat, msg)))
    assert res["ok"] and res["telegram"] == "not posted" and calls == []   # it was never posted to Telegram
    assert build_site(db, cfg(tmp_path))["deals"] == 0
    assert not (tmp_path / "site" / f"deals/{a['id']}.html").exists()
    assert withdraw(db, [999], None)[0]["ok"] is False


def test_site_setup_writes_settings_from_the_clone(tmp_path, monkeypatch, capsys):
    from affiliate_scraper.commands import pages_url_from_remote
    assert pages_url_from_remote("https://github.com/GregoKrol/mechiremet.git\n") == "https://gregokrol.github.io/mechiremet/"
    assert pages_url_from_remote("git@github.com:gregokrol/gregokrol.github.io.git") == "https://gregokrol.github.io/"
    clone = tmp_path / "mechiremet"
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", "https://github.com/gregokrol/mechiremet.git"],
                   check=True)
    (tmp_path / "automation.json").write_text(json.dumps({"db": "x.db", "site": {"enabled": False}}), encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@mechiremet")
    assert main(["site", "setup", "--publish-dir", str(clone)]) == 0
    site = json.loads((tmp_path / "automation.json").read_text(encoding="utf-8"))["site"]
    assert site["enabled"] and site["base_url"] == "https://gregokrol.github.io/mechiremet/"
    assert site["telegram_url"] == "https://t.me/mechiremet" and site["publish_dir"] == clone.resolve().as_posix()
    project = Path(__file__).resolve().parents[1]
    assert main(["site", "setup", "--publish-dir", str(project)]) == 2      # never the project itself
