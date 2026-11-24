"""Graphics commands the Telegram manager may run: profile pictures and HyperFrames reels."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from affiliate_scraper import media
from affiliate_scraper.media import MediaError, profile_pics, reel_command, reel_dir, reel_new, reel_run
from affiliate_scraper.telegram import TelegramError, TelegramPublisher

ROOT = Path(__file__).resolve().parents[1]
LOGO = ROOT / "video" / "first-reel" / "assets" / "logo.png"


def test_profile_pics_are_square_and_have_a_round_preview(tmp_path):
    files = profile_pics(LOGO, tmp_path)
    assert [f.name for f in files] == ["profile-a-full-logo.png", "profile-b-cart.png", "preview-circle.png"]
    for f in files[:2]:
        assert Image.open(f).size == (1080, 1080)
    a = Image.open(files[0]).convert("RGB")
    assert a.getpixel((5, 5)) == (10, 20, 48)            # the logo's black corners are gone
    with pytest.raises(MediaError):
        profile_pics(tmp_path / "nope.png", tmp_path)


@pytest.mark.parametrize("bad", ["../x", "..", "a/b", "Week", "x", "", "C:\\temp", "a b"])
def test_reel_name_stays_inside_video(bad):
    with pytest.raises(MediaError):
        reel_dir(ROOT, bad)


def test_reel_new_copies_the_template_without_renders(tmp_path):
    tpl = tmp_path / "video" / "first-reel"
    (tpl / "renders").mkdir(parents=True)
    (tpl / ".hyperframes").mkdir()
    (tpl / "compositions").mkdir()
    (tpl / "index.html").write_text("<html></html>", encoding="utf-8")
    (tpl / "compositions" / "01.html").write_text("x", encoding="utf-8")
    (tpl / "renders" / "old.mp4").write_bytes(b"0")
    (tpl / "CAPTION.md").write_text("old caption", encoding="utf-8")
    dst = reel_new(tmp_path, "week-48")
    assert (dst / "index.html").is_file() and (dst / "compositions" / "01.html").is_file()
    assert (dst / "renders").is_dir() and not list((dst / "renders").iterdir())
    assert not (dst / ".hyperframes").exists() and not (dst / "CAPTION.md").exists()
    with pytest.raises(MediaError):
        reel_new(tmp_path, "week-48")                    # never overwrites


def test_reel_command_is_pinned_and_checks_its_input(tmp_path):
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    assert reel_command("check", tmp_path, npx="npx")[:4] == ["npx", "-y", media.HYPERFRAMES, "check"]
    assert reel_command("snapshot", tmp_path, "1,3.5", npx="npx")[-2:] == ["--at", "1,3.5"]
    with pytest.raises(MediaError):
        reel_command("snapshot", tmp_path, "1; rm -rf /", npx="npx")
    with pytest.raises(MediaError):
        reel_command("publish", tmp_path, npx="npx")
    with pytest.raises(MediaError):
        reel_command("check", tmp_path / "missing", npx="npx")


def test_reel_render_returns_the_video(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    monkeypatch.setattr(media, "find_npx", lambda: "npx")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, cwd=kw["cwd"], env=kw["env"])
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stdout="[hyperframes] progress\nrendered\n")

    code, tail, mp4 = reel_run("render", tmp_path, runner=fake_run)
    assert code == 0 and mp4.is_file() and mp4.parent == tmp_path / "renders"
    assert tail == "rendered" and seen["cwd"] == str(tmp_path) and seen["env"]["HYPERFRAMES_NO_TELEMETRY"] == "1"
    fail = reel_run("render", tmp_path, runner=lambda c, **k: SimpleNamespace(returncode=2, stdout="boom"))
    assert fail[0] == 2 and fail[2] is None

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    assert reel_run("check", tmp_path, runner=slow)[0] == 124


def test_send_file_to_owner_but_never_to_the_channel_from_the_manager(tmp_path, monkeypatch):
    video = tmp_path / "r.mp4"
    video.write_bytes(b"mp4")
    tg = TelegramPublisher(token="123:" + "A" * 35, channel="@deals_il", admin_chat="42", min_interval=0)
    calls = []
    monkeypatch.setattr(tg, "session", SimpleNamespace(post=lambda url, **kw: calls.append((url, kw)) or
                                                        SimpleNamespace(status_code=200, json=lambda: {"ok": True,
                                                                                                      "result": {}})))
    tg.send_file("42", video, "draft")
    assert calls[-1][0].endswith("/sendVideo") and "video" in calls[-1][1]["files"]
    monkeypatch.setenv("MECHIR_AGENT_RUN", "1")
    with pytest.raises(TelegramError):
        tg.send_file("@deals_il", video)                 # the public channel stays the owner's


def test_reel_refuses_a_planted_local_package(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    monkeypatch.setattr(media, "find_npx", lambda: "npx")
    with pytest.raises(MediaError):
        reel_run("check", tmp_path, runner=lambda c, **k: SimpleNamespace(returncode=0, stdout=""))


def test_inside_project_only(tmp_path):
    assert media.inside_project(tmp_path, "brand") == (tmp_path / "brand").resolve()
    for bad in ("../x", "/etc", "brand/../../x"):
        with pytest.raises(MediaError):
            media.inside_project(tmp_path, bad)


# ---------------------------------------------------------------- the commands: files go to the OWNER only
class FakeTG:
    sent: list = []

    def __init__(self, admin_chat="42", fail=False):
        self.admin_chat, self.channel, self.fail = admin_chat, "@deals_il", fail

    def send_file(self, chat_id, path, caption=""):
        if self.fail:
            raise TelegramError("network down")
        FakeTG.sent.append(("file", chat_id, Path(path).name))

    def send_album(self, chat_id, paths, caption=""):
        if self.fail:
            raise TelegramError("network down")
        FakeTG.sent.append(("album", chat_id, len(paths)))


def _run_cli(monkeypatch, tmp_path, argv, tg):
    from affiliate_scraper import telegram
    from affiliate_scraper.cli import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(telegram, "TelegramPublisher", lambda: tg)
    FakeTG.sent = []
    return main(argv)


def test_profile_pic_command_sends_to_the_owner(tmp_path, monkeypatch, capsys):
    logo = tmp_path / "logo.png"
    logo.write_bytes(LOGO.read_bytes())
    assert _run_cli(monkeypatch, tmp_path, ["profile-pic", "--logo", "logo.png", "--telegram"], FakeTG()) == 0
    assert [c[1] for c in FakeTG.sent] == ["42", "42", "42"]
    assert _run_cli(monkeypatch, tmp_path, ["profile-pic", "--logo", "logo.png", "--out", "../x"], FakeTG()) == 2
    assert _run_cli(monkeypatch, tmp_path, ["profile-pic", "--logo", "logo.png", "--telegram"], FakeTG("")) == 0
    assert FakeTG.sent == []                              # no private chat set -> nothing is sent anywhere


def test_reel_commands_send_to_the_owner_and_report_telegram_errors(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "video" / "week-48"
    (proj / "renders").mkdir(parents=True)
    (proj / "index.html").write_text("x", encoding="utf-8")
    mp4 = proj / "renders" / "week-48.mp4"

    def fake_reel_run(action, project, at=""):
        if action == "render":
            mp4.write_bytes(b"0")
            return 0, "done", mp4
        shot = project / ".hyperframes" / "artifacts" / "snapshots" / "run1"
        shot.mkdir(parents=True, exist_ok=True)
        (shot / "frame-00-at-1s.png").write_bytes(b"0")
        return 0, "done", None

    monkeypatch.setattr(media, "reel_run", fake_reel_run)
    assert _run_cli(monkeypatch, tmp_path, ["reel", "render", "week-48", "--telegram"], FakeTG()) == 0
    assert FakeTG.sent == [("file", "42", "week-48.mp4")]
    assert _run_cli(monkeypatch, tmp_path, ["reel", "snapshot", "week-48", "--telegram"], FakeTG()) == 0
    assert FakeTG.sent == [("album", "42", 1)]
    # Telegram down -> a clear message and exit 1, not a traceback
    assert _run_cli(monkeypatch, tmp_path, ["reel", "snapshot", "week-48", "--telegram"], FakeTG(fail=True)) == 1
    assert "Telegram problem" in capsys.readouterr().err
    assert _run_cli(monkeypatch, tmp_path, ["reel", "check", "../etc"], FakeTG()) == 2
