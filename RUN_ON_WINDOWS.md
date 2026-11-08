# הרצה ב-Windows (CMD), גרסה 3

<div dir="rtl">

מדריך להתקנה, הפעלה, אוטומציה ופתרון תקלות ב-Windows 11 עם Python 3.11 ומעלה.

## תוכן

1. [התקנה (פעם אחת)](#1-התקנה-פעם-אחת)
2. [קבצי הגדרות](#2-קבצי-הגדרות)
3. [איסוף מוצרים](#3-איסוף-מוצרים)
4. [תוצאות, היסטוריה וציונים](#4-תוצאות-היסטוריה-וציונים)
5. [ממשק הניהול (Dashboard)](#5-ממשק-הניהול-dashboard)
6. [אוטומציה: הרצה מתוזמנת, n8n ואישור ידני](#6-אוטומציה-הרצה-מתוזמנת-n8n-ואישור-ידני)
7. [מפתחות API ומשתני סביבה](#7-מפתחות-api-ומשתני-סביבה)
8. [ההבדלים בין CMD לדוגמאות Linux](#8-ההבדלים-בין-cmd-לדוגמאות-linux)
9. [פתרון תקלות](#9-פתרון-תקלות)

---

## 1. התקנה (פעם אחת)

**Python 3.11 ומעלה:** מורידים מ-[python.org](https://www.python.org/downloads/) ומסמנים בהתקנה **"Add python.exe to PATH"**. בודקים:

</div>

```cmd
py --version
```

<div dir="rtl">

**הורדת הקוד:** עם Git (מומלץ, כדי שיהיה קל לעדכן):

</div>

```cmd
cd /d C:\
git clone -b claude/web-scraping-v3 https://github.com/gregokrol/Web_Scraping.git
```

<div dir="rtl">

אפשר גם בלי Git: בדף ה-branch ב-GitHub לוחצים על **Code** ואז **Download ZIP**, מחלצים ל-`C:\Web_Scraping`
ומשנים את שם התיקייה ל-`Web_Scraping` אם צריך.

> הגרסה החדשה נמצאת כרגע ב-branch `claude/web-scraping-v3`. אחרי מיזוג ל-`main` אפשר יהיה להוריד בלי לציין branch.

**סביבה וירטואלית (venv):** מומלץ, כדי שהספריות לא יתנגשו עם פרויקטים אחרים.

</div>

```cmd
cd /d C:\Web_Scraping
py -m venv .venv
.venv\Scripts\activate
py -m pip install --upgrade pip
py -m pip install -r requirements-dev.txt
```

<div dir="rtl">

אחרי `activate` מופיע `(.venv)` בתחילת השורה. **בכל חלון CMD חדש** צריך להריץ שוב `cd /d C:\Web_Scraping` ואת `.venv\Scripts\activate`.

**בדיקה שהכול עובד:**

</div>

```cmd
py -m pytest -q
py main.py --help
```

<div dir="rtl">

צפוי לראות בסוף שורה עם `passed` בלי `failed`. אם Playwright לא מותקן, יופיע `1 skipped` וזה תקין.

**אופציונלי, לאתרים דינמיים (JavaScript):**

</div>

```cmd
py -m pip install -r requirements-js.txt
py -m playwright install chromium
```

<div dir="rtl">

---

## 2. קבצי הגדרות

</div>

```cmd
copy affiliate_config.example.json affiliate_config.json
copy sources.example.json sources.json
copy automation.example.json automation.json
notepad affiliate_config.json
```

<div dir="rtl">

| קובץ | מה יש בו |
|---|---|
| `affiliate_config.json` | מזהי השותפים שלך (`params` או `template`), UTM, **עמלה** (`commission_rate`), ו-`commission_verified: true` רק אחרי שבדקת בלוח הבקרה של התוכנית, וקהל יעד (`audience`) |
| `sources.json` | חנויות: סוג (`web`/`feed`/`browser`), אמינות 0 עד 1 (הערכה שלך), עלות משלוח ידועה, אחריות, סלקטורים לחנויות בלי נתונים מובנים, כתובות התחלה |
| `automation.json` | מה האוטומציה סורקת, ספי עסקאות, שפת התוכן, `require_approval` (ברירת מחדל `true`) |

שלושת הקבצים מוחרגים מ-Git (`.gitignore`), כך שהם לא יעלו ל-GitHub בטעות. **לא כותבים בהם סיסמאות או מפתחות API**
(ראו [סעיף 7](#7-מפתחות-api-ומשתני-סביבה)).

---

## 3. איסוף מוצרים

**סדר עדיפות:** API רשמי ו-Product Feed קודמים לדפי מוצר ציבוריים.

</div>

```cmd
:: product feed (Google Merchant XML / Awin / CJ CSV) - URL or local file
py main.py --feed https://merchant.example/google_feed.xml -c affiliate_config.json
py main.py --feed C:\feeds\awin.csv -c affiliate_config.json

:: official eBay API (keys in environment variables - section 7)
py main.py --ebay-gtin 4006381333931
py main.py --ebay-search "electric kettle 1.7l"

:: public product pages
py main.py https://store.example/products/kettle -c affiliate_config.json
py main.py https://store.example --discover sitemap --max-pages 100 --sources sources.json -c affiliate_config.json

:: dynamic site that allows crawling
py main.py https://store.example/p/1 --render js
```

<div dir="rtl">

**פרמטרים חשובים:**

| פרמטר | מה עושה |
|---|---|
| `--dry-run` | מראה מה ייסרק ומה robots.txt מתיר, **בלי** להוריד דפי מוצר ובלי לכתוב קבצים או למסד הנתונים |
| `--no-cache` | תמיד מוריד מחדש |
| `--cache-ttl-hours 6` | תוקף ה-Cache (ברירת מחדל 12) |
| `--max-age-hours 24` | מחיר ישן יותר מזה מסומן כלא מאומת |
| `--in-stock-only` | רק מוצרים שהחנות **אישרה** שהם במלאי |
| `--verified-only` | רק מחירים מאומתים |
| `--sort deal` / `--sort affiliate` | מיון לפי הציונים |
| `--delay 3` | שניות בין בקשות לאותו אתר |
| `--no-db` | בלי שמירה להיסטוריה |

שבירת שורה ב-CMD נעשית עם `^`:

</div>

```cmd
py main.py https://store.example --discover sitemap --max-pages 100 ^
  --in-stock-only --verified-only --sort deal --top 30 ^
  -c affiliate_config.json --sources sources.json --formats csv,html,md
```

<div dir="rtl">

---

## 4. תוצאות, היסטוריה וציונים

</div>

```cmd
explorer output
py main.py deals
py main.py deals --sort affiliate
py main.py history https://store.example/products/kettle
py main.py history 4006381333931
py main.py db runs
py main.py db errors
```

<div dir="rtl">

| קובץ | תוכן |
|---|---|
| `output\<name>.csv` | כל המוצרים, כולל `price_verified`, `validation_status`, `deal_score`, `affiliate_score`, `deal_notes`. נפתח באקסל עם עברית תקינה |
| `output\<name>_comparison.csv` | מוצרים זהים בכמה חנויות: מינימום, חציון, חיסכון, `partial` ו-`uncertain` |
| `output\<name>_errors.csv` | יומן שגיאות לכל כתובת |
| `data\web_scraping.db` | מסד הנתונים (SQLite) עם ההיסטוריה |

**איך לקרוא את הציונים:**
- `insufficient_data` פירושו שאין מספיק מידע אמין לציון. זה מכוון: עדיף על פני ציון מומצא.
- **Deal Score** צריך מחיר מאומת ועוד השוואה לחנויות אחרות או היסטוריה של לפחות 3 תצפיות לאורך 7 ימים.
- **Affiliate Score** צריך עמלה שסימנת כמאומתת, או נתוני המרות שייבאת:

</div>

```cmd
py main.py import-performance C:\reports\awin_october.csv --source "Awin Oct 2026"
```

<div dir="rtl">

(הקובץ צריך עמודות `url` או `domain`, ואז `clicks` ו-`conversions`, ואם יש גם `revenue`.)

---

## 5. ממשק הניהול (Dashboard)

</div>

```cmd
py main.py dashboard
```

<div dir="rtl">

פותחים בדפדפן את **http://127.0.0.1:8765**. מה אפשר לעשות שם:
- מוצרים ועסקאות, עם סינון לפי קטגוריה וחנות
- גרף היסטוריית מחירים
- השוואת מחירים בין חנויות
- התראות ואישור עסקאות
- ניהול חנויות ותוכניות שותפים
- סריקה ידנית
- ייצוא ל-CSV או JSON

הממשק עובד רק מהמחשב שלך (`127.0.0.1`) ואין בו התחברות, ולכן לא כדאי להריץ אותו עם `--host 0.0.0.0`.
עוצרים אותו עם `Ctrl+C`.

---

## 6. אוטומציה: הרצה מתוזמנת, n8n ואישור ידני

**ריצה מלאה אחת:** סורקת את כל המקורות שהוגדרו, שומרת היסטוריה, מחשבת ציונים, מזהה עסקאות ושולחת התראה ל-n8n.

</div>

```cmd
py main.py run --config automation.json --dry-run
py main.py run --config automation.json
```

<div dir="rtl">

**אישור ידני (ברירת המחדל):** עסקה חדשה נשארת במצב `pending` ולא מתפרסמת עד שתאשר אותה.

</div>

```cmd
py main.py alerts
py main.py alerts --show 12
py main.py approve 12 15
py main.py reject 13
py main.py publish
```

<div dir="rtl">

`alerts --show` מציג את התוכן שנוצר, ולצד כל טענה בו את הנתון שעליו היא מבוססת.
`publish` שולח ל-n8n רק עסקאות מאושרות, וכל עסקה פעם אחת בלבד. התראה על אותה עסקה לא תישלח שוב,
אלא אם המחיר ירד עוד.

**תזמון ב-Windows Task Scheduler:**

</div>

```cmd
py main.py schedule --every daily --at 07:43
py main.py schedule --every daily --at 07:43 --create
py main.py schedule --every hourly --hours 6 --at 00:10 --create
py main.py schedule --remove
```

<div dir="rtl">

הפקודה יוצרת קובץ `run_automation.bat`, ו-Task Scheduler מפעיל אותו. הקובץ משתמש ב-Python שהריץ את הפקודה, ולכן מריצים `schedule` כשה-venv פעיל.
יומן הריצות נכתב לקובץ `logs\automation.log`. כדי לבדוק את המשימה: מקש Windows, מקלידים "Task Scheduler",
ומחפשים בספרייה את `WebScrapingAffiliate`.

**n8n:** הוראות מלאות בקובץ [docs/N8N.md](docs/N8N.md).

---

## 7. מפתחות API ומשתני סביבה

מפתחות **לא** נשמרים בקוד ולא בקבצי ההגדרות. מגדירים אותם פעם אחת עם `setx` ופותחים חלון CMD חדש:

</div>

```cmd
setx EBAY_CLIENT_ID "your-client-id"
setx EBAY_CLIENT_SECRET "your-client-secret"
setx EBAY_CAMPAIGN_ID "your-epn-campaign-id"
setx N8N_WEBHOOK_URL "http://localhost:5678/webhook/web-scraping"
setx WEBHOOK_SECRET "a-long-random-string"
```

<div dir="rtl">

בדיקה בחלון חדש: `echo %EBAY_CLIENT_ID%`.

---

## 8. ההבדלים בין CMD לדוגמאות Linux

| נושא | Linux/Mac | CMD |
|---|---|---|
| שבירת שורה | `\` | `^` |
| מרכאות | `'...'` | `"..."` בלבד |
| העתקה | `cp` | `copy` |
| Python | `python3` | `py` |
| הפעלת venv | `source .venv/bin/activate` | `.venv\Scripts\activate` |
| מעבר לתיקייה בכונן אחר | `cd` | `cd /d` |
| משתנה סביבה קבוע | `export X=1` | `setx X "1"` (בחלון חדש) |

---

## 9. פתרון תקלות

| מה רואים | מה עושים |
|---|---|
| `'py' is not recognized` | מתקינים Python מחדש עם **Add to PATH**, או מנסים `python` במקום `py` |
| `ModuleNotFoundError` | ה-venv לא פעיל. מריצים `.venv\Scripts\activate` ואז `py -m pip install -r requirements-dev.txt` |
| `activate` נחסם ב-PowerShell | עובדים ב-**CMD** ולא ב-PowerShell, או מריצים ב-PowerShell `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `No products found` | מוסיפים `-v` ובודקים את `output\*_errors.csv`. הסיבות הנפוצות: אין בעמוד נתונים מובנים (מוסיפים `selectors` ב-`sources.json`), robots.txt חוסם, או שהסינון פסל את כל המוצרים |
| `http_403` ואחריו `host_skipped` | האתר חוסם בוטים, והמערכת הפסיקה לפנות אליו. **לא עוקפים חסימה.** משתמשים ב-Feed או ב-API רשמי |
| `http_429` | האתר מגביל קצב. מגדילים את `--delay` ומריצים מאוחר יותר |
| `timeout` | האתר איטי. מנסים `--timeout 40` |
| `unsafe_url` | הכתובת מפנה לרשת פנימית (localhost, 192.168...). זו חסימה מכוונת |
| מחירים מסומנים `unverified` | המחיר ישן מ-`--max-age-hours`, חסר מטבע, או נלקח רק מסלקטור HTML. מריצים עם `--no-cache` |
| `insufficient_data` בציון | חסרים נתונים: לחנות אחת אין השוואה, ההיסטוריה קצרה, ולציון השותפים חסרה עמלה מאומתת. זה צפוי בהתחלה, ואחרי כמה ימים של ריצות ההיסטוריה מצטברת |
| עסקאות לא מתפרסמות | צריך לאשר אותן קודם (`py main.py approve <id>`) ולוודא ש-`N8N_WEBHOOK_URL` מוגדר |
| Dashboard לא נפתח | בודקים שהפקודה עדיין רצה בחלון ה-CMD, ומנסים פורט אחר: `py main.py dashboard --port 8800` |
| `--render js` נכשל | `py -m pip install -r requirements-js.txt` ואז `py -m playwright install chromium` |
| `database is locked` | סוגרים ריצה אחרת (למשל משימה מתוזמנת) ומנסים שוב |
| המשימה המתוזמנת לא רצה | בודקים את `logs\automation.log`. ב-Task Scheduler מסמנים "Run whether user is logged on or not" אם צריך |
| עברית משובשת באקסל | פותחים את ה-CSV בלחיצה כפולה (הקובץ ב-UTF-8 עם BOM) |

**עדכון לגרסה חדשה:** `git pull` ואז `py -m pip install -r requirements-dev.txt` ואז `py -m pytest -q`.

אם מופיעה שגיאה אחרת, מעתיקים את הפלט ואת `output\*_errors.csv` לשיחה עם Claude.

</div>
