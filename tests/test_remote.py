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
    # find_claude also looks where the installers put it (~\.local\bin, %APPDATA%, %LOCALAPPDATA%): point all of
    # those at an empty folder, so a computer that HAS Claude Code still runs this as "not installed"
    for var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "empty"))
    assert remote.find_claude() is None
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


def test_agent_run_cannot_publish_to_the_channel_or_the_site(monkeypatch, tmp_path):
    from affiliate_scraper.site import SiteConfig, publish_site
    from affiliate_scraper.telegram import TelegramError, TelegramPublisher
    tg = TelegramPublisher(token="123:x", channel="@deals_il", admin_chat="42", env={})
    calls = []
    monkeypatch.setattr(tg, "session", type("S", (), {"post": lambda self, url, **kw: calls.append(url) or
                                                      type("R", (), {"json": lambda s: {"ok": True, "result": {}}})()})())
    monkeypatch.setenv("MECHIR_AGENT_RUN", "1")
    for method in ("sendMessage", "sendPhoto", "pinChatMessage", "deleteMessage"):
        with pytest.raises(TelegramError, match="blocked while the agent manager runs"):
            tg._api(method, {"chat_id": "@deals_il", "text": "x"})
    tg._api("sendMessage", {"chat_id": "42", "text": "to the owner is fine"})
    assert len(calls) == 1                                           # only the private message went out
    with pytest.raises(RuntimeError, match="blocked while the agent manager runs"):
        publish_site(SiteConfig(out_dir=str(tmp_path), publish_branch="gh-pages"))
    monkeypatch.delenv("MECHIR_AGENT_RUN")
    tg._api("sendMessage", {"chat_id": "@deals_il", "text": "the owner's own publish works"})
    assert len(calls) == 2


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell script as a stand-in for claude")
def test_claude_runs_with_the_lock(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(f"#!{sys.executable}\nimport os\nprint('LOCK=' + os.environ.get('MECHIR_AGENT_RUN', ''))\n",
                    encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    assert "LOCK=1" in run_claude("x", tmp_path, exe=str(fake))


def test_progress_reports_and_task_shows_them(tmp_path, monkeypatch, capsys):
    from affiliate_scraper.commands import progress_line, read_progress
    monkeypatch.chdir(tmp_path)
    assert main(["progress", "plan", "3 משימות: web-scanner, QA", "--no-telegram"]) == 0
    assert main(["progress", "qa-fail", "web-scanner סבב 1: 2 הערות", "--no-telegram"]) == 0
    entries = read_progress()
    assert [e["kind"] for e in entries] == ["plan", "qa-fail"]
    assert progress_line(entries[1]) == "🔍 QA: FAIL: web-scanner סבב 1: 2 הערות"
    gate = threading.Event()
    tg = FakeTG()
    lst = Listener(tg, tmp_path, "42", runner=lambda t, d: gate.wait(5) and "ok", out_dir=tmp_path / "o")
    lst.progress = lambda: [progress_line(p) for p in read_progress(since=lst.started_at)]
    lst.handle(msg("/manager משימה"))
    import time
    time.sleep(0.1)
    assert main(["progress", "start", "price-matcher: בדיקת השוואות", "--no-telegram"]) == 0
    lst.handle(msg("/task"))
    shown = tg.sent[-1][1]
    assert "▶️ התחיל: price-matcher" in shown and "QA: FAIL" not in shown   # only the current task's stages
    gate.set()
    lst.busy.join(5)


def test_progress_kind_is_checked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["progress", "deleted-everything", "x"])


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell script as a stand-in for claude")
def test_only_the_final_summary_is_returned(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(f"#!{sys.executable}\nprint('ה-QA עדיין רץ.')\nprint('זו הודעת סיום של המתנה ישנה.')\n"
                    "print('## סיכום: המשימה')\nprint('מה נעשה: הכול')\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    out = run_claude("x", tmp_path, exe=str(fake))
    assert out.startswith("## סיכום: המשימה") and "המתנה ישנה" not in out


def test_disclosure_does_not_claim_links_that_do_not_exist_yet():
    from affiliate_scraper.content import DISCLOSURE
    from affiliate_scraper.site import DISCLOSURE as SITE
    assert "ייתכן" in DISCLOSURE["he"] and "ייתכן" in SITE and "כולל קישורי שותפים" not in DISCLOSURE["he"] + SITE


def test_every_project_skill_and_agent_loads():
    """Claude Code reads the YAML header of each skill/agent - a broken one is silently skipped."""
    import re
    root = Path(__file__).parent.parent / ".claude"
    files = list((root / "skills").glob("*/SKILL.md")) + list((root / "agents").glob("*.md"))
    assert len(files) >= 21
    for f in files:
        text = f.read_text(encoding="utf-8")
        m = re.match(r"---\n(.*?)\n---\n", text, re.S)
        assert m, f
        head = m.group(1)
        assert re.search(r"^name:\s*\S", head, re.M) and re.search(r"^description:\s*\S", head, re.M), f
        for line in head.splitlines():                     # an unquoted ": " inside a value breaks the YAML
            key, _, value = line.partition(": ")
            if key in ("name", "description") and not value.startswith('"'):
                assert ": " not in value, (f, line)
    for s in ("social", "copywriting", "video"):            # the owner's rules come first in the borrowed skills
        assert "כללי הבעלים קודמים" in (root / "skills" / s / "SKILL.md").read_text(encoding="utf-8")


def test_every_press_in_a_batch_is_answered_before_the_slow_actions_run(tmp_path):
    from affiliate_scraper.remote import listen_forever
    order = []
    tg = ButtonTG()
    tg.answer_button = lambda cid, text="": order.append(("answer", cid))
    a, b = press("dpub:1"), press("dpub:2")
    a["update_id"], b["update_id"] = 1, 2
    a["callback_query"]["id"], b["callback_query"]["id"] = "cbA", "cbB"
    stop = threading.Event()
    batches = [[a, b]]

    def get_updates(offset):
        if not batches:
            stop.set()
            return []
        return batches.pop()
    tg.get_updates = get_updates
    lst = Listener(tg, tmp_path, "42", out_dir=tmp_path / "o",
                   actions={"dpub": lambda i: order.append(("action", i)) or "ok"})
    listen_forever(lst, lambda: None, lambda o: None, stop)
    assert order == [("answer", "cbA"), ("answer", "cbB"), ("action", 1), ("action", 2)]   # each answered once


def test_batch_answers_only_presses_that_will_run_each_once(tmp_path):
    tg = ButtonTG()
    done = []
    lst = Listener(tg, tmp_path, "42", out_dir=tmp_path / "o", actions={"dpub": lambda i: done.append(i) or "ok"})
    other = press("dpub:1", from_id=99)
    group = press("dpub:2", chat_id=-100, chat_type="group")
    unknown = press("rm:3")
    good = press("dpub:4")
    for u, cid in ((other, "o"), (group, "g"), (unknown, "u"), (good, "ok")):
        u["callback_query"]["id"] = cid
    batch = [other, group, unknown, good]
    lst.ack_buttons(batch)
    assert tg.answered == [("ok", "מבצע...")]                       # only the press that will run
    for u in batch:
        lst.handle(u)
    ids = [cid for cid, _ in tg.answered]
    assert sorted(ids) == ["g", "o", "ok", "u"] and len(ids) == len(set(ids))   # each press answered once
    assert done == [4] and lst._acked == set()


def test_failed_batch_answer_is_retried_by_the_handler(tmp_path):
    tg = ButtonTG()
    calls = []

    def flaky(cid, text=""):
        calls.append(cid)
        if len(calls) == 1:
            raise RuntimeError("network")
    tg.answer_button = flaky
    lst = Listener(tg, tmp_path, "42", out_dir=tmp_path / "o", actions={"dpub": lambda i: "ok"})
    u = press("dpub:5")
    lst.ack_buttons([u])
    lst.handle(u)
    assert calls == ["cb1", "cb1"]


# ------------------------------------------------------------------ pictures with a task
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class PhotoTG(FakeTG):
    def __init__(self, files):
        super().__init__()
        self.files, self.downloaded = files, []

    def download_file(self, file_id, max_bytes=0):
        self.downloaded.append(file_id)
        return self.files[file_id]


def photo_msg(file_id, caption=None, chat_id=42, chat_type="private", group=None, size=1000):
    m = {"chat": {"id": chat_id, "type": chat_type},
         "photo": [{"file_id": "small", "width": 90, "height": 90, "file_size": 10},
                   {"file_id": file_id, "width": 1080, "height": 1920, "file_size": size}]}
    if caption is not None:
        m["caption"] = caption
    if group:
        m["media_group_id"] = group
    return {"update_id": 5, "message": m}


def wait_task(lst):
    for _ in range(200):          # up to 10 s for the task to start (an album waits album_wait first)
        if lst.busy:
            lst.busy.join(5)
            return
        threading.Event().wait(0.05)


def test_photo_with_manager_caption_is_saved_and_given_to_the_manager(tmp_path):
    ran = []
    tg = PhotoTG({"big": JPEG})
    lst = Listener(tg, tmp_path, "42", runner=lambda task, d: ran.append(task) or "ok", out_dir=tmp_path / "o")
    lst.handle(photo_msg("big", caption="/manager נתוני האינסטגרם של השבוע"))
    wait_task(lst)
    assert tg.downloaded == ["big"]                                  # the largest size, not the thumbnail
    [saved] = list((tmp_path / "agents_output" / "inbox").glob("*.jpg"))
    assert saved.read_bytes() == JPEG
    [task] = ran
    assert task.startswith("נתוני האינסטגרם של השבוע") and f"agents_output/inbox/{saved.name}" in task
    assert any("קיבלתי" in t and "📷" in t for _, t in tg.sent)


def test_photo_without_the_command_gets_a_hint_and_is_not_downloaded(tmp_path):
    ran = []
    tg = PhotoTG({"big": JPEG})
    lst = Listener(tg, tmp_path, "42", runner=lambda task, d: ran.append(task) or "ok", out_dir=tmp_path / "o")
    lst.handle(photo_msg("big"))
    lst.handle(photo_msg("big", caption="מה זה?"))
    assert ran == [] and tg.downloaded == []
    assert len(tg.sent) == 2 and all("/manager" in t for _, t in tg.sent)


def test_photo_from_anyone_else_is_ignored(tmp_path):
    tg = PhotoTG({"big": JPEG})
    lst = Listener(tg, tmp_path, "42", runner=lambda task, d: "ok", out_dir=tmp_path / "o")
    lst.handle(photo_msg("big", caption="/manager x", chat_id=99))
    lst.handle(photo_msg("big", caption="/manager x", chat_type="group"))
    assert tg.downloaded == [] and tg.sent == []


def test_an_album_is_one_task_with_all_its_photos(tmp_path):
    ran = []
    tg = PhotoTG({"a": JPEG, "b": PNG, "c": JPEG})
    lst = Listener(tg, tmp_path, "42", runner=lambda task, d: ran.append(task) or "ok", out_dir=tmp_path / "o")
    lst.album_wait = 2.0          # long enough that a busy test machine (full suite) does not split it
    lst.handle(photo_msg("a", group="G1"))
    lst.handle(photo_msg("b", caption="/manager שלוש תמונות", group="G1"))   # the caption on one photo only
    lst.handle(photo_msg("c", group="G1"))
    wait_task(lst)
    assert len(ran) == 1 and ran[0].startswith("שלוש תמונות")
    assert ran[0].count("agents_output/inbox/") == 3 and ".png" in ran[0]
    assert tg.downloaded == ["a", "b", "c"]


def test_a_file_that_is_not_a_picture_is_refused(tmp_path):
    ran = []
    tg = PhotoTG({"fake": b"MZ\x90\x00 an exe pretending"})
    lst = Listener(tg, tmp_path, "42", runner=lambda task, d: ran.append(task) or "ok", out_dir=tmp_path / "o")
    lst.handle(photo_msg("fake", caption="/manager x"))
    wait_task(lst)
    assert ran == [] and not list((tmp_path / "agents_output" / "inbox").glob("*"))
    assert any("לא הצלחתי" in t for _, t in tg.sent)
    # a pdf or video is not taken as a picture at all - and the task is not started without it
    lst.handle({"update_id": 6, "message": {"chat": {"id": 42, "type": "private"}, "caption": "/manager y",
                                            "document": {"file_id": "d", "mime_type": "application/pdf"}}})
    assert ran == [] and "d" not in tg.downloaded and "רק תמונות" in tg.sent[-1][1]


def test_picture_sent_as_a_file_is_accepted_but_its_name_is_not_used(tmp_path):
    ran = []
    tg = PhotoTG({"d": PNG})
    lst = Listener(tg, tmp_path, "42", runner=lambda task, d: ran.append(task) or "ok", out_dir=tmp_path / "o")
    lst.handle({"update_id": 7, "message": {"chat": {"id": 42, "type": "private"}, "caption": "/manager z",
                                            "document": {"file_id": "d", "mime_type": "image/png",
                                                         "file_name": "../../evil.bat"}}})
    wait_task(lst)
    [saved] = list((tmp_path / "agents_output" / "inbox").glob("*"))
    assert saved.suffix == ".png" and "evil" not in saved.name and len(ran) == 1


def test_too_many_or_too_big_pictures_are_refused(tmp_path):
    ran = []
    tg = PhotoTG({"big": JPEG})
    lst = Listener(tg, tmp_path, "42", runner=lambda task, d: ran.append(task) or "ok", out_dir=tmp_path / "o")
    lst.handle(photo_msg("big", caption="/manager x", size=remote.MAX_IMAGE_BYTES + 1))
    lst.command("/manager x", [{"file_id": "big", "size": 1}] * (remote.MAX_IMAGES + 1))
    assert ran == [] and tg.downloaded == []
    assert "גדולה מדי" in tg.sent[0][1] and "יותר מדי תמונות" in tg.sent[1][1]


def test_download_file_uses_the_file_url_and_has_a_size_limit():
    from types import SimpleNamespace
    from affiliate_scraper.telegram import TelegramError, TelegramPublisher

    class Resp:
        def __init__(self, body, status=200):
            self.body, self.status_code = body, status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, n):
            for i in range(0, len(self.body), n):
                yield self.body[i:i + n]

    class Bot:
        def __init__(self, size=None, body=JPEG):
            self.size, self.body, self.got = size, body, []

        def post(self, url, json=None, **kw):
            assert url.endswith("/getFile") and json == {"file_id": "F"}
            result = {"file_path": "photos/file_1.jpg"}
            if self.size is not None:
                result["file_size"] = self.size
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": result})

        def get(self, url, stream=False, timeout=None):
            self.got.append(url)
            return Resp(self.body)

    bot = Bot()
    tg = TelegramPublisher(token="123:" + "A" * 35, admin_chat="42", session=bot, min_interval=0, env={})
    assert tg.download_file("F") == JPEG
    assert bot.got == [f"https://api.telegram.org/file/bot123:{'A' * 35}/photos/file_1.jpg"]
    with pytest.raises(TelegramError, match="too large"):
        TelegramPublisher(token="123:" + "A" * 35, session=Bot(size=50), min_interval=0, env={}).download_file("F", 10)
    with pytest.raises(TelegramError, match="too large"):          # no size given: stopped while reading
        TelegramPublisher(token="123:" + "A" * 35, session=Bot(body=b"x" * 100), min_interval=0,
                          env={}).download_file("F", 10)


@pytest.mark.parametrize("caption", ["מנהל: נתוני השבוע", "מנהל נתוני השבוע", "/m נתוני השבוע",
                                     "/manager@mechir_bot נתוני השבוע"])
def test_every_manager_form_works_in_a_caption(tmp_path, caption):
    ran = []
    tg = PhotoTG({"big": JPEG})
    lst = Listener(tg, tmp_path, "42", runner=lambda task, d: ran.append(task) or "ok", out_dir=tmp_path / "o")
    lst.handle(photo_msg("big", caption=caption))
    wait_task(lst)
    assert len(ran) == 1 and ran[0].startswith("נתוני השבוע") and "agents_output/inbox/" in ran[0]
