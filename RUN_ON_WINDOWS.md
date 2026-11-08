# הרצה ב-Windows (CMD)

<div dir="rtl">

מדריך להרצת הכלי מתוך שורת הפקודה (CMD) של Windows: התקנה, הרצה, פתרון תקלות, ואיך לעבוד עם Claude על המחשב שלך.

## תוכן

1. [הכנה (פעם אחת)](#1-הכנה-פעם-אחת)
2. [הגדרת קישורי השותפים](#2-הגדרת-קישורי-השותפים)
3. [הרצה](#3-הרצה)
4. [ההבדלים בין CMD לדוגמאות ב-README](#4-ההבדלים-בין-cmd-לדוגמאות-ב-readme)
5. [פתרון תקלות](#5-פתרון-תקלות)
6. [עבודה עם Claude על המחשב שלך](#6-עבודה-עם-claude-על-המחשב-שלך)

---

## 1. הכנה (פעם אחת)

### התקנת Python

מורידים מ-[python.org](https://www.python.org/downloads/) ומתקינים.
בזמן ההתקנה חשוב לסמן את התיבה **"Add python.exe to PATH"**.

כדי לוודא שההתקנה עבדה, פותחים CMD (מקש Windows, מקלידים `cmd`, Enter) ומריצים:

</div>

```cmd
python --version
```

<div dir="rtl">

### הורדת הקוד

> **שימו לב:** הגרסה החדשה נמצאת כרגע ב-branch `claude/friendly-ramanujan-n71p6h` ועדיין לא ב-`main`.
> אחרי שהיא תמוזג ל-`main`, אפשר להוריד מהדף הראשי של הפרויקט בלי לציין branch.

**בלי Git:** נכנסים ל-
[הגרסה החדשה ב-GitHub](https://github.com/gregokrol/Web_Scraping/tree/claude/friendly-ramanujan-n71p6h),
לוחצים על **Code** ואז **Download ZIP**, ומחלצים את הקובץ ל-`C:\Web_Scraping`.
(התיקייה שבתוך ה-ZIP עשויה להיקרא `Web_Scraping-claude-friendly-ramanujan-n71p6h`. אפשר לשנות לה את השם ל-`Web_Scraping`.)

**עם Git מותקן:**

</div>

```cmd
cd C:\
git clone -b claude/friendly-ramanujan-n71p6h https://github.com/gregokrol/Web_Scraping.git
```

<div dir="rtl">

### התקנת הספריות

</div>

```cmd
cd C:\Web_Scraping
python -m pip install -r requirements.txt
```

<div dir="rtl">

---

## 2. הגדרת קישורי השותפים

</div>

```cmd
copy affiliate_config.example.json affiliate_config.json
notepad affiliate_config.json
```

<div dir="rtl">

ב-Notepad מחליפים את `YOUR-AMAZON-TAG-20` ואת שאר הערכים ב-ID האמיתיים שלך ושומרים.
אם עוד אין לך ID כאלה, אפשר לדלג על השלב הזה ולהריץ בלי `-c`.

---

## 3. הרצה

### מוצר בודד

</div>

```cmd
cd C:\Web_Scraping
python main.py https://some-store.com/products/some-product -v
```

<div dir="rtl">

### חנות שלמה דרך ה-sitemap

</div>

```cmd
python main.py https://some-store.com --discover sitemap --max-pages 100 --min-rating 4 --in-stock-only --top 30 -c affiliate_config.json --formats csv,html,md
```

<div dir="rtl">

### עמוד קטגוריה

</div>

```cmd
python main.py https://some-store.com/collections/coffee --discover links --include "/products/" -c affiliate_config.json
```

<div dir="rtl">

### רשימת כתובות מקובץ

יוצרים קובץ `urls.txt` עם כתובת אחת בכל שורה, ומריצים:

</div>

```cmd
python main.py -f urls.txt -c affiliate_config.json
```

<div dir="rtl">

### פתיחת התוצאות

</div>

```cmd
explorer output
```

<div dir="rtl">

| קובץ | שימוש |
|---|---|
| `products_*.csv` | פתיחה באקסל (העברית מוצגת תקין) |
| `products_*.html` | דף כרטיסי מוצר, נפתח בדפדפן |
| `products_*.md` | טקסט לפוסט בבלוג |
| `products_*.json` | חיבור לאתר, לבוט או ל-Zapier |

כל האפשרויות מופיעות ב:

</div>

```cmd
python main.py --help
```

<div dir="rtl">

---

## 4. ההבדלים בין CMD לדוגמאות ב-README

| נושא | ב-README (Linux/Mac) | ב-CMD |
|---|---|---|
| שבירת פקודה לכמה שורות | `\` | `^`, או לכתוב הכול בשורה אחת |
| מרכאות סביב ביטוי רגולרי | `'...'` או `"..."` | רק `"..."` |
| העתקת קובץ | `cp` | `copy` |
| הפעלת Python | `python3` | `python`, או `py` |

דוגמה לשבירת שורות ב-CMD:

</div>

```cmd
python main.py https://some-store.com --discover sitemap --max-pages 100 ^
  --min-rating 4 --in-stock-only --top 30 ^
  -c affiliate_config.json --formats csv,html,md
```

<div dir="rtl">

---

## 5. פתרון תקלות

| מה רואים | מה עושים |
|---|---|
| `'python' is not recognized...` | מנסים `py` במקום `python`. אם גם זה לא עובד, מתקינים את Python מחדש ומסמנים **Add python.exe to PATH**. |
| `'pip' is not recognized...` | משתמשים ב-`python -m pip install -r requirements.txt`. |
| `ModuleNotFoundError: No module named 'requests'` (או `bs4`, `lxml`) | הספריות לא הותקנו. מריצים שוב את פקודת ההתקנה מתוך `C:\Web_Scraping`. |
| `can't open file 'main.py'` | לא נמצאים בתיקייה הנכונה. מריצים `cd C:\Web_Scraping`. |
| `No products found` | העמוד לא מפרסם נתוני מוצר מובנים, או ש-robots.txt של האתר חוסם, או שהסינון שהגדרת פסל את כל המוצרים. מוסיפים `-v` כדי לראות את הסיבה. |
| `HTTP 403` או `HTTP 429` | האתר חוסם בוטים או מגביל קצב. מגדילים את ההשהיה (`--delay 5`). אם עדיין חוסם, משתמשים ב-API או ב-feed הרשמי של תוכנית השותפים (Amazon, AliExpress וכו'). |
| `robots.txt disallows ...` | בעל האתר ביקש לא לסרוק את העמוד הזה. הכלי מכבד את הבקשה. |
| עברית משובשת באקסל | פותחים את ה-CSV בלחיצה כפולה, לא דרך ייבוא טקסט. |
| תוצאות ישנות | הכלי שומר עמודים ב-`.cache`. מוחקים את התיקייה (`rmdir /s /q .cache`) או מריצים עם `--cache-dir ""`. |

אם מופיעה שגיאה אחרת, מעתיקים את כל מה שהודפס ב-CMD ומדביקים בשיחה עם Claude.
כדי להעתיק מ-CMD: מסמנים את הטקסט עם העכבר ולוחצים Enter (או Ctrl+C ב-Windows 10/11).

---

## 6. עבודה עם Claude על המחשב שלך

שיחה עם Claude Code באתר או באפליקציה רצה על שרת בענן, לא על המחשב שלך.
לכן Claude לא יכול לגשת לקבצים שלך או להריץ את הכלי מול אתרים אמיתיים בשבילך.

| איפה | מה אפשר לעשות |
|---|---|
| **שיחה בענן** (Claude Code באתר או באפליקציה) | לכתוב ולתקן קוד, להריץ טסטים, לשמור ל-GitHub. **אין** גישה למחשב שלך, ואתרים חיצוניים עשויים להיות חסומים. |
| **Claude Desktop** על המחשב שלך | הכול, כולל הרצה מול אתרים אמיתיים ותיקון ישירות בתיקייה שלך. |

### אפשרות 1: Claude Desktop (מומלץ)

1. מתקינים את [Claude Desktop](https://claude.ai/download).
2. פותחים בו את התיקייה `C:\Web_Scraping`.
3. מבקשים, למשל: "תריץ את main.py על החנות X ותתקן את מה שלא עובד".

### אפשרות 2: חיבור המחשב לשיחה בענן (Remote Control)

1. ב-Claude Desktop נכנסים ל-**Settings**, אחר כך **Claude Code**, ואז **Remote Control**.
2. מוסיפים את התיקייה `C:\Web_Scraping`.
3. משאירים את Claude Desktop פתוח, וחוזרים לשיחה בענן. Claude יוכל לבקש גישה לתיקייה, ותצטרך לאשר.

> האפשרות הזו עדיין לא זמינה לכל החשבונות. אם היא לא עובדת אצלך, משתמשים באפשרות 1.

### אפשרות 3: הרצה ידנית

מריצים ב-CMD לפי המדריך הזה, מדביקים את הפלט בשיחה, ו-Claude מתקן את הקוד ושומר את השינויים ל-GitHub.
אחרי כל תיקון מורידים את הגרסה החדשה: `git pull`, או ZIP חדש.

</div>
