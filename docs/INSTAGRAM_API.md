# חיבור אינסטגרם בלי פייסבוק (Instagram API with Instagram Login)

המטרה בשלב הזה: **לקרוא נתונים** על הרילס ועל העוקבים, כדי לראות מה עובד. הפרסום נשאר ידני.
לא צריך עמוד פייסבוק. יש דרך אחת, והיא עובדת רק לחשבון אינסטגרם מקצועי (עסקי או יוצר).
ההנחיות נבדקו מול התיעוד של Meta ב-24.11.2026. אם מסך נראה אחרת, הכותרות בתיעוד הן הקובעות.

## 1. חשבון מקצועי (2 דקות, בטלפון)
באינסטגרם: פרופיל ← ☰ ← "סוג חשבון וכלים" ← "מעבר לחשבון מקצועי".
- בוחרים **יוצר (Creator)** או **עסק (Business)**. שניהם עובדים.
- קטגוריה מתאימה: "אתר קניות" או "מוצרי צריכה".
- כשאינסטגרם שואל אם לחבר עמוד פייסבוק, מדלגים.

## 2. חשבון מפתח ב-Meta (5 דקות, במחשב)
1. נכנסים ל-https://developers.facebook.com ← "Get Started".
2. Meta יבקש להתחבר. אם יש לך פרופיל פייסבוק אישי, משתמשים בו. אם אין, נרשמים לחשבון מפתח לפי מה שהאתר מציע.
   - חשבון המפתח הוא רק כדי ליצור אפליקציה. לא צריך עמוד פייסבוק, והוא לא מתפרסם בשום מקום.
3. מאשרים את תנאי המפתחים. אם מבקשים, מאמתים טלפון או מייל.

## 3. יוצרים אפליקציה (5 דקות)
1. "My Apps" ← "Create App".
2. סוג או "Use case": בוחרים את האפשרות של **Instagram**, שנקראת "Manage messaging & content on Instagram", או "Other" ואז "Business".
3. שם האפליקציה: `mechiremet-insights`. האפליקציה פרטית, ורק אתה משתמש בה.
4. בתוך האפליקציה: "Add product" ← **Instagram** ← "API setup with Instagram login".

## 4. מקבלים מפתח גישה לחשבון שלך (5 דקות)
המסך נמצא ב-Instagram ← "API setup with Instagram login" ← "Generate access tokens".
1. "Add account" ← מתחברים עם חשבון האינסטגרם של "מחיר אמת" ומאשרים.
2. מסמנים רק את ההרשאות הנחוצות:
   - `instagram_business_basic`: פרטי החשבון והפוסטים
   - `instagram_business_manage_insights`: צפיות, שמירות, הגעה ועוקבים
   - **לא** לסמן `instagram_business_content_publish` עכשיו. את הפרסום מוסיפים אחר כך, אם תרצה, ורק עם כפתור אישור שלך.
3. "Generate token" ← נוצר מפתח ארוך (long-lived), שתקף **60 יום**.
4. **לא מעתיקים את המפתח לצ'אט, לקובץ או לגיט.** כותבים ל-Claude "יש לי מפתח". Claude יפתח טופס מאובטח לשמירה.
   המפתח יישמר בהגדרות הסביבה, ושרת ה-proxy יצרף אותו לבקשות ל-`graph.instagram.com`. Claude לא רואה את המפתח.

> האפליקציה נשארת במצב פיתוח (Development), ולא צריך לשלוח אותה לבדיקה של Meta (App Review).
> לפי התיעוד, מספיק שהמפתח נוצר מתוך ה-App Dashboard לחשבון המקצועי שלך עצמו.

## 5. מה Claude יעשה עם המפתח
- **קריאה בלבד:** לכל ריל יקרא צפיות, הגעה, שמירות, שיתופים ולייקים, ויקרא גם את מספר העוקבים.
- **הדוח:** יכין דוח שבועי לפי `docs/agents/CONTENT_PIPELINE.md`, וכל מספר בו יהיה "נמדד", עם המקור "Instagram API".
- **ההחלטות:** הדוח ישמש להחליט על הריל הבא. הפוסט שעבד הכי טוב נכנס לבנק הרעיונות.
- **בלי שינויים:** Claude לא יפרסם, לא ימחק ולא יענה לתגובות.

## 6. כל 60 יום
המפתח פג אחרי 60 יום, ואז חוזרים לשלב 4 ← "Generate token" ← "יש לי מפתח חדש".
Claude יזכיר לך כמה ימים לפני.

## מה לא עושים
- לא נותנים לשירות חיצוני את הסיסמה של אינסטגרם.
- לא מאשרים הרשאות שלא צריך.
- לא קונים עוקבים ולא משתמשים בבוטים (`RULES.md`).

מקורות (Meta, נבדקו 24.11.2026):
- [Instagram API with Instagram Login](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login)
- [Business Login for Instagram](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login)
- [Get started](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/get-started)
- [Insights](https://developers.facebook.com/documentation/instagram-platform/insights)
