"""Run the agent manager from Telegram: you write "/manager <task>" to the bot in private, a listener on
your computer runs Claude Code with .claude/commands/manager.md, and the summary comes back to Telegram.

Safety:
* only messages from YOUR private chat (TELEGRAM_ADMIN_CHAT) are accepted - everything else is ignored
* the task text goes to Claude on stdin, never on a command line (no shell, no injection through & | ")
* Claude runs with a fixed, limited tool list: read/edit project files, the project's own commands and
  tests, read-only git. No commit, push, delete or other shell commands (docs/agents/RULES.md)
* one task at a time; a time limit stops a run that hangs
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# what Claude may do when started from Telegram (Claude Code --allowedTools syntax)
ALLOWED_TOOLS = ",".join([
    "Read", "Grep", "Glob", "Edit", "Write", "WebSearch", "Task", "Agent",
    "Bash(py main.py:*)", "Bash(python main.py:*)", "Bash(py -m pytest:*)", "Bash(python -m pytest:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
])
TASK_TIMEOUT = 60 * 60          # one hour
HEARTBEAT = 10 * 60             # "still working" every 10 minutes, so a long task does not look stuck
# The manager plans and coordinates; Sonnet does that well and saves the account's quota. The agents keep
# their own models (.claude/agents/*.md: opus for the QA gate, sonnet for the rest).
DEFAULT_MANAGER_MODEL = "sonnet"
_MODEL_OK = re.compile(r"^(sonnet|opus|haiku|claude-[a-z0-9.-]+)$")
MAX_TASK_CHARS = 2000
LOCK_PORT = 47823               # a second listener would make Telegram answer 409 Conflict

HELP = ("🤖 מנהל הסוכנים - פקודות:\n"
        "/manager <משימה> - המנהל מפרק את המשימה, מחלק לסוכנים, מעביר QA ומחזיר סיכום\n"
        "   (אפשר גם: מנהל: <משימה>)\n"
        "/task - מה המנהל עושה עכשיו, וכמה זמן\n"
        "/status - דוח בדיקה עכשיו\n"
        "/digest - הסיכום לשיתוף עכשיו\n"
        "/help - ההודעה הזאת\n\n"
        "הסוכנים לא מפרסמים ולא עושים commit. הם מכינים טיוטות ושינויים לבדיקה שלך.")

_MANAGER = re.compile(r"^\s*(?:(?:/manager|/m)(?:@\w+)?(?=\s|$)|מנהל(?=[\s:：,-]|$)\s*[:：,-]?)\s*(.*)$", re.S | re.I)


def parse_command(text: str) -> tuple[str, str]:
    """-> (command, argument). command: manager | status | digest | help | unknown."""
    t = (text or "").strip()
    m = _MANAGER.match(t)
    if m:
        return "manager", m.group(1).strip()
    word = t.split()[0].split("@")[0].lower() if t else ""
    if word in ("/status", "/digest", "/help", "/start", "/task"):
        return ("help" if word == "/start" else word[1:]), ""
    return "unknown", t


def manager_prompt(task: str) -> str:
    return ("You were started from the owner's private Telegram chat. Read .claude/commands/manager.md and "
            "docs/agents/RULES.md first, then act as the manager described there for the task below. "
            "Never commit, push or publish anything. Your final message is sent back to Telegram: write it "
            "in Hebrew, in the summary format of manager.md, under 3500 characters, plain text (no tables).\n\n"
            f"The task:\n{task}")


class ClaudeNotFound(RuntimeError):
    pass


def find_claude() -> Optional[str]:
    """The Claude Code program: on PATH, else where its installers put it. A listener started at login (or a
    window opened before the install) often has no PATH entry for it - Windows only adds it to new windows."""
    found = shutil.which("claude")
    if found:
        return found
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    candidates = [home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude"]
    for var, rel in (("APPDATA", ("npm", "claude.cmd")), ("LOCALAPPDATA", ("Programs", "claude", "claude.exe")),
                     ("LOCALAPPDATA", ("AnthropicClaude", "claude.exe"))):
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]).joinpath(*rel))
    return next((str(c) for c in candidates if c.is_file()), None)


def run_claude(task: str, project_dir: Path, timeout: int = TASK_TIMEOUT, exe: Optional[str] = None,
               model: Optional[str] = DEFAULT_MANAGER_MODEL) -> str:
    """Run Claude Code headless in the project with the manager instructions; returns its final answer."""
    exe = exe or find_claude()
    if not exe:
        raise ClaudeNotFound("Claude Code (the 'claude' command) is not installed on this computer")
    args = [exe, "-p", "--output-format", "text", "--permission-mode", "acceptEdits",
            "--allowedTools", ALLOWED_TOOLS]
    if model:
        if not _MODEL_OK.match(model):
            raise ValueError(f"unknown model name: {model!r} (use sonnet, opus, haiku or a claude-... id)")
        args += ["--model", model]
    # the task goes on stdin: a .cmd launcher on Windows would otherwise re-parse it with cmd.exe
    r = subprocess.run(args, input=manager_prompt(task), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(project_dir), timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout or "").strip()
    if r.returncode != 0 and not out:
        raise RuntimeError(f"claude exited with {r.returncode}: {(r.stderr or '').strip()[:500]}")
    return out or "(Claude returned no text)"


class Listener:
    """Reads the bot's messages and runs one manager task at a time in a worker thread."""

    def __init__(self, tg, project_dir: Path, admin_chat: str,
                 runner: Callable[[str, Path], str] = run_claude,
                 status: Optional[Callable[[], str]] = None, digest: Optional[Callable[[], str]] = None,
                 out_dir: Optional[Path] = None, actions: Optional[dict[str, Callable[[int], str]]] = None):
        """``actions``: button handlers by prefix, e.g. {"cpost": publish a channel price, "dpub": approve and
        publish a deal} - each gets the id and returns the text to show you."""
        self.actions = actions or {}
        self.tg, self.project_dir, self.admin_chat = tg, Path(project_dir), str(admin_chat).strip()
        self.runner, self.status, self.digest = runner, status, digest
        self.out_dir = Path(out_dir or self.project_dir / "agents_output" / "telegram")
        self.busy: Optional[threading.Thread] = None
        self.current = ""
        self.started: Optional[float] = None
        self.heartbeat = HEARTBEAT

    def authorized(self, msg: dict) -> bool:
        chat = msg.get("chat") or {}
        return chat.get("type") == "private" and str(chat.get("id")) == self.admin_chat

    def reply(self, text: str) -> None:
        try:
            self.tg.send_long(self.admin_chat, text)
        except Exception as exc:  # noqa: BLE001 - a failed reply must not stop the listener
            log.warning("could not reply on Telegram: %s", exc)

    def handle_button(self, cq: dict) -> None:
        """A press on a button in YOUR private chat. Anyone else's press is refused (and does nothing)."""
        msg = cq.get("message") or {}
        mine = (str((cq.get("from") or {}).get("id")) == self.admin_chat and self.authorized(msg))
        data = str(cq.get("data") or "")
        prefix, _, ident = data.partition(":")
        if not mine or prefix not in self.actions or not ident.isdigit():
            try:
                self.tg.answer_button(cq.get("id", ""), "לא מורשה" if not mine else "פעולה לא מוכרת")
            except Exception:  # noqa: BLE001
                pass
            log.info("ignored a button press: %s from %s", data, (cq.get("from") or {}).get("id"))
            return
        try:
            self.tg.answer_button(cq["id"], "מבצע...")
            self.tg.clear_buttons(self.admin_chat, msg["message_id"])     # one decision per message
        except Exception as exc:  # noqa: BLE001
            log.warning("button acknowledgement failed: %s", exc)
        try:
            result = self.actions[prefix](int(ident))
        except Exception as exc:  # noqa: BLE001 - tell, do not crash the listener
            result = f"❌ נכשל: {str(exc)[:300]}"
        self.reply(result)

    def handle(self, update: dict) -> None:
        if update.get("callback_query"):
            self.handle_button(update["callback_query"])
            return
        msg = update.get("message") or {}
        if not msg or not self.authorized(msg):
            if msg:
                log.info("ignored a message from chat %s", (msg.get("chat") or {}).get("id"))
            return
        cmd, arg = parse_command(msg.get("text") or "")
        if cmd == "help":
            self.reply(HELP)
        elif cmd == "task":
            if self.busy and self.busy.is_alive():
                mins = (time.monotonic() - (self.started or time.monotonic())) / 60
                self.reply(f"⏳ המנהל עובד כבר {mins:.0f} דקות (עד {TASK_TIMEOUT // 60}) על:\n{self.current[:300]}")
            else:
                self.reply("המנהל פנוי. אין משימה פעילה.")
        elif cmd == "status":
            self.reply(self.status() if self.status else "הדוח לא זמין.")
        elif cmd == "digest":
            text = self.digest() if self.digest else None
            self.reply(text or "אין כרגע עסקאות לשיתוף.")
        elif cmd == "manager":
            if not arg:
                self.reply("כתוב את המשימה אחרי הפקודה, למשל:\n/manager תכין 3 טיוטות לאינסטגרם מהעסקאות של היום")
            elif len(arg) > MAX_TASK_CHARS:
                self.reply(f"המשימה ארוכה מדי ({len(arg)} תווים). עד {MAX_TASK_CHARS}.")
            elif self.busy and self.busy.is_alive():
                self.reply(f"⏳ המנהל עוד עובד על המשימה הקודמת:\n{self.current[:200]}\nשלח שוב כשהיא תסתיים.")
            else:
                self.current = arg
                self.busy = threading.Thread(target=self._work, args=(arg,), daemon=True)
                self.busy.start()
        else:
            self.reply("לא הבנתי. " + HELP)

    def _work(self, task: str) -> None:
        self.reply(f"✅ קיבלתי. המנהל מתחיל לעבוד על:\n{task[:300]}\n\nזה יכול לקחת כמה דקות. הסיכום יגיע לכאן.")
        started = self.started = time.monotonic()
        done = threading.Event()

        def beat():                                   # "still working" while the task runs
            while not done.wait(self.heartbeat):
                self.reply(f"⏳ המנהל עדיין עובד ({(time.monotonic() - started) / 60:.0f} דקות). "
                           "הסיכום יגיע לכאן. /task מראה מה הוא עושה.")
        threading.Thread(target=beat, daemon=True).start()
        try:
            answer = self.runner(task, self.project_dir)
        except ClaudeNotFound:
            answer = ("❌ Claude Code לא מותקן במחשב, ולכן אי אפשר להפעיל את המנהל.\n"
                      "מתקינים לפי docs/AGENTS.md (קטע 'הפעלה מטלגרם'), ונכנסים עם החשבון שלך.")
        except subprocess.TimeoutExpired:
            answer = f"❌ המשימה נעצרה אחרי {TASK_TIMEOUT // 60} דקות בלי סיום. כדאי לחלק אותה למשימות קטנות יותר."
        except Exception as exc:  # noqa: BLE001
            answer = f"❌ המנהל נכשל: {str(exc)[:500]}"
        done.set()
        minutes = (time.monotonic() - started) / 60
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
            (self.out_dir / f"{stamp}.md").write_text(f"# משימה\n{task}\n\n# תשובה\n{answer}\n", encoding="utf-8")
        except OSError:
            pass
        self.reply(f"📋 סיכום המנהל ({minutes:.0f} דקות):\n\n{answer}")


def single_instance() -> Optional[socket.socket]:
    """A listening socket on localhost as a lock: None if another listener already runs."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def listen_forever(listener: Listener, get_offset: Callable[[], Optional[int]],
                   save_offset: Callable[[int], None], stop: Optional[threading.Event] = None) -> None:
    offset = get_offset()
    while not (stop and stop.is_set()):
        try:
            updates = listener.tg.get_updates(offset)
        except Exception as exc:  # noqa: BLE001 - network hiccups: wait and go on
            if "409" in str(exc):
                raise RuntimeError("another program is reading this bot's messages (Telegram 409)") from None
            log.warning("getUpdates failed: %s", exc)
            time.sleep(10)
            continue
        for u in updates:
            offset = int(u["update_id"]) + 1
            save_offset(offset)          # saved first: a message that crashes us is not repeated forever
            listener.handle(u)


def startup_folder() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" if appdata else None
