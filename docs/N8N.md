# n8n integration

<div dir="rtl">

המערכת שולחת אירועי JSON ל-Webhook של n8n. n8n מעביר אותם הלאה, למשל לטלגרם, לוורדפרס, למייל או לגוגל שיטס.

**כלל חשוב:** רק אירוע `deal.publish` אמור להוביל לפרסום פומבי, והוא נשלח רק עבור עסקאות שאישרת.

</div>

## 1. Create the webhook in n8n

1. New workflow, then add a **Webhook** node. Method: `POST`, Path: e.g. `web-scraping`.
2. Copy the **Production URL** (for example `http://localhost:5678/webhook/web-scraping`).
3. Activate the workflow.

## 2. Tell Web_Scraping the URL (no secrets in files)

```cmd
setx N8N_WEBHOOK_URL "http://localhost:5678/webhook/web-scraping"
setx WEBHOOK_SECRET "choose-a-long-random-string"
```

Open a **new** CMD window after `setx`. Alternatively put `webhook_url` in `automation.json` (which is git-ignored).

## 3. Events

Every request body looks like `{"event": "...", "sent_at": "ISO time", "data": {...}}`.

| event | when | use in n8n |
|---|---|---|
| `scrape.completed` | `py main.py ... --webhook env` | store products/comparisons in Sheets/DB |
| `deals.pending_approval` | `run` found new deals (once per deal) | notify **yourself** (private Telegram chat / email) |
| `crawl.errors` | a run had failures | notify yourself |
| `deal.publish` | an **approved** deal (once per deal) | post publicly: `data.content.telegram`, `data.content.html`, `data.content.markdown`, `data.affiliate_url` |

`deal.publish` data: `alert_id, kind (price_drop / lowest_90d / top_deal), title, price, currency, deal_score,
affiliate_score, store, url, affiliate_url, content{title, telegram, markdown, html, claims[{text, basis}], link, lang}`.

## 4. Suggested workflow

```
Webhook ─► IF {{$json.body.event}} == "deal.publish" ─► Telegram (channel): {{$json.body.data.content.telegram}}
        └► IF {{$json.body.event}} == "deals.pending_approval" ─► Telegram (private chat): "N deals waiting - open the dashboard"
        └► IF {{$json.body.event}} == "crawl.errors" ─► Email to yourself
```

## 5. Verify the signature (recommended)

When `WEBHOOK_SECRET` is set, every request has the header
`X-Signature-SHA256` = hex HMAC-SHA256 of the **raw request body** with your secret.

In n8n, enable the Webhook node's **Raw Body** option, then compare the header against
`crypto.createHmac('sha256', SECRET).update(rawBody).digest('hex')` in a Code node and stop the workflow
if they differ. How the raw body is exposed to the Code node differs between n8n versions, so check
your version's Webhook documentation. Re-serialising the parsed JSON will **not** give the same bytes.

As a simpler alternative, keep n8n reachable only from your own computer (`localhost`) and use an
unguessable webhook path.

## 6. Approval flow

```cmd
py main.py run --config automation.json     :: scrape + detect deals + notify you
py main.py alerts                           :: or the dashboard: "התראות ואישור"
py main.py alerts --show 12                 :: content + the data behind every claim
py main.py approve 12
py main.py publish                          :: sends deal.publish for approved deals only
```

<div dir="rtl">

אפשר גם לתת ל-n8n להפעיל את המערכת בעצמו, עם Execute Command node שמריץ את הפקודה
`cmd /c "cd /d C:\Web_Scraping && .venv\Scripts\python main.py run --config automation.json"`.

</div>
