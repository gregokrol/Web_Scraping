# Web_Scraping - "מחיר אמת"

Price-tracking and deals system for Israeli stores (Python). Read `STATUS.md` for where the project stands.

## Rules (always)
Follow `docs/agents/RULES.md`: respect robots.txt and terms, never bypass blocks or CAPTCHA, no secrets in files,
no success claims without running `py -m pytest -q`, no destructive git, no public posting without the owner.
**Always write to the owner in Hebrew** - every reply, every summary, every question, also after a tool error
or a long technical step (code, commands and file names stay as they are). Plain text, no `<div>` wrappers.

## The agent manager
When the owner hands over a task for the team - the message starts with `/manager`, `מנהל:` or `מנהל`, or asks
to "pass it to the manager" / "תעביר למנהל" - act as the manager: follow `.claude/commands/manager.md` with that
task (plan, run the agents in `.claude/agents/` in parallel where possible, QA every output, summarize).
The same manager can be started from Telegram (`py main.py telegram-listen`, see `docs/AGENTS.md`).
