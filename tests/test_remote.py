"""Telegram -> agent manager bridge (affiliate_scraper/remote.py)."""

import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from affiliate_scraper import remote
from affiliate_scraper.cli import main
from affiliate_scraper.remote import ALLOWED_TOOLS, Listener, listen_forever, parse_command, run_claude


@pytest.mark.parametrize("text,expected", [
    ("/manager תכין טיוטות", ("manager", "תכין טיוטות")),
    ("/manager@mechir_bot בדוק את פוקס", ("manager", "בדוק את פוקס")),
    ("/m משימה קצרה", ("manager", "משימה קצרה")),
    ("מנהל: תכנן משפך", ("manager", "תכנן משפך")),
    ("מנהל תכנן משפך", ("manager", "תכנן משפך")),
    ("/manager", ("manager", "")),
    ("/menu", ("unknown", "/menu")),
    ("מנהלים טובים", ("unknown", "מנהלים טובים")),
    ("/status@mechir_bot", ("status", "")),
    ("/digest", ("digest", "")),
    ("/start", ("help", "")),
])
def test_parse_command(text, expected):
    assert parse_command(text) == expected


class FakeTG:
    def __init__(self, updates=()):
        self.sent, self.updates = [], list(updates)

    def send_long(self, chat_id, text):
        self.sent.append((str(chat_id), text))

    def get_updates(self, offset=None, wait=50):
        out, self.updates = self.updates, []
        return out


def msg(text, chat_id=42, chat_type="private", uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat_id, "type": chat_type}, "text": text}}


def test_only_your_private_chat_is_obeyed(tmp_path):
    ran = []
    tg = FakeTG()
    lst = Listener(tg, tmp_path, "42", runner=lambda task, d: ran.append(task) or "ok", out_dir=tmp_path / "o")
    lst.handle(msg("/manager do it", chat_id=99))                  # someone else
    lst.handle(msg("/manager do it", chat_id=42, chat_type="group"))  # a group with the same id
    lst.handle({"update_id": 3, "channel_post": {"chat": {"id": 42}, "text": "/manager x"}})
    assert ran == [] and tg.sent == []


def test_manager_task_runs_once_and_reports_back(tmp_path):
    gate = threading.Event()

    def runner(task, project):
        gate.wait(5)
        return f"סיכום: {task}"

    tg = FakeTG()
    lst = Listener(tg, tmp_path, "42", runner=runner, out_dir=tmp_path / "o")
    lst.handle(msg("/manager תכין 3 טיוטות"))
    lst.handle(msg("/manager עוד משימה"))                         # while busy -> told to wait
    gate.set()
    lst.busy.join(5)
    texts = [t for _, t in tg.sent]
    assert any("קיבלתי" in t for t in texts) and any("עוד עובד" in t for t in texts)
    assert any(t.startswith("📋 סיכום המנהל") and "סיכום: תכין 3 טיוטות" in t for t in texts)
    assert all(cid == "42" for cid, _ in tg.sent)
    [saved] = list((tmp_path / "o").glob("*.md"))
    assert "תכין 3 טיוטות" in saved.read_text(encoding="utf-8")


