"""Graphics for the owner, as fixed commands the Telegram manager may run (``py main.py ...`` is on its allow-list,
a new script an agent writes is not - that stays blocked on purpose):

* ``profile_pics``: two Instagram profile pictures from the logo (full logo / the cart alone) + a round preview.
* ``reel_*``: a HyperFrames reel project under ``video/``: copy the template, check, snapshot, render.
  Only folders inside ``video/`` of this project, only the pinned HyperFrames version.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

HYPERFRAMES = "hyperframes@0.9.86"          # pinned: the same project renders the same way later
TEMPLATE = "first-reel"
_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_AT_OK = re.compile(r"^\d+(\.\d+)?(,\d+(\.\d+)?){0,11}$")
RENDER_TIMEOUT = 20 * 60


class MediaError(RuntimeError):
    pass


# ------------------------------------------------------------------ profile pictures
def profile_pics(logo: Path, out_dir: Path, size: int = 1080) -> list[Path]:
    """Returns [full logo, cart only, round preview]. The logo is round (600x600 with black corners)."""
    from PIL import Image, ImageDraw
    if not Path(logo).is_file():
        raise MediaError(f"logo not found: {logo}")
    out_dir.mkdir(parents=True, exist_ok=True)
    src = Image.open(logo).convert("RGBA").resize((600, 600), Image.LANCZOS)

    def circle(n: int, r: Optional[float] = None, box: Optional[tuple] = None) -> "Image.Image":
        m = Image.new("L", box or (n, n), 0)
        c, r = n / 2, (r or n / 2)
        ImageDraw.Draw(m).ellipse((c - r, c - r, c + r, c + r), fill=255)
        return m

    def gradient(n: int) -> "Image.Image":
        g = Image.new("RGBA", (n, n))
        d = ImageDraw.Draw(g)
        for y in range(n):
            t = y / n
            d.line([(0, y), (n, y)], fill=(int(22 * (1 - t) + 6 * t), int(58 * (1 - t) + 11 * t),
                                           int(122 * (1 - t) + 24 * t), 255))
        return g

    # A: the whole logo without its black corners, with a margin for Instagram's round crop
    a = Image.new("RGBA", (size, size), (10, 20, 48, 255))
    inner = int(size * 0.926)
    logo_a = src.resize((inner, inner), Image.LANCZOS)
    logo_a.putalpha(circle(inner, inner / 2 - 3))
    a.alpha_composite(logo_a, ((size - inner) // 2, (size - inner) // 2))
    # B: the cart with the % tag only (inside the blue ring), bigger - readable at ~110 px
    ring = Image.new("RGBA", src.size, (0, 0, 0, 0))
    ring.paste(src, (0, 0), circle(600, 276))
    cart = ring.crop((70, 35, 510, 276))
    px = cart.load()
    for y in range(cart.height):
        for x in range(cart.width):
            r, g, b, al = px[x, y]
            if al and r < 40 and g < 50 and b < 90:      # the logo's dark navy -> transparent
                px[x, y] = (0, 0, 0, 0)
    w = int(size * 0.8)
    h = int(cart.height * w / cart.width)
    cart = cart.resize((w, h), Image.LANCZOS)
    b_img = gradient(size)
    b_img.alpha_composite(cart, ((size - w) // 2 + size // 54, (size - h) // 2 + size // 108))

    pa, pb = out_dir / "profile-a-full-logo.png", out_dir / "profile-b-cart.png"
    a.convert("RGB").save(pa)
    b_img.convert("RGB").save(pb)
    preview = Image.new("RGB", (1000, 560), (255, 255, 255))
    for i, im in enumerate((a, b_img)):
        for n, pos in ((400, (60 + i * 500, 30)), (110, (205 + i * 500, 440))):   # big, and the real size
            s = im.resize((n, n), Image.LANCZOS)
            s.putalpha(circle(n))
            preview.paste(s, pos, s)
    pp = out_dir / "preview-circle.png"
    preview.save(pp)
    return [pa, pb, pp]


def inside_project(project_root: Path, path) -> Path:
    """``path`` (relative to the project) resolved - refused when it points outside the project folder."""
    root = Path(project_root).resolve()
    p = (root / path).resolve()
    if p != root and root not in p.parents:
        raise MediaError(f"{path}: only files inside the project folder")
    return p


# ------------------------------------------------------------------ reels (HyperFrames)
def reel_dir(project_root: Path, name: str) -> Path:
    """``video/<name>`` inside this project - nothing else, so the command cannot touch other folders."""
    if not _NAME_OK.match(name or ""):
        raise MediaError("reel name: lowercase letters, digits and '-' only (e.g. week-48)")
    return (Path(project_root) / "video" / name).resolve()


def reel_new(project_root: Path, name: str) -> Path:
    """A new reel project: a copy of the first reel (fonts, GSAP, logo, the 3 scene files) to edit."""
    src, dst = reel_dir(project_root, TEMPLATE), reel_dir(project_root, name)
    if not (src / "index.html").is_file():
        raise MediaError(f"the template {src} is missing (git pull)")
    if dst.exists():
        raise MediaError(f"{dst} already exists - pick another name")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("renders", ".hyperframes", "node_modules",
                                                            "CAPTION.md", "*.mp4"))
    (dst / "renders").mkdir()
    return dst


def find_npx() -> Optional[str]:
    return shutil.which("npx") or shutil.which("npx.cmd")


def reel_command(action: str, project: Path, at: str = "", out: Optional[Path] = None,
                 npx: Optional[str] = None) -> list[str]:
    if action not in ("check", "snapshot", "render"):
        raise MediaError("action: check, snapshot or render")
    if not (project / "index.html").is_file():
        raise MediaError(f"no index.html in {project}")
    cmd = [npx or find_npx() or "npx", "-y", HYPERFRAMES, action]
    if action == "snapshot":
        if at and not _AT_OK.match(at):
            raise MediaError("--at: seconds separated by commas, e.g. 1,3.5,9")
        cmd += ["--at", at or "1,4,8,12,16"]
    if action == "render":
        cmd += ["-f", "30", "-o", str(out)]
    return cmd


def reel_run(action: str, project: Path, at: str = "", timeout: int = RENDER_TIMEOUT,
             runner=subprocess.run) -> tuple[int, str, Optional[Path]]:
    """Runs HyperFrames in the reel folder. Returns (exit code, the last lines of output, the mp4 for render)."""
    npx = find_npx()
    if not npx and runner is subprocess.run:
        raise MediaError("Node.js is not installed (npx not found) - install the LTS version from nodejs.org")
    out = None
    if action == "render":
        out = project / "renders" / f"{project.name}-{datetime.now().strftime('%Y-%m-%d_%H%M')}.mp4"
        out.parent.mkdir(exist_ok=True)
    cmd = reel_command(action, project, at, out, npx)
    # npx would run a local copy from node_modules or follow a .npmrc in the folder - only the pinned public package
    planted = [n for n in ("node_modules", ".npmrc") if (project / n).exists()]
    if planted:
        raise MediaError(f"remove {', '.join(planted)} from {project} first (only the pinned HyperFrames runs)")
    env = {**os.environ, "HYPERFRAMES_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1"}
    try:
        res = runner(cmd, cwd=str(project), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                     timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, f"{action} did not finish in {timeout // 60} minutes", None
    text = [ln for ln in (res.stdout or "").strip().splitlines() if not ln.startswith("[hyperframes]")]
    tail = "\n".join(text[-25:])
    if action == "render" and (res.returncode != 0 or not out.is_file()):
        return res.returncode or 1, tail, None
    return res.returncode, tail, out


def latest_snapshots(project: Path) -> list[Path]:
    """The frames of the newest ``snapshot`` run (HyperFrames writes them under .hyperframes/artifacts)."""
    base = project / ".hyperframes" / "artifacts" / "snapshots"
    runs = sorted((p for p in base.glob("*") if p.is_dir()), key=lambda p: p.stat().st_mtime) if base.exists() else []
    return sorted(runs[-1].glob("frame-*.png")) if runs else []
