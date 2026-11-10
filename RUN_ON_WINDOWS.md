# הרצה ב-Windows (CMD), גרסה 3

<div dir="rtl">

מדריך להתקנה, הפעלה, אוטומציה ופתרון תקלות ב-Windows 11 עם Python 3.11 ומעלה.

## הדרך הקלה: התקנה בלחיצה כפולה

1. מורידים את הקובץ [install_windows.bat](https://raw.githubusercontent.com/gregokrol/Web_Scraping/claude/web-scraping-v3/install_windows.bat): קליק ימני על הקישור, ואז "שמור קישור בשם".
2. לוחצים עליו לחיצה כפולה. אם Windows מציג "Windows protected your PC", לוחצים **More info** ואז **Run anyway**.
3. הקובץ עושה הכול לבד:
   - מתקין Python אם הוא חסר, ושואל לפני כן
   - מוריד את הקוד ל-`C:\Web_Scraping`
   - מתקין את הספריות ומכין את קבצי ההגדרות
   - מריץ בדיקות ויוצר קיצור דרך **Web Scraping** בשולחן העבודה
4. מפעילים מקיצור הדרך. נפתח תפריט עם מספרים: סריקה, חיפוש ב-AliExpress וב-Amazon, Dashboard, אוטומציה, אישור עסקאות, פרסום לטלגרם ותזמון.

אם Python הותקן באמצע, סוגרים את החלון ומפעילים את `install_windows.bat` שוב.

ההוראות שלמטה מיועדות למי שמעדיף להתקין ידנית.

## תוכן

1. [התקנה (פעם אחת)](#1-התקנה-פעם-אחת)
2. [קבצי הגדרות](#2-קבצי-הגדרות)
3. [איסוף מוצרים](#3-איסוף-מוצרים)
4. [תוצאות, היסטוריה וציונים](#4-תוצאות-היסטוריה-וציונים)
5. [ממשק הניהול (Dashboard)](#5-ממשק-הניהול-dashboard)
6. [אוטומציה: הרצה מתוזמנת, n8n ואישור ידני](#6-אוטומציה-הרצה-מתוזמנת-n8n-ואישור-ידני)
   - [פרסום ישיר לערוץ טלגרם](#פרסום-ישיר-לערוץ-טלגרם)
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

:: Amazon Creators API (keys in environment variables - section 7)
py main.py --amazon-search "electric kettle"
py main.py --amazon-asin B0XXXXXXXX,B0YYYYYYYY

:: AliExpress Affiliate API (returns the affiliate link and the commission rate)
py main.py --aliexpress-search "usb c hub"
py main.py --aliexpress-id 1005001234567890

:: Israeli store XML feed (the feed it gives price-comparison sites)
py main.py --feed https://store.co.il/feeds/products.xml --sources sources.json

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
py main.py schedule --every twice-daily --at 08:13 --create
py main.py schedule --every hourly --hours 6 --at 00:10 --create
py main.py schedule --remove
```

<div dir="rtl">

הפקודה יוצרת קובץ `run_automation.bat`, ו-Task Scheduler מפעיל אותו. הקובץ משתמש ב-Python שהריץ את הפקודה, ולכן מריצים `schedule` כשה-venv פעיל.
יומן הריצות נכתב לקובץ `logs\automation.log`. כדי לבדוק את המשימה: מקש Windows, מקלידים "Task Scheduler",
ומחפשים בספרייה את `WebScrapingAffiliate`.

**n8n:** הוראות מלאות בקובץ [docs/N8N.md](docs/N8N.md).

### אתר שמתחבר לערוץ (GitHub Pages, בחינם)

המערכת בונה אתר מהעסקאות **שאישרת** בלבד, עם עמוד לכל עסקה, גרף היסטוריית מחיר והשוואה בין חנויות.
- **מטלגרם לאתר:** בכל פוסט בערוץ יש כפתור "📈 היסטוריית מחיר" שמוביל לעמוד העסקה באתר.
- **מהאתר לטלגרם:** בכל עמוד באתר יש כפתור "הצטרפו לערוץ".
- **מגוגל:** האתר כולל sitemap, RSS ותגיות SEO, ולכן גוגל יכול למצוא אותו.

**הקמה (פעם אחת, בערך 10 דקות):**
1. ב-GitHub לוחצים **New repository**, נותנים לו שם (למשל `mechiremet`), בוחרים **Public** ולוחצים **Create repository**.
2. ב-CMD:

</div>

```cmd
cd /d C:\Projects
git clone https://github.com/YOUR-USER/mechiremet.git
```

<div dir="rtl">

3. בתפריט בוחרים **12** ומוסיפים ל-`automation.json` (עם `/` ולא `\` בנתיב):

</div>

```json
"site": {
  "enabled": true,
  "base_url": "https://YOUR-USER.github.io/mechiremet/",
  "telegram_url": "https://t.me/mechiremet",
  "title": "מחיר אמת",
  "out_dir": "site",
  "publish_dir": "C:/Projects/mechiremet"
},
```

<div dir="rtl">

4. בתפריט בוחרים **21** כדי לראות את האתר במחשב, ואחר כך **22** כדי לפרסם.
5. ב-GitHub, במאגר `mechiremet`, נכנסים ל-**Settings**, אחר כך **Pages**, ושם בוחרים **Deploy from a branch**, ואז `main` ו-`/ (root)`, ולוחצים **Save**.
6. אחרי דקה או שתיים האתר זמין בכתובת `https://YOUR-USER.github.io/mechiremet/`.

מכאן כל ריצה (תפריט **6**, או לפי התזמון) מעדכנת את האתר לבד, **לפני** שהפוסט עולה לטלגרם. ככה הקישור בפוסט כבר עובד.

**לא להגדיר את `publish_dir` לתיקיית המערכת עצמה.** המערכת מסרבת לעשות את זה, כי הפרסום מחליף את כל הקבצים בתיקייה.

### פרסום ישיר לערוץ טלגרם

המערכת יכולה לפרסם עסקאות מאושרות ישירות בערוץ טלגרם שלך, בלי n8n.

**הקמה (פעם אחת):**
1. בטלגרם פותחים את **@BotFather**, שולחים `/newbot` ומעתיקים את ה-Token.
2. יוצרים ערוץ ומוסיפים את הבוט כ**מנהל** עם הרשאה לפרסם הודעות.
3. מוצאים את ה-Chat ID הפרטי שלך, למשל דרך הבוט **@userinfobot**. לשם יגיעו בקשות האישור.
4. מגדירים משתני סביבה ופותחים חלון CMD חדש:

</div>

```cmd
setx TELEGRAM_BOT_TOKEN "123456:ABC..."
setx TELEGRAM_CHANNEL "@my_deals_channel"
setx TELEGRAM_ADMIN_CHAT "123456789"
py main.py telegram-check --test-admin
```

<div dir="rtl">

5. ב-`automation.json` משנים ל-`"telegram": {"enabled": true}`.

**איך זה עובד ביום-יום:**
1. המשימה המתוזמנת מריצה את `run`.
2. עסקאות חדשות נשלחות **אליך בפרטי** לאישור.
3. מאשרים: `py main.py approve <id>`, או בלחיצה ב-Dashboard.
4. בריצה הבאה, או מיד עם `py main.py publish --telegram`, העסקה מתפרסמת **בערוץ**.

כל עסקה מתפרסמת פעם אחת בלבד. כל פוסט כולל תמונה, את העובדות המאומתות, כפתור רכישה, את תאריך המחיר ושורת גילוי נאות.

**לוגו על התמונות:** הלוגו של הערוץ (`assets\logo.png`) מוטבע בפינה של כל תמונת מוצר. כדי להחליף לוגו, מחליפים את הקובץ. כדי לבטל: `setx TELEGRAM_WATERMARK "0"`. אם לא הצלחנו להוריד את תמונת המוצר, היא מתפרסמת בלי לוגו.

| אם רואים | מה עושים |
|---|---|
| `bot is admin: False` | מוסיפים את הבוט כמנהל בערוץ עם הרשאה לפרסם |
| `Forbidden: bot is not a member` | אותו הדבר: הבוט לא נמצא בערוץ |
| `chat not found` | בודקים את `TELEGRAM_CHANNEL`. בערוץ ציבורי `@שם`, בפרטי מזהה שמתחיל ב-`-100` |
| `Unauthorized` | ה-Token שגוי. מפיקים חדש ב-@BotFather |

**לפני שמפרסמים:** תוכניות שותפים קובעות היכן מותר לפרסם קישורים. למשל, לפי התנאים של KSP קישור שלא עובר דרך אתר צד שלישי לא מזכה בעמלה, ולכן כדאי לשאול את KSP אם ערוץ טלגרם נחשב. בדקו את התנאים של כל תוכנית, ובמיוחד את הכללים לפרסום ברשתות חברתיות ובקבוצות צ'אט.

---

## 7. מפתחות API ומשתני סביבה

מפתחות **לא** נשמרים בקוד ולא בקבצי ההגדרות. מגדירים אותם פעם אחת עם `setx` ופותחים חלון CMD חדש:

</div>

```cmd
setx AMAZON_CREDENTIAL_ID "your-credential-id"
setx AMAZON_CREDENTIAL_SECRET "your-credential-secret"
setx AMAZON_CREDENTIAL_VERSION "2.1"
setx AMAZON_PARTNER_TAG "yourtag-20"
setx AMAZON_MARKETPLACE "www.amazon.com"
setx ALIEXPRESS_APP_KEY "your-app-key"
setx ALIEXPRESS_APP_SECRET "your-app-secret"
setx ALIEXPRESS_TRACKING_ID "your-tracking-id"
setx N8N_WEBHOOK_URL "http://localhost:5678/webhook/web-scraping"
setx WEBHOOK_SECRET "a-long-random-string"
```

<div dir="rtl">

בדיקה בחלון חדש: `echo %AMAZON_PARTNER_TAG%`.

**מאיפה משיגים את המפתחות:**

| מקור | איפה | הערות |
|---|---|---|
| **Amazon** | Associates Central, בתפריט Tools, תחת Creators API | `AMAZON_CREDENTIAL_VERSION` מופיע ליד המפתחות. Amazon נותנת גישה ל-API רק לחשבונות עם מכירות דרך הקישורים, וכדאי לבדוק את הדרישות העדכניות. המערכת **לא שומרת היסטוריית מחירים** של Amazon, כי התנאים שלה מגבילים שמירת נתונים. השוואה בין חנויות עובדת כרגיל |
| **AliExpress** | AliExpress Portals, ואז פלטפורמת המפתחים, ושם מגישים בקשה ל-Affiliate API | ה-Tracking ID נמצא ב-Portals. בלעדיו מתקבלות תוצאות ריקות. ברירת המחדל: עברית (`HE`), משלוח לישראל (`IL`), מחיר ב-USD |
| **חנויות ישראליות** | מבקשים מהחנות את קובץ ה-XML שהיא נותנת לאתרי השוואת מחירים | לא צריך מפתח. מגדירים את החנות ב-`sources.json` עם `"currency": "ILS"`. מחירים בפורמט `₪ 189.90` מזוהים אוטומטית |

eBay עדיין נתמך (`EBAY_CLIENT_ID` ו-`EBAY_CLIENT_SECRET`), אבל הוא לא חובה.

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
| `blocked_by_terms` | תנאי השימוש של האתר אוסרים איסוף אוטומטי (למשל KSP, סעיף 6.7). המערכת לא סורקת אותו גם עם `--ignore-robots`. אחרי שמקבלים אישור בכתב, מוסיפים את הדומיין ל-`"permitted_domains"` ב-`sources.json` |
| `unsafe_url` | הכתובת מפנה לרשת פנימית (localhost, 192.168...). זו חסימה מכוונת |
| מחירים מסומנים `unverified` | המחיר ישן מ-`--max-age-hours`, חסר מטבע, או נלקח רק מסלקטור HTML. מריצים עם `--no-cache` |
| `insufficient_data` בציון | חסרים נתונים: לחנות אחת אין השוואה, ההיסטוריה קצרה, ולציון השותפים חסרה עמלה מאומתת. זה צפוי בהתחלה, ואחרי כמה ימים של ריצות ההיסטוריה מצטברת |
| עסקאות לא מתפרסמות | צריך לאשר אותן קודם (`py main.py approve <id>`) ולוודא ש-`N8N_WEBHOOK_URL` מוגדר |
| Dashboard לא נפתח | בודקים שהפקודה עדיין רצה בחלון ה-CMD, ומנסים פורט אחר: `py main.py dashboard --port 8800` |
| הפוסט עלה בלי תמונה | טלגרם לא הצליח להוריד את התמונה מהחנות. במקרה כזה המערכת מפרסמת טקסט בלבד, וזה תקין |
| `credentials_missing` | חסר משתנה סביבה. ההודעה אומרת איזה. מגדירים עם `setx` ופותחים חלון CMD חדש |
| AliExpress מחזיר `no_results` | בודקים את `ALIEXPRESS_TRACKING_ID`, ושה-App אושר ל-Affiliate API |
| Amazon מחזיר `http_401` / `http_403` | המפתחות או ה-`AMAZON_CREDENTIAL_VERSION` לא נכונים, או שהחשבון עוד לא קיבל גישה ל-API |
| Amazon מחזיר `http_429` | חרגת מקצב הבקשות. המערכת כבר שולחת לכל היותר בקשה בשנייה, אז מריצים פחות חיפושים בכל ריצה |
| `--render js` נכשל | `py -m pip install -r requirements-js.txt` ואז `py -m playwright install chromium` |
| `database is locked` | סוגרים ריצה אחרת (למשל משימה מתוזמנת) ומנסים שוב |
| המשימה המתוזמנת לא רצה | בודקים את `logs\automation.log`. ב-Task Scheduler מסמנים "Run whether user is logged on or not" אם צריך |
| עברית משובשת באקסל | פותחים את ה-CSV בלחיצה כפולה (הקובץ ב-UTF-8 עם BOM) |

**עדכון לגרסה חדשה:** `git pull` ואז `py -m pip install -r requirements-dev.txt` ואז `py -m pytest -q`.

אם מופיעה שגיאה אחרת, מעתיקים את הפלט ואת `output\*_errors.csv` לשיחה עם Claude.

</div>