def test_missing_claude_is_explained(tmp_path, monkeypatch):
    monkeypatch.setattr(remote.shutil, "which", lambda name: None)
    tg = FakeTG()
    lst = Listener(tg, tmp_path, "42", out_dir=tmp_path / "o")
    lst.handle(msg("/manager x"))
    lst.busy.join(5)
    assert any("Claude Code לא מותקן" in t for _, t in tg.sent)


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell script as a stand-in for claude")
def test_task_text_goes_on_stdin_not_on_the_command_line(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(f"#!{sys.executable}\nimport sys\nprint('ARGS:', sys.argv[1:])\nprint('STDIN:', sys.stdin.read())\n",
                    encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    out = run_claude('בדוק" & del C:\\ & echo "', tmp_path, exe=str(fake))
    args_line = next(ln for ln in out.splitlines() if ln.startswith("ARGS:"))
    assert "del C:" not in args_line and "del C:" in out          # only in stdin
    assert "--permission-mode', 'acceptEdits'" in args_line and "manager.md" in out


def test_allowed_tools_have_no_commit_push_or_free_shell():
    assert "git push" not in ALLOWED_TOOLS and "git commit" not in ALLOWED_TOOLS
    assert "Bash" not in ALLOWED_TOOLS.split(",")                     # never the unrestricted shell
    assert all(t.startswith("Bash(") for t in ALLOWED_TOOLS.split(",") if t.startswith("Bash"))


def test_listen_loop_saves_the_offset_before_handling(tmp_path):
    stop = threading.Event()
    saved = []
    tg = FakeTG([msg("/help", uid=7), msg("/help", uid=8)])

    class StopAfter(Listener):
        def handle(self, update):
            super().handle(update)
            if update["update_id"] == 8:
                stop.set()

    lst = StopAfter(tg, tmp_path, "42", out_dir=tmp_path / "o")
    listen_forever(lst, lambda: None, saved.append, stop)
    assert saved == [8, 9] and sum("/manager" in t for _, t in tg.sent) == 2


def test_send_long_splits_at_lines(monkeypatch):
    from affiliate_scraper.telegram import TelegramPublisher
    tg = TelegramPublisher(token="123:x", env={})
    calls = []
    monkeypatch.setattr(tg, "_api", lambda method, payload, **kw: calls.append(payload["text"]) or {})
    tg.send_long("42", "\n".join(f"line {i} " + "x" * 90 for i in range(100)), limit=1000)
    assert len(calls) > 1 and all(len(c) <= 1000 for c in calls)
    assert "".join(calls).count("line ") == 100


def test_install_puts_a_hidden_starter_in_the_startup_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    assert main(["telegram-listen", "--install"]) == 0
    vbs = tmp_path / "AppData" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / \
        "WebScrapingTelegramManager.vbs"
    assert vbs.exists() and "run_telegram_manager.bat" in vbs.read_text(encoding="utf-16")
    assert "telegram-listen" in (tmp_path / "run_telegram_manager.bat").read_text(encoding="utf-8")
    assert main(["telegram-listen", "--uninstall"]) == 0 and not vbs.exists()


def test_listener_needs_the_bot_and_your_chat(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["telegram-listen"]) == 2
    assert "TELEGRAM_ADMIN_CHAT" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell script as a stand-in for claude")
def test_manager_model_is_passed_and_checked(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(f"#!{sys.executable}\nimport sys\nprint('ARGS:', sys.argv[1:])\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    assert "'--model', 'sonnet'" in run_claude("x", tmp_path, exe=str(fake))          # the default
    assert "'--model', 'opus'" in run_claude("x", tmp_path, exe=str(fake), model="opus")
    assert "--model" not in run_claude("x", tmp_path, exe=str(fake), model=None)       # the account's default
    with pytest.raises(ValueError):
        run_claude("x", tmp_path, exe=str(fake), model="sonnet; rm -rf /")


def test_agents_have_the_planned_models():
    import re
    root = Path(__file__).parent.parent / ".claude" / "agents"      # (no PyYAML on the user's computer)
    models = {p.stem: re.search(r"^model:\s*(\S+)\s*$", p.read_text(encoding="utf-8").split("---\n")[1], re.M).group(1)
              for p in root.glob("*.md")}
    assert models == {"data-keeper": "sonnet", "web-scanner": "sonnet", "price-matcher": "sonnet",
                      "instagram-marketer": "sonnet", "funnel-analyst": "sonnet", "qa-reviewer": "opus"}


class ButtonTG(FakeTG):
    def __init__(self):
        super().__init__()
        self.answered, self.cleared = [], []

    def answer_button(self, cid, text=""):
        self.answered.append((cid, text))

    def clear_buttons(self, chat_id, message_id):
        self.cleared.append(message_id)


def press(data, from_id=42, chat_id=42, chat_type="private"):
    return {"update_id": 9, "callback_query": {"id": "cb1", "data": data, "from": {"id": from_id},
                                               "message": {"message_id": 77, "chat": {"id": chat_id, "type": chat_type}}}}


def test_button_press_runs_the_action_once(tmp_path):
    done = []
    tg = ButtonTG()
    lst = Listener(tg, tmp_path, "42", out_dir=tmp_path / "o",
                   actions={"cpost": lambda i: done.append(i) or f"✅ פורסם {i}"})
    lst.handle(press("cpost:12"))
    assert done == [12] and tg.cleared == [77] and any("✅ פורסם 12" in t for _, t in tg.sent)


def test_button_press_from_anyone_else_does_nothing(tmp_path):
    done = []
    tg = ButtonTG()
    lst = Listener(tg, tmp_path, "42", out_dir=tmp_path / "o", actions={"cpost": lambda i: done.append(i) or "x"})
    lst.handle(press("cpost:12", from_id=99))                        # someone else's press
    lst.handle(press("cpost:12", chat_id=-1005, chat_type="channel"))  # a press in the channel
    lst.handle(press("rm:12"))                                       # unknown action
    lst.handle(press("cpost:12; drop table"))                        # not an id
    assert done == [] and tg.cleared == [] and tg.sent == []


def test_failed_action_is_told_not_crashing(tmp_path):
    tg = ButtonTG()

    def boom(i):
        raise RuntimeError("Telegram is down")

    lst = Listener(tg, tmp_path, "42", out_dir=tmp_path / "o", actions={"dpub": boom})
    lst.handle(press("dpub:3"))
    assert any("נכשל" in t and "Telegram is down" in t for _, t in tg.sent)


def test_claude_found_outside_path(tmp_path, monkeypatch):
    from affiliate_scraper.remote import find_claude
    monkeypatch.setattr(remote.shutil, "which", lambda name: None)       # not on PATH (installed today)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert find_claude() is None
    exe = tmp_path / ".local" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert find_claude() == str(exe)


def test_task_command_and_still_working_messages(tmp_path):
    gate = threading.Event()
    tg = FakeTG()
    lst = Listener(tg, tmp_path, "42", runner=lambda task, d: gate.wait(5) and "סיכום", out_dir=tmp_path / "o")
    lst.heartbeat = 0.05
    lst.handle(msg("/task"))
    assert "פנוי" in tg.sent[-1][1]
    lst.handle(msg("/manager משימה ארוכה"))
    import time
    time.sleep(0.2)
    lst.handle(msg("/task"))
    assert any("עובד כבר" in t and "משימה ארוכה" in t for _, t in tg.sent)
    assert any("עדיין עובד" in t for _, t in tg.sent)                 # the heartbeat
    gate.set()
    lst.busy.join(5)
    n = len(tg.sent)
    time.sleep(0.2)
    assert len(tg.sent) == n                                          # no heartbeat after the summary
