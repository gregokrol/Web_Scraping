"""The global frame-picker skill (global-skills/frame-picker): best frames from a video, and image edits."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

SKILL = Path(__file__).resolve().parents[1] / "global-skills" / "frame-picker" / "scripts"
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


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
