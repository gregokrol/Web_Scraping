"""Run the agent manager from Telegram: you write "/manager <task>" to the bot in private, a listener on
your computer runs Claude Code with .claude/commands/manager.md, and the summary comes back to Telegram.

Safety:
* only messages from YOUR private chat (TELEGRAM_ADMIN_CHAT) are accepted - everything else is ignored
* the task text goes to Claude on stdin, never on a command line (no shell, no injection through & | ")
* Claude runs with a fixed, limited tool list: read/edit project files, the project's own commands and
  tests, read-only git. No commit, push, delete or other shell commands (docs/agents/RULES.md)
* one task at a time; a time limit stops a run that hangs
* pictures sent with "/manager <task>" in the caption (e.g. Instagram Insights screenshots) are saved in
  agents_output/inbox (git-ignored) and their paths are added to the task: only jpg / png / webp, checked by
  their bytes, at most 10 per task and 10 MB each, file names chosen by us (never the sender's)
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
from datetime import datetime, timezone
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
# pictures sent with a task (e.g. Instagram Insights screenshots): saved in the project for the manager to Read
MAX_IMAGES = 10                 # a Telegram album holds up to 10
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALBUM_WAIT = 3.0                # the photos of one album arrive as separate messages: collect them first
IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
IMAGE_HINT = ("📷 כדי שהמנהל יקרא את התמונה, שלח אותה שוב וכתוב בכיתוב שלה /manager ואת המשימה, למשל:\n"
              "/manager אלה נתוני האינסטגרם של השבוע, תעדכן את funnel-analyst")

HELP = ("🤖 מנהל הסוכנים - פקודות:\n"
        "/manager <משימה> - המנהל מפרק את המשימה, מחלק לסוכנים, מעביר QA ומחזיר סיכום\n"
        "   (אפשר גם: מנהל: <משימה>)\n"
        "📷 תמונה או כמה תמונות עם /manager <משימה> בכיתוב - המנהל מקבל גם אותן (למשל צילומי מסך מאינסטגרם)\n"
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


def image_refs(msg: dict) -> list[dict]:
    """The pictures in a message: [{"file_id", "size"}]. A photo (Telegram keeps several sizes: the largest),
    or a picture sent as a file (jpg / png / webp only)."""
    photos = msg.get("photo") or []
    if photos:
        best = max(photos, key=lambda p: (p.get("width", 0) * p.get("height", 0), p.get("file_size", 0)))
        return [{"file_id": best["file_id"], "size": int(best.get("file_size") or 0)}]
    doc = msg.get("document") or {}
    if doc.get("file_id") and str(doc.get("mime_type", "")).lower() in IMAGE_TYPES:
        return [{"file_id": doc["file_id"], "size": int(doc.get("file_size") or 0)}]
    return []


def image_kind(data: bytes) -> Optional[str]:
    """The file extension by the bytes themselves (never by the sender's file name), or None if no picture."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def manager_prompt(task: str) -> str:
    return ("You were started from the owner's private Telegram chat. Read .claude/commands/manager.md and "
            "docs/agents/RULES.md first, then act as the manager described there for the task below. "
            "Never commit, push or publish anything. This run has no human watching it live: start every agent "
            "in the FOREGROUND (run_in_background: false) and wait for it, never end your turn while an agent "
            "runs, and do not write interim status messages - report stages with 'py main.py progress' instead. "
            "Write exactly ONE final message, which is sent back to Telegram: in Hebrew, in the summary format of "
            "manager.md (it starts with '## סיכום'), under 3500 characters, plain text (no tables).\n\n"
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
    from .telegram import AGENT_RUN_ENV
    env = {**os.environ, AGENT_RUN_ENV: "1"}       # every command Claude runs inherits it: no public actions
    r = subprocess.run(args, input=manager_prompt(task), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(project_dir), timeout=timeout, env=env,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout or "").strip()
    heads = [m.start() for m in re.finditer(r"(?m)^## סיכום", out)]
    if heads:                         # only the final summary, not waiting notes printed before it
        out = out[heads[-1]:]
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
        self._acked: set = set()          # button presses already answered (ack_buttons)
        self.tg, self.project_dir, self.admin_chat = tg, Path(project_dir), str(admin_chat).strip()
        self.runner, self.status, self.digest = runner, status, digest
        self.out_dir = Path(out_dir or self.project_dir / "agents_output" / "telegram")
        self.inbox = self.project_dir / "agents_output" / "inbox"     # pictures you sent (git-ignored)
        self.album_wait = ALBUM_WAIT
        self._albums: dict[str, dict] = {}
        self._lock = threading.Lock()         # album parts and task start come from the timer thread too
        self.busy: Optional[threading.Thread] = None
        self.current = ""
        self.started: Optional[float] = None
        self.started_at = ""                  # ISO time of the current task, to read only its progress
        # callable() -> ["📋 התוכנית: ...", ...] - the reported stages of the current task (agents_output/progress.jsonl)
        self.progress: Optional[Callable[[], list[str]]] = None
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
        mine = self._mine(cq)
        data = str(cq.get("data") or "")
        prefix, _, ident = data.partition(":")
        if not self._valid_press(cq):
            try:
                self.tg.answer_button(cq.get("id", ""), "לא מורשה" if not mine else "פעולה לא מוכרת")
            except Exception:  # noqa: BLE001
                pass
            log.info("ignored a button press: %s from %s", data, (cq.get("from") or {}).get("id"))
            return
        try:
            if cq["id"] in self._acked:
                self._acked.discard(cq["id"])              # answered already by ack_buttons
            else:
                self.tg.answer_button(cq["id"], "מבצע...")
            self.tg.clear_buttons(self.admin_chat, msg["message_id"])     # one decision per message
        except Exception as exc:  # noqa: BLE001
            log.warning("button acknowledgement failed: %s", exc)
        try:
            result = self.actions[prefix](int(ident))
        except Exception as exc:  # noqa: BLE001 - tell, do not crash the listener
            result = f"❌ נכשל: {str(exc)[:300]}"
        self.reply(result)

    def _mine(self, cq: dict) -> bool:
        return str((cq.get("from") or {}).get("id")) == self.admin_chat and self.authorized(cq.get("message") or {})

    def _valid_press(self, cq: dict) -> bool:
        """YOUR press, in your private chat, on a known action with a numeric id - the only presses that run."""
        prefix, _, ident = str(cq.get("data") or "").partition(":")
        return self._mine(cq) and prefix in self.actions and ident.isdigit()

    def ack_buttons(self, updates: list) -> None:
        """Answer every button press of a batch at once, before any action runs: Telegram only accepts the
        answer for a short time, and one action (publish + website) can take a minute."""
        for u in updates:
            cq = u.get("callback_query") or {}
            if cq.get("id") and self._valid_press(cq):
                try:
                    self.tg.answer_button(cq["id"], "מבצע...")
                    self._acked.add(cq["id"])
                except Exception as exc:  # noqa: BLE001
                    log.warning("button acknowledgement failed: %s", exc)

    def handle(self, update: dict) -> None:
        if update.get("callback_query"):
            self.handle_button(update["callback_query"])
            return
        msg = update.get("message") or {}
        if not msg or not self.authorized(msg):
            if msg:
                log.info("ignored a message from chat %s", (msg.get("chat") or {}).get("id"))
            return
        text = msg.get("text") or msg.get("caption") or ""     # a picture's words come as its caption
        images = image_refs(msg)
        if not images and any(msg.get(k) for k in ("document", "video", "audio", "voice", "video_note", "sticker")):
            self.reply("אפשר לצרף למשימה רק תמונות (jpg, png או webp). את הקובץ הזה המנהל לא מקבל.")
            return
        if images and msg.get("media_group_id"):
            self._album_part(str(msg["media_group_id"]), text, images)
            return
        self.command(text, images)

    def _album_part(self, group: str, text: str, images: list[dict]) -> None:
        """One photo of an album. The album is handled as ONE message once no more photos arrive."""
        with self._lock:
            album = self._albums.setdefault(group, {"text": "", "images": [], "timer": None})
            album["images"] += images
            if text and not album["text"]:
                album["text"] = text                   # the caption is usually on one photo only
            if album["timer"]:
                album["timer"].cancel()
            album["timer"] = threading.Timer(self.album_wait, self.album_done, args=(group,))
            album["timer"].daemon = True
            album["timer"].start()

    def album_done(self, group: str) -> None:
        with self._lock:
            album = self._albums.pop(group, None)
        if album:
            self.command(album["text"], album["images"])

    def command(self, text: str, images: Optional[list[dict]] = None) -> None:
        images = images or []
        cmd, arg = parse_command(text)
        if images and cmd != "manager":
            self.reply(IMAGE_HINT)
            return
        if cmd == "help":
            self.reply(HELP)
        elif cmd == "task":
            if self.busy and self.busy.is_alive():
                mins = (time.monotonic() - (self.started or time.monotonic())) / 60
                stages = self.progress() if self.progress else []
                text = f"⏳ המנהל עובד כבר {mins:.0f} דקות (עד {TASK_TIMEOUT // 60}) על:\n{self.current[:300]}"
                if stages:
                    text += "\n\nהשלבים עד עכשיו:\n" + "\n".join(stages)
                else:
                    text += "\n\nעוד לא דווח שלב (התוכנית מגיעה בדקות הראשונות)."
                self.reply(text)
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
            elif len(images) > MAX_IMAGES:
                self.reply(f"יותר מדי תמונות ({len(images)}). עד {MAX_IMAGES} בכל משימה.")
            elif any(i["size"] > MAX_IMAGE_BYTES for i in images):
                self.reply(f"תמונה גדולה מדי. עד {MAX_IMAGE_BYTES // (1024 * 1024)} MB לתמונה.")
            else:
                with self._lock:
                    if self.busy and self.busy.is_alive():
                        self.reply(f"⏳ המנהל עוד עובד על המשימה הקודמת:\n{self.current[:200]}\n"
                                   "שלח שוב כשהיא תסתיים.")
                        return
                    self.current = arg
                    self.busy = threading.Thread(target=self._work, args=(arg, images), daemon=True)
                    self.busy.start()
        else:
            self.reply("לא הבנתי. " + HELP)

    def save_images(self, images: list[dict]) -> list[str]:
        """Download the pictures into agents_output/inbox -> their paths relative to the project (for Read)."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        paths = []
        for n, ref in enumerate(images, 1):
            data = self.tg.download_file(ref["file_id"], max_bytes=MAX_IMAGE_BYTES)
            kind = image_kind(data)
            if not kind:
                raise ValueError(f"קובץ {n} הוא לא תמונה (jpg, png או webp)")
            path = self.inbox / f"{stamp}_{n}.{kind}"
            path.write_bytes(data)
            try:
                paths.append(path.relative_to(self.project_dir).as_posix())
            except ValueError:
                paths.append(str(path))
        return paths

    def _work(self, task: str, images: Optional[list[dict]] = None) -> None:
        note = f"\n📷 עם {len(images)} תמונות" if images else ""
        self.reply(f"✅ קיבלתי. המנהל מתחיל לעבוד על:\n{task[:300]}{note}\n\n"
                   "זה יכול לקחת כמה דקות. הסיכום יגיע לכאן.")
        if images:
            try:
                paths = self.save_images(images)
            except Exception as exc:  # noqa: BLE001 - tell, do not crash the listener
                self.reply(f"❌ לא הצלחתי לשמור את התמונות, והמשימה לא התחילה: {str(exc)[:300]}")
                return
            task += ("\n\nThe owner attached these pictures (screenshots). Open each one with the Read tool and "
                     "use only what is really in them; do not guess numbers that are not readable:\n"
                     + "\n".join(f"- {p}" for p in paths))
        started = self.started = time.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
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


RUN_LOCK_PORT = 47824           # one automation run at a time (a manual run and the scheduled one)


def single_instance(port: int = LOCK_PORT) -> Optional[socket.socket]:
    """A listening socket on localhost as a lock: None if another one (listener / run) already holds it.
    The operating system frees it when the process ends, also after a crash."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
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
        listener.ack_buttons(updates)
        for u in updates:
            offset = int(u["update_id"]) + 1
            save_offset(offset)          # saved first: a message that crashes us is not repeated forever
            listener.handle(u)


def startup_folder() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" if appdata else None
