"""The global frame-picker skill (global-skills/frame-picker): best frames from a video, and image edits."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

SKILL = Path(__file__).resolve().parents[1] / "global-skills" / "frame-picker" / "scripts"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def run(script, *args):
    res = subprocess.run([sys.executable, str(SKILL / script), *map(str, args)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return res.stdout


@pytest.fixture()
def video(tmp_path):
    """4 s: 1 s black, 1 s blurred noise, 2 s a sharp still test pattern."""
    v = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=black:s=320x240:d=1",
                    "-f", "lavfi", "-i", "nullsrc=s=320x240:d=1,geq=random(1)*255:128:128,boxblur=8",
                    "-f", "lavfi", "-i", "testsrc=s=320x240:d=2:r=25,trim=end=2,select='eq(n,0)',loop=60:1",
                    "-filter_complex", "[0][1][2]concat=n=3:v=1,format=yuv420p,fps=25",
                    str(v)], check=True)
    return v


@needs_ffmpeg
def test_best_frames_are_sharp_and_never_black(video, tmp_path):
    out = json.loads(run("frames.py", video, "--out", tmp_path / "f", "--top", "3", "--min-gap", "0.5"))
    assert out["size"] == "320x240" and out["frames_checked"] >= 12
    best = out["best"][0]
    assert best["time"] >= 2.0                                   # the sharp still, not the black or the blur
    assert all(b["brightness"] > 0.06 for b in out["best"])      # no black frame
    assert Image.open(best["file"]).size == (320, 240)           # saved at full size
    assert Path(out["contact_sheet"]).is_file()


def test_edit_makes_a_new_file_and_never_overwrites(tmp_path):
    src = tmp_path / "a.png"
    Image.new("RGB", (1000, 600), (90, 120, 160)).save(src)
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (100, 100), (255, 0, 0, 255)).save(logo)
    line = run("edit_image.py", src, "-o", tmp_path / "out.jpg", "--aspect", "4:5", "--resize", "400",
               "--auto", "--saturation", "1.1", "--watermark", logo)
    assert Image.open(tmp_path / "out.jpg").size == (400, 500) and "aspect 4:5" in line
    assert Image.open(src).size == (1000, 600)                    # the source is untouched
    res = subprocess.run([sys.executable, str(SKILL / "edit_image.py"), str(src), "-o", str(src)],
                         capture_output=True, text=True)
    assert res.returncode != 0 and "never overwritten" in res.stderr


def _make(tmp_path, name, *lavfi_and_filters):
    v = tmp_path / name
    subprocess.run(["ffmpeg", "-v", "error", "-y", *lavfi_and_filters, str(v)], check=True)
    return v


@needs_ffmpeg
def test_black_video_is_a_clear_error_not_a_crash(tmp_path):
    v = _make(tmp_path, "black.mp4", "-f", "lavfi", "-i", "color=black:s=160x120:d=1", "-pix_fmt", "yuv420p")
    res = subprocess.run([sys.executable, str(SKILL / "frames.py"), str(v), "--out", str(tmp_path / "o")],
                         capture_output=True, text=True)
    assert res.returncode != 0 and "black" in res.stderr and "Traceback" not in res.stderr


@needs_ffmpeg
def test_rotated_phone_video_start_end_and_hebrew_path(tmp_path):
    v = _make(tmp_path, "src.mp4", "-f", "lavfi", "-i", "testsrc=s=320x180:d=3:r=10", "-pix_fmt", "yuv420p")
    heb = tmp_path / "סרטון מהטלפון.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(v), "-c", "copy",
                    str(heb)], check=True)
    out = json.loads(run("frames.py", heb, "--out", tmp_path / "f", "--start", "1", "--end", "2.5",
                         "--top", "2", "--min-gap", "0.2"))
    assert out["size"] == "180x320"                              # upright, as it was filmed
    assert all(1 <= b["time"] <= 2.5 for b in out["best"])
    assert Image.open(out["best"][0]["file"]).size == (180, 320)
    res = subprocess.run([sys.executable, str(SKILL / "frames.py"), str(heb), "--start", "2", "--end", "1"],
                         capture_output=True, text=True)
    assert res.returncode != 0 and "--end later than --start" in res.stderr



def test_edit_crop_focus_and_bad_values(tmp_path):
    src = tmp_path / "b.png"
    Image.new("RGB", (800, 800), (40, 40, 40)).save(src)
    run("edit_image.py", src, "-o", tmp_path / "c.png", "--crop", "100,100,300,200")
    assert Image.open(tmp_path / "c.png").size == (300, 200)
    run("edit_image.py", src, "-o", tmp_path / "f.png", "--aspect", "9:16", "--focus", "0.9,0.5")
    assert Image.open(tmp_path / "f.png").size == (450, 800)
    for bad in (["--crop", "700,700,300,300"], ["-o", str(tmp_path / "noext")]):
        args = [sys.executable, str(SKILL / "edit_image.py"), str(src)]
        args += bad if bad[0] == "-o" else ["-o", str(tmp_path / "x.png"), *bad]
        res = subprocess.run(args, capture_output=True, text=True)
        assert res.returncode != 0 and "Traceback" not in res.stderr
