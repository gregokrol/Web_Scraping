# Affiliate Product Scraper

<div dir="rtl">

כלי פייתון שאוסף **מידע ציבורי על מוצרים** (שם, מחיר, הנחה, דירוג, מספר ביקורות, תמונה, זמינות)
מאתרי מסחר, ממיר את קישורי המוצרים ל**קישורי שותפים (Affiliate)** שלך, מדרג את המוצרים לפי
"כמה כדאי לקדם אותם", ומייצא CSV / JSON ודף HTML / Markdown מוכן לפרסום, כולל גילוי נאות.

## למה זה עובד טוב יותר מגרסה 1

| גרסה 1 (Indeed) | גרסה 2 |
|---|---|
| בחירת שדות לפי class של CSS: נשבר בכל שינוי עיצוב | קורא **נתונים מובנים** (schema.org JSON-LD, microdata, OpenGraph) שהאתרים מפרסמים בשביל גוגל |
| באג: `requests.get(url, headers)` שלח את ה-headers כפרמטרים | Session עם retries, back-off, כיבוד `Retry-After` |
| באג: תמיד שלף את עמוד 0 | גילוי מוצרים דרך sitemap או מעבר על עמודי קטגוריה עם pagination |
| התחזות לדפדפן, בלי השהיה | User-Agent כן, כיבוד `robots.txt` ו-`Crawl-delay`, השהיה בין בקשות |
| `except:` ריק שבולע שגיאות | טיפול שגיאות מפורש ולוגים |
| בלי טסטים | 13 טסטים, כולל טסט מקצה לקצה מול שרת מקומי |

## התקנה

**משתמשי Windows:** יש מדריך מלא להרצה ב-CMD, כולל פתרון תקלות, בקובץ [RUN_ON_WINDOWS.md](RUN_ON_WINDOWS.md).

</div>

```bash
pip install -r requirements.txt
cp affiliate_config.example.json affiliate_config.json   # put your affiliate IDs here
```

<div dir="rtl">

## שימוש

</div>

```bash
# Specific product pages
python main.py https://store.example/products/mug https://store.example/products/cup -c affiliate_config.json

# Whole store via its sitemap: top 30 in-stock products rated 4.2+ with 20+ reviews
python main.py https://store.example --discover sitemap --max-pages 300 \
    --min-rating 4.2 --min-reviews 20 --in-stock-only --top 30 \
    -c affiliate_config.json --formats csv,json,html,md --title "Best coffee gear 2026"

# Follow links from a category page (with pagination)
python main.py https://store.example/collections/coffee --discover links --include "/products/"

# Deals only: at least 20% off, under 100
python main.py -f urls.txt --min-discount 20 --max-price 100 --sort discount
```

<div dir="rtl">

הפלט נכתב לתיקייה `output/`:
* `products_*.csv`: לאקסל / Google Sheets (UTF-8 עם BOM, עברית מוצגת תקין)
* `products_*.json`: לחיבור לאתר, בוט טלגרם, Zapier וכו'
* `products_*.html`: דף כרטיסי מוצר רספונסיבי עם `rel="sponsored nofollow"` וגילוי נאות
* `products_*.md`: לפוסט בבלוג (WordPress, Ghost, Medium)

### הגדרת תוכניות שותפים (`affiliate_config.json`)

* **`params`**: פרמטרים שמתווספים לקישור (Amazon `tag`, eBay `campid`, חנויות Shopify עם `ref`).
* **`template`**: פורמט deep-link של רשתות (Awin, CJ, Impact, Rakuten, AliExpress Portals).
  משתנים: `{url}`, `{url_encoded}`, `{domain}`, `{sku}`.
* **`utm`**: פרמטרי UTM שמתווספים לכל קישור, כדי למדוד ב-Google Analytics.
* **`only_affiliated: true`**: שומר רק מוצרים מדומיינים שיש לך עבורם תוכנית שותפים.

תגי affiliate של אחרים (`tag=`, `ref=`, `aff...`) ופרמטרי מעקב (`utm_*`, `fbclid`, `gclid`) **מוסרים**
מהקישור המקורי לפני שהתג שלך מתווסף.

### ציון "כדאיות קידום" (`score`, 0 עד 100)

דירוג משוקלל לפי מספר הביקורות (עד 40), אחוז הנחה (עד 25), זמינות במלאי (+15, או -30 אם אזל)
ושלמות הנתונים (עד 20). הנוסחה נמצאת ב-`affiliate_scraper/models.py` (`Product.score`) ופשוט לשנות אותה לפי הנישה שלך.

## ⚠️ שימוש חוקי ואחראי: חשוב לקרוא

* **קראו את תנאי השימוש של כל אתר ושל כל תוכנית שותפים.** חלק מהתוכניות **אוסרות** scraping של האתר
  שלהן ומחייבות שימוש ב-API רשמי. **Amazon Associates**, למשל, דורשת להציג מחירים רק דרך
  [Product Advertising API / Creators API](https://affiliate-program.amazon.com/) ואוסרת להציג מחירים ישנים.
  כש-API או product feed רשמי קיים (Amazon, eBay Browse API, AliExpress Affiliate API, product feeds של Awin/CJ/Impact),
  **העדיפו אותו**. הכלי הזה מתאים בעיקר לחנויות שמאשרות זאת ולמוצרים שאין להם feed.
* ה-scraper **מכבד robots.txt כברירת מחדל**. הדגל `--ignore-robots` מיועד רק לאתרים שלכם או לאתרים שנתנו לכם אישור בכתב.
* הכלי **לא** עוקף CAPTCHA, חסימות, התחברות או paywall, ולא מתחזה לדפדפן. אם אתר חוסם, כבדו את זה.
* אל תעתיקו תיאורים ותמונות מלאים כמו שהם: הם עלולים להיות מוגנים בזכויות יוצרים. כתבו תוכן משלכם
  (גם טוב יותר ל-SEO) והשתמשו בתמונות שהתוכנית מספקת לשותפים.
* **גילוי נאות** על קישורי שותפים נדרש לפי חוק (FTC בארה"ב, הגנת הצרכן בישראל ובאיחוד האירופי).
  הייצוא כולל טקסט גילוי, ואפשר לשנות אותו עם `--disclosure`.
* מחירים משתנים, ולכן כדאי להריץ שוב באופן קבוע (cron) ולהציג תאריך עדכון. הייצוא עושה את זה אוטומטית.

</div>

## Project layout

```
affiliate_scraper/
  fetcher.py     polite HTTP: robots.txt, crawl-delay, retries, cache
  discovery.py   find product URLs (XML sitemaps, listing pages + pagination)
  extractors.py  JSON-LD / microdata / OpenGraph / fallback -> Product
  affiliate.py   affiliate link builder (params or network deep-link templates)
  pipeline.py    dedupe, filters, ranking
  exporters.py   CSV, JSON, HTML cards, Markdown
  cli.py         command-line interface
tests/           pytest suite (offline)
```

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
