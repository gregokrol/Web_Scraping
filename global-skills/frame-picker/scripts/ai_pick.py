"""Optional: ask Claude (the Anthropic API) to choose the best frame and suggest a crop - for use WITHOUT a
Claude chat open (a batch of videos, a scheduled job). In a Claude Code / claude.ai conversation this is not
needed: Claude looks at contact-sheet.jpg itself.

    python ai_pick.py FRAMES_DIR [--goal "Instagram post of the product"] [--aspect 4:5] [--apply OUT.jpg]

FRAMES_DIR is the folder frames.py wrote (best-*.png + contact-sheet.jpg). Sends the contact sheet and the
candidate frames (scaled down) to Claude, gets back JSON: which frame, why, a crop (ratio + focus point) and
problems it sees. With --apply it runs edit_image.py with that crop.

Needs: pip install anthropic, and a key - ANTHROPIC_API_KEY in the environment or `ant auth login`. The key
is never written to a file. Every call is billed to the key's account (a few cents for one video).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image

MODEL = "claude-opus-5-5"
MAX_SIDE = 1280                       # enough for Claude to judge sharpness, eyes, text; keeps the request small

SCHEMA = {
    "type": "object",
    "properties": {
        "best": {"type": "integer", "description": "number of the chosen frame (1 = best-01)"},
        "ranking": {"type": "array", "items": {"type": "integer"}, "description": "all frame numbers, best first"},
        "reason": {"type": "string", "description": "one or two sentences: why this frame"},
        "problems": {"type": "array", "items": {"type": "string"},
                     "description": "what is wrong in the chosen frame, if anything (blur, closed eyes, cut text)"},
        "aspect": {"type": "string", "description": "crop ratio, e.g. 4:5, 1:1, 9:16"},
        "focus_x": {"type": "number", "description": "crop centre from the left, 0..1"},
        "focus_y": {"type": "number", "description": "crop centre from the top, 0..1"},
    },
    "required": ["best", "ranking", "reason", "problems", "aspect", "focus_x", "focus_y"],
    "additionalProperties": False,
}


def _jpeg_b64(path: Path, max_side: int = MAX_SIDE) -> str:
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def frames_in(folder: Path) -> list[Path]:
    frames = sorted(folder.glob("best-*-at-*s.png"))
    if not frames:
        sys.exit(f"no best-*.png in {folder} - run frames.py first")
    return frames


def build_content(folder: Path, goal: str, aspect: str) -> list[dict]:
    """The user message: the contact sheet, then each candidate with its number, then the task."""
    content: list[dict] = []
    sheet = folder / "contact-sheet.jpg"
    if sheet.is_file():
        content += [{"type": "text", "text": "Overview of all candidates:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": _jpeg_b64(sheet, 1600)}}]
    for n, p in enumerate(frames_in(folder), 1):
        t = re.search(r"-at-([\d.]+)s", p.name)
        content += [{"type": "text", "text": f"Frame {n} (at {t.group(1) if t else '?'} s):"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": _jpeg_b64(p)}}]
    content.append({"type": "text", "text": (
        "These frames come from one video. Choose the single best one to use as a still image"
        f"{' for: ' + goal if goal else ''}. Judge like a photo editor: the subject is sharp and complete, "
        "eyes open, no motion blur, no text or graphic caught half-way through an animation, nothing important "
        "covered or cut at the edge, good light. Then suggest a crop"
        f"{' with ratio ' + aspect if aspect else ' (pick the ratio that suits the image)'}: give the centre "
        "of the crop (focus_x, focus_y from 0 to 1) so the subject stays in it. Say what is still wrong with the "
        "chosen frame, if anything. Describe only what you see - do not guess what happens outside the frames.")})
    return content


def ask_claude(content: list[dict]) -> dict:
    try:
        import anthropic
    except ImportError:
        sys.exit("the anthropic package is not installed: pip install anthropic")
    client = anthropic.Anthropic()        # key from ANTHROPIC_API_KEY or `ant auth login`, never from a file
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",          # if the model declines, the API retries on a fallback model
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError:
        sys.exit("no valid Anthropic key: set ANTHROPIC_API_KEY or run `ant auth login`")
    except anthropic.RateLimitError:
        sys.exit("the Anthropic API is busy (rate limit) - try again in a minute")
    except anthropic.APIConnectionError:
        sys.exit("cannot reach the Anthropic API - check the internet connection")
    except anthropic.APIStatusError as exc:
        sys.exit(f"Anthropic API error {exc.status_code}: {exc.message}")
    if response.stop_reason == "refusal":
        sys.exit("Claude declined to judge these images")
    if response.stop_reason == "max_tokens":
        sys.exit("the answer was cut off (max_tokens) - try again with fewer frames (--top 3 in frames.py)")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        answer = json.loads(text)
    except json.JSONDecodeError:
        sys.exit("Claude's answer was not the expected JSON - try again")
    answer["model"] = getattr(response, "model", MODEL)   # a fallback model may have answered
    return answer


def check(answer: dict, n_frames: int) -> dict:
    """The model's JSON, made safe to use: a frame that exists, a focus inside the image, a valid ratio."""
    def num(key, default, kind=float):
        try:
            return kind(answer.get(key, default))
        except (TypeError, ValueError):
            return default
    best = num("best", 1, int)
    if not 1 <= best <= n_frames:
        best = 1
    fx = min(max(num("focus_x", 0.5), 0.0), 1.0)
    fy = min(max(num("focus_y", 0.5), 0.0), 1.0)
    aspect = str(answer.get("aspect", "4:5"))
    if not re.fullmatch(r"\d{1,2}:\d{1,2}", aspect):
        aspect = "4:5"
    return {**answer, "best": best, "focus_x": round(fx, 3), "focus_y": round(fy, 3), "aspect": aspect}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("frames_dir")
    ap.add_argument("--goal", default="", help="what the picture is for, e.g. 'Instagram post of the product'")
    ap.add_argument("--aspect", default="", help="crop ratio you want (default: Claude chooses)")
    ap.add_argument("--apply", metavar="OUT", help="also make the edited image (edit_image.py) at this path")
    a = ap.parse_args(argv)
    folder = Path(a.frames_dir)
    frames = frames_in(folder)
    answer = check(ask_claude(build_content(folder, a.goal, a.aspect)), len(frames))
    if a.aspect:
        answer["aspect"] = a.aspect
    answer["file"] = str(frames[answer["best"] - 1])
    if a.apply:
        cmd = [sys.executable, str(Path(__file__).with_name("edit_image.py")), answer["file"], "-o", a.apply,
               "--aspect", answer["aspect"], "--focus", f"{answer['focus_x']},{answer['focus_y']}", "--auto"]
        subprocess.run(cmd, check=True)
        answer["edited"] = a.apply
    print(json.dumps(answer, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
