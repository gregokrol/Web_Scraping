"""Find the best frames in a video and save them as separate images.

    python frames.py VIDEO [--out DIR] [--top 5] [--fps 4] [--min-gap 1.0] [--start S] [--end S]

Every sampled frame gets a score from things a person would also look at:
  * sharp, not blurred (variance of the Laplacian)
  * well exposed (not too dark / too bright, few burnt-out or black pixels)
  * contrast (standard deviation of brightness)
  * steady (little change from the frames around it: no motion blur, no cut or fade in progress)
The best ones, at least --min-gap seconds apart, are saved at FULL resolution as PNG, plus a contact sheet
(contact-sheet.jpg) with each candidate's time and score - look at it to choose the final one.
Prints a JSON summary. Needs ffmpeg (on PATH, or the pip package imageio-ffmpeg), numpy and Pillow.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

SCORE_W = 320            # frames are scored small (fast); the chosen ones are saved at full size


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        sys.exit("ffmpeg not found: install it (https://ffmpeg.org) or run: pip install imageio-ffmpeg")


def video_info(ff: str, video: Path) -> tuple[int, int, float]:
    """(width, height, duration seconds) read from ffmpeg's own report - works without ffprobe."""
    err = subprocess.run([ff, "-hide_banner", "-i", str(video)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    if not m:
        sys.exit(f"not a video ffmpeg can read: {video}")
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0.0
    rot = re.search(r"rotate\s*:\s*(-?\d+)|displaymatrix: rotation of (-?[\d.]+)", err)
    w, h = int(m.group(1)), int(m.group(2))
    if rot and abs(float(rot.group(1) or rot.group(2))) % 180 == 90:
        w, h = h, w                     # phone video stored sideways: ffmpeg turns it upright when decoding
    return w, h, dur


def sample_gray(ff: str, video: Path, fps: float, w: int, h: int, start: float, end: float):
    sh = max(2, int(round(h * SCORE_W / w / 2)) * 2)
    cmd = [ff, "-v", "error"]
    if start:
        cmd += ["-ss", f"{start}"]
    cmd += ["-i", str(video)]
    if end:
        cmd += ["-t", f"{max(0.1, end - start)}"]
    cmd += ["-vf", f"fps={fps},scale={SCORE_W}:{sh},format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    n = len(raw) // (SCORE_W * sh)
    frames = np.frombuffer(raw[: n * SCORE_W * sh], dtype=np.uint8).reshape(n, sh, SCORE_W).astype(np.float32)
    times = start + np.arange(n) / fps
    return frames, times


def laplacian_var(img: np.ndarray) -> float:
    lap = (-4 * img[1:-1, 1:-1] + img[:-2, 1:-1] + img[2:, 1:-1] + img[1:-1, :-2] + img[1:-1, 2:])
    return float(lap.var())


def score_frames(frames: np.ndarray) -> list[dict]:
    if len(frames) == 0:
        return []
    sharp = np.array([laplacian_var(f) for f in frames])
    mean = frames.mean(axis=(1, 2)) / 255
    contrast = frames.std(axis=(1, 2)) / 255
    clipped = ((frames < 6) | (frames > 249)).mean(axis=(1, 2))
    diff = np.zeros(len(frames))
    if len(frames) > 1:
        d = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2)) / 255
        diff[0], diff[-1] = d[0], d[-1]
        diff[1:-1] = np.maximum(d[:-1], d[1:])

    def norm(x):                         # 0..1 against this video's own range (robust to outliers)
        lo, hi = np.percentile(x, 5), np.percentile(x, 95)
        return np.clip((x - lo) / (hi - lo), 0, 1) if hi > lo else np.full_like(x, 0.5)

    exposure = 1 - np.clip(np.abs(mean - 0.5) / 0.4, 0, 1)
    steady = 1 - norm(diff)
    total = (0.40 * norm(np.log1p(sharp)) + 0.20 * exposure + 0.15 * norm(contrast) + 0.25 * steady
             - 0.5 * np.clip(clipped - 0.15, 0, 1))
    total[mean < 0.06] = -1              # black frames (fades, start/end) never win
    return [{"i": i, "score": round(float(total[i]), 3), "sharp": round(float(sharp[i]), 1),
             "brightness": round(float(mean[i]), 2), "contrast": round(float(contrast[i]), 2),
             "motion": round(float(diff[i]), 3)} for i in range(len(frames))]


def pick(scored: list[dict], times: np.ndarray, top: int, min_gap: float, frames=None,
         min_diff: float = 0.04) -> list[dict]:
    """The best frames, at least ``min_gap`` seconds apart and (with ``frames``) visibly different from the
    ones already chosen - five near-copies of one shot are not five choices."""
    chosen = []
    for s in sorted(scored, key=lambda s: s["score"], reverse=True):
        t = float(times[s["i"]])
        if s["score"] < 0 or any(abs(t - c["time"]) < min_gap for c in chosen):
            continue
        if frames is not None and any(np.abs(frames[s["i"]] - frames[c["i"]]).mean() / 255 < min_diff
                                      for c in chosen):
            continue
        chosen.append({**s, "time": round(t, 3)})
        if len(chosen) == top:
            break
    return chosen


def extract(ff: str, video: Path, t: float, out: Path) -> None:
    subprocess.run([ff, "-v", "error", "-y", "-ss", f"{t}", "-i", str(video), "-frames:v", "1", str(out)],
                   check=True)


def contact_sheet(paths: list[Path], labels: list[str], out: Path, cell_w: int = 360) -> None:
    ims = [Image.open(p).convert("RGB") for p in paths]
    cell_h = max(int(im.height * cell_w / im.width) for im in ims) + 28
    cols = min(len(ims), 5)
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    for k, (im, lab) in enumerate(zip(ims, labels)):
        x, y = (k % cols) * cell_w, (k // cols) * cell_h
        sheet.paste(im.resize((cell_w, int(im.height * cell_w / im.width))), (x, y + 28))
        d.text((x + 6, y + 6), lab, fill=(255, 255, 255))
    sheet.save(out, quality=88)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("video")
    ap.add_argument("--out", default=None, help="folder for the images (default: <video name>-frames next to it)")
    ap.add_argument("--top", type=int, default=5, help="how many best frames to save (default 5)")
    ap.add_argument("--fps", type=float, default=4, help="frames checked per second (default 4)")
    ap.add_argument("--min-gap", type=float, default=1.0, help="seconds between chosen frames (default 1)")
    ap.add_argument("--start", type=float, default=0.0, help="only look from this second")
    ap.add_argument("--end", type=float, default=0.0, help="only look until this second")
    a = ap.parse_args(argv)
    video = Path(a.video)
    if not video.is_file():
        sys.exit(f"no such file: {video}")
    ff = ffmpeg_exe()
    w, h, dur = video_info(ff, video)
    frames, times = sample_gray(ff, video, a.fps, w, h, a.start, a.end)
    if len(frames) == 0:
        sys.exit("no frames could be read from the video")
    chosen = pick(score_frames(frames), times, max(1, a.top), a.min_gap, frames)
    out = Path(a.out) if a.out else video.with_name(video.stem + "-frames")
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for rank, c in enumerate(chosen, 1):
        p = out / f"best-{rank:02d}-at-{c['time']:.2f}s.png"
        extract(ff, video, c["time"], p)
        c["file"] = str(p)
        paths.append(p)
    sheet = out / "contact-sheet.jpg"
    contact_sheet(paths, [f"#{i} {c['time']:.2f}s score {c['score']:.2f}" for i, c in enumerate(chosen, 1)], sheet)
    print(json.dumps({"video": str(video), "size": f"{w}x{h}", "duration_s": round(dur, 2),
                      "frames_checked": int(len(frames)), "best": chosen, "contact_sheet": str(sheet)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
