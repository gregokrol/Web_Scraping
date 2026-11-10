"""Brand the images posted to the channel with the channel logo (watermark).

Every product photo shared from the channel keeps advertising "מחיר אמת".
Needs Pillow (in requirements.txt). The logo lives in ``assets/logo.png``; a
round logo on a square canvas is cut to a circle so its corners don't show.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

import requests

from .security import UnsafeURLError, check_url

log = logging.getLogger(__name__)

DEFAULT_LOGO = Path(__file__).resolve().parents[1] / "assets" / "logo.png"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_SIDE = 1280          # Telegram recompresses large photos anyway
MIN_SIDE = 480           # tiny store thumbnails are upscaled so the logo stays legible


class BrandingError(RuntimeError):
    pass


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageOps  # noqa: F401
        return Image, ImageDraw, ImageOps
    except ImportError as exc:  # pragma: no cover - Pillow is a requirement
        raise BrandingError("Pillow is not installed: py -m pip install pillow") from exc


def load_logo(path: Path | str = DEFAULT_LOGO, circle: bool = True):
    Image, ImageDraw, _ = _pil()
    logo = Image.open(path).convert("RGBA")
    if circle:
        w, h = logo.size
        side = min(w, h)
        logo = logo.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        mask = Image.new("L", logo.size, 0)
        ImageDraw.Draw(mask).ellipse((1, 1, side - 2, side - 2), fill=255)
        alpha = Image.composite(logo.getchannel("A"), mask, mask)
        logo.putalpha(alpha)
    return logo


def watermark(image_bytes: bytes, logo_path: Path | str = DEFAULT_LOGO, size_ratio: float = 0.2,
              margin_ratio: float = 0.03, position: str = "bottom-left", opacity: float = 0.95) -> bytes:
    """Return a JPEG of ``image_bytes`` with the logo in a corner."""
    Image, _, ImageOps = _pil()
    try:
        base = Image.open(io.BytesIO(image_bytes))
        base = ImageOps.exif_transpose(base)
    except Exception as exc:  # noqa: BLE001 - any decode problem means "not an image we can use"
        raise BrandingError(f"not a readable image: {exc}") from None
    if base.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", base.size, "white")
        rgba = base.convert("RGBA")
        bg.paste(rgba, mask=rgba.getchannel("A"))
        base = bg
    else:
        base = base.convert("RGB")

    longest = max(base.size)
    scale = MAX_SIDE / longest if longest > MAX_SIDE else (MIN_SIDE / longest if longest < MIN_SIDE else 1.0)
    if scale != 1.0:
        base = base.resize((max(1, round(base.width * scale)), max(1, round(base.height * scale))), Image.LANCZOS)

    logo = load_logo(logo_path)
    side = max(48, round(min(base.size) * size_ratio))
    logo = logo.resize((side, side), Image.LANCZOS)
    if opacity < 1:
        logo.putalpha(logo.getchannel("A").point(lambda a: round(a * opacity)))
    m = round(min(base.size) * margin_ratio)
    x = m if "left" in position else base.width - side - m
    y = base.height - side - m if "bottom" in position else m
    base.paste(logo, (x, y), logo)

    out = io.BytesIO()
    base.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def download_image(url: str, session: Optional[requests.Session] = None, allow_private: bool = False,
                   timeout: float = 15.0) -> bytes:
    """Download a product image safely (SSRF check, size limit, must be an image)."""
    try:
        check_url(url, allow_private)
    except UnsafeURLError as exc:
        raise BrandingError(f"unsafe image URL: {exc}") from None
    sess = session or requests.Session()
    try:
        resp = sess.get(url, timeout=timeout, stream=True, allow_redirects=False,
                        headers={"User-Agent": "AffiliateProductBot/3.0", "Accept": "image/*"})
    except requests.RequestException as exc:
        raise BrandingError(f"image download failed: {exc}") from None
    if resp.status_code != 200:
        raise BrandingError(f"image download failed: HTTP {resp.status_code}")
    ctype = resp.headers.get("Content-Type", "")
    if ctype and not ctype.lower().startswith("image/"):
        raise BrandingError(f"not an image ({ctype})")
    chunks, size = [], 0
    for chunk in resp.iter_content(64 * 1024):
        size += len(chunk)
        if size > MAX_IMAGE_BYTES:
            raise BrandingError("image too large")
        chunks.append(chunk)
    return b"".join(chunks)
