# Web_Scraping v3: Affiliate Deal Finder

<div dir="rtl">

מערכת מקומית ל-Windows שאוספת מידע על מוצרים **ממקורות מורשים**, משווה מחירים בין חנויות, שומרת היסטוריית
מחירים, מזהה עסקאות אמיתיות (ומסמנת הנחות מטעות), מחשבת שני ציונים נפרדים (**Deal Score** לצרכן ו-**Affiliate Score**
לכדאיות מסחרית), מכינה תוכן קידום עם גילוי נאות, ושולחת ל-n8n רק עסקאות שאישרת.

**עיקרון מנחה: דיוק לפני כמות.** מה שלא אומת מסומן כ"לא מאומת", ומה שאין עליו נתונים מקבל `insufficient_data`.
המערכת אף פעם לא מנחשת עמלות, ביקוש או המרות.

> משתמשי Windows: מדריך התקנה והפעלה מלא נמצא בקובץ [RUN_ON_WINDOWS.md](RUN_ON_WINDOWS.md).

## מה יש בגרסה 3

| תחום | יכולות |
|---|---|
| **מקורות (Source Adapters)** | API רשמי (**Amazon Creators API**, **AliExpress Affiliate API**, eBay), **XML של חנויות ישראליות**, Product Feeds (Google Merchant XML, CSV של Awin/CJ וכו'), JSON-LD, Microdata, OpenGraph, סלקטורים של CSS לחנויות שהגדרת, Playwright לאתרים דינמיים שמאפשרים סריקה |
| **אמינות** | Cache עם TTL ותאריך שליפה לכל מחיר, סימון מחירים ישנים, טיפול ב-Timeout/403/429 עם Circuit Breaker, יומן שגיאות לכל URL, הגנת SSRF, מגבלת גודל, `--in-stock-only` מתוקן |
| **אימות נתונים** | מטבע ISO, טווחי מחיר, ספרת ביקורת של GTIN, זמינות, דירוג. לכל מוצר `validation_status` ו-`validation_issues` |
| **השוואת מחירים** | התאמה לפי GTIN, אחר כך מותג+דגם, אחר כך מאפיינים, עם ציון ביטחון. וריאציות (נפח, מארז, צבע, מצב, אחריות, יבואן) לא מאוחדות. מחושבים מינימום, חציון, פער, חיסכון ומחיר כולל משלוח, ומסומן אם ההשוואה חלקית |
| **היסטוריה (SQLite)** | מינימום וחציון ל-30 ול-90 יום, ירידות מחיר, "הנמוך ב-90 יום", בדיקת הנחה מול המחיר הנמוך ב-30 הימים הקודמים |
| **ציונים** | Deal Score ו-Affiliate Score נפרדים, כל אחד 0 עד 100 או `insufficient_data` |
| **אוטומציה** | `run` מלא, Windows Task Scheduler, **פרסום ישיר לערוץ טלגרם**, Webhook ל-n8n עם חתימת HMAC, מניעת התראות כפולות, אישור ידני לפני פרסום |
| **Dashboard** | ממשק מקומי ב-`127.0.0.1`: מוצרים, השוואה, גרף היסטוריה, סינון, ציונים, ניהול חנויות ותוכניות שותפים, אישור עסקאות, סריקה ידנית, ייצוא |

</div>

## Easiest: one-click install (Windows)

Download [`install_windows.bat`](https://raw.githubusercontent.com/gregokrol/Web_Scraping/claude/web-scraping-v3/install_windows.bat)
and double-click it (step-by-step Hebrew guide: [INSTALL.md](INSTALL.md)). It installs Python if needed, the code, the libraries and the config files, then runs the
self-tests and creates a **Web Scraping** desktop shortcut that opens a menu (`start_windows.bat`).

## Quick start (Windows CMD)

```cmd
cd /d C:\Web_Scraping
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements-dev.txt
py -m pytest -q
py main.py --help
```

```cmd
:: 1. one-time setup
copy affiliate_config.example.json affiliate_config.json
copy sources.example.json sources.json
copy automation.example.json automation.json

:: 2. collect (official APIs and feeds first, then public product pages)
py main.py --amazon-search "electric kettle"
py main.py --aliexpress-search "usb c hub"
py main.py --feed https://store.co.il/feeds/products.xml --sources sources.json
py main.py --feed https://merchant.example/google_feed.xml -c affiliate_config.json
py main.py https://store.example --discover sitemap --max-pages 100 --sources sources.json -c affiliate_config.json

:: 3. look at the results
py main.py deals
py main.py history 4006381333931
py main.py dashboard            (open http://127.0.0.1:8765)

:: 4. automate
py main.py run --config automation.json
py main.py alerts               (review)
py main.py approve 12 15
py main.py publish
py main.py schedule --every daily --at 07:43 --create
```

## Commands

| Command | Purpose |
|---|---|
| `py main.py <urls> [options]` | Scrape (v2-compatible). New: `--dry-run`, `--no-cache`, `--cache-ttl-hours`, `--max-age-hours`, `--verified-only`, `--feed`, `--amazon-search`, `--amazon-asin`, `--aliexpress-search`, `--aliexpress-id`, `--ebay-search`, `--ebay-gtin`, `--render js`, `--sources`, `--db`/`--no-db`, `--webhook`, `--sort deal/affiliate` |
| `db [stats\|runs\|errors]` | Database statistics, crawl runs, last errors |
| `history <url\|barcode>` | 30/90-day low & median, drop detection, discount check |
| `deals` | Current deals by Deal or Affiliate Score |
| `import-performance report.csv` | Real clicks/conversions from your network report (feeds the Affiliate Score) |
| `alerts [--detect] [--show ID]` | Deal alerts and the generated content (with the data basis of every claim) |
| `approve ID...` / `reject ID...` | Manual approval |
| `publish [--telegram]` | Post approved deals to your Telegram channel (or n8n), once each |
| `telegram-check` | Verify bot token, channel and admin rights |
| `run --config automation.json` | Full pipeline: scrape all sources, save history, score, detect deals, notify n8n |
| `schedule` | Windows Task Scheduler task (prints by default, `--create` registers) |
| `dashboard` | Local web UI on 127.0.0.1:8765 |

## The two scores

**Deal Score** (consumer value): market position vs other stores 35 · price history 25 · store trust 15 · shipping 10 ·
warranty 5 · confirmed stock 10. It needs a verified price **and** either a market comparison or enough price history.
Otherwise it is `insufficient_data`. Misleading "was" prices are penalised.

**Affiliate Score** (commercial value): Deal Score 40 · **verified** commission 25 · observed demand (review count) 10 ·
audience fit 10 · real conversion rate (30+ clicks) 15. It needs a verified commission or imported conversion data.

Components without data are left out, never guessed. `deal_notes` lists what each score was based on, and
`confidence` shows how much of the model was covered.

## Output files

`output/<name>.csv|json|html|md` (products), `<name>_comparison.csv|json` (cross-store groups),
`<name>_errors.csv` (per-URL error log), `data/web_scraping.db` (SQLite history). CSV files are UTF-8 with BOM, so
Hebrew displays correctly in Excel.

## Rules the system enforces

* robots.txt, Crawl-delay and per-host delays are respected. On 403 or a bot challenge it stops, and it **never bypasses
  CAPTCHA or blocks**. On 429 it honours Retry-After.
* Official APIs and product feeds are preferred. Many programmes (e.g. Amazon Associates) forbid scraping and require
  their API. Check each site's and programme's terms.
* No secrets in code or config: keys come from environment variables (`AMAZON_*`, `ALIEXPRESS_*`, `EBAY_*`,
  `N8N_WEBHOOK_URL`, `WEBHOOK_SECRET`). See RUN_ON_WINDOWS.md section 7 for where to get each key.
* Amazon data is not kept as long-term price history, because Amazon's terms restrict storing product data. Check the
  current terms yourself. `tests/test_no_secrets.py` checks the repository.
* Nothing is published without your approval (`require_approval: true` by default). Each deal is published at most once.
* Generated content states only verified facts, always includes the price date, and always includes an affiliate disclosure.

## Project layout

```
affiliate_scraper/
  sources/        source adapters: amazon (Creators API), aliexpress (Affiliate API), feed (Google XML,
                  generic/Israeli XML, CSV), web, browser (Playwright), ebay
  fetcher.py      polite HTTP: robots, delays, TTL cache, 403/429/timeout handling, SSRF guard
  security.py     URL / input safety
  extractors.py   JSON-LD, microdata, OpenGraph, CSS selectors
  validation.py   price/currency/GTIN/availability/freshness validation
  attributes.py   variant attributes (size, pack, colour, condition, warranty, importer)
  matching.py     cross-store matching + price comparison
  db.py           SQLite schema and storage
  history.py      30/90-day stats, price drops, discount checks
  scoring.py      Deal Score / Affiliate Score
  content.py      fact-based promo content (he/en)
  alerts.py       deal detection, dedupe, approval & publishing
  webhook.py      n8n webhook (HMAC signed)
  telegram.py     direct Telegram channel publishing (Bot API)
  scheduler.py    Windows Task Scheduler
  dashboard/      FastAPI + single-page UI
  cli.py, commands.py
tests/            pytest suite (offline: local HTTP server, fake APIs)
docs/N8N.md       n8n integration guide
docs/UPGRADE_V3.md  v3 spec with implementation status per requirement
```
