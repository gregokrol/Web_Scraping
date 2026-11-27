"""Edit an image (e.g. a frame from frames.py) and save it as a NEW file - the source is never changed.

    python edit_image.py IN.png -o OUT.jpg [options]

Options, applied in this order:
  --crop X,Y,W,H          cut out this rectangle (pixels)
  --aspect 4:5            center-crop to a ratio (1:1, 4:5, 9:16, 16:9, 3:2 ...); --focus 0.5,0.4 moves the
                          center (0..1 from the left / top), e.g. to keep a face that is above the middle
  --rotate 90             degrees, counter-clockwise;   --flip   mirror left-right
  --resize 1080           width in pixels (height follows), or 1080x1350 (crop first to keep the ratio)
  --auto                  automatic fix: stretch the contrast a little, then a light sharpen
  --brightness 1.1        1 = unchanged, 0.9 darker, 1.2 brighter
  --contrast 1.1          same scale;   --saturation 1.1   same scale (0 = black and white)
  --sharpen 1.3           1 = unchanged
  --watermark logo.png    a logo in a corner: --wm-pos br|bl|tr|tl (default br), --wm-size 0.15 (of width)
  --quality 92            JPEG / WebP quality;  the format comes from the -o file name (.jpg .png .webp)
Prints what was done and the new size.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps


def _ratio(text: str) -> float:
    a, _, b = text.partition(":")
    r = float(a) / float(b)
    if r <= 0:
        raise ValueError
    return r


def aspect_crop(im: Image.Image, ratio: float, focus=(0.5, 0.5)) -> Image.Image:
    w, h = im.size
    if w / h > ratio:                       # too wide: cut the sides
        nw, nh = int(round(h * ratio)), h
    else:                                   # too tall: cut top / bottom
        nw, nh = w, int(round(w / ratio))
    x = int(round(min(max(focus[0] * w - nw / 2, 0), w - nw)))
    y = int(round(min(max(focus[1] * h - nh / 2, 0), h - nh)))
    return im.crop((x, y, x + nw, y + nh))


def watermark(im: Image.Image, logo_path: str, pos: str = "br", size: float = 0.15) -> Image.Image:
    logo = Image.open(logo_path).convert("RGBA")
    lw = max(16, int(im.width * size))
    logo = logo.resize((lw, int(logo.height * lw / logo.width)), Image.LANCZOS)
    m = int(im.width * 0.03)
    x = m if "l" in pos else im.width - logo.width - m
    y = m if "t" in pos else im.height - logo.height - m
    base = im.convert("RGBA")
    base.alpha_composite(logo, (x, y))
    return base


def edit(im: Image.Image, a) -> tuple[Image.Image, list[str]]:
    done = []
    im = ImageOps.exif_transpose(im)        # phone photos: upright as they were taken
    if a.crop:
        x, y, w, h = (int(v) for v in a.crop.split(","))
        im = im.crop((x, y, x + w, y + h))
        done.append(f"crop {w}x{h} at {x},{y}")
    if a.aspect:
        fx, fy = (float(v) for v in a.focus.split(","))
        im = aspect_crop(im, _ratio(a.aspect), (fx, fy))
        done.append(f"aspect {a.aspect}")
    if a.rotate:
        im = im.rotate(a.rotate, expand=True, resample=Image.BICUBIC)
        done.append(f"rotate {a.rotate:g}")
    if a.flip:
        im = ImageOps.mirror(im)
        done.append("flip")
    if a.resize:
        if "x" in a.resize:
            w, h = (int(v) for v in a.resize.lower().split("x"))
        else:
            w = int(a.resize)
            h = int(round(im.height * w / im.width))
        im = im.resize((w, h), Image.LANCZOS)
        done.append(f"resize {w}x{h}")
    if a.auto:
        rgb = im.convert("RGB")
        im = ImageOps.autocontrast(rgb, cutoff=0.5).filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
        done.append("auto contrast + light sharpen")
    for name, cls in (("brightness", ImageEnhance.Brightness), ("contrast", ImageEnhance.Contrast),
                      ("saturation", ImageEnhance.Color), ("sharpen", ImageEnhance.Sharpness)):
        v = getattr(a, name)
        if v is not None and v != 1:
            im = cls(im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im).enhance(v)
            done.append(f"{name} {v:g}")
    if a.watermark:
        im = watermark(im, a.watermark, a.wm_pos, a.wm_size)
        done.append(f"logo {a.wm_pos}")
    return im, done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("image")
    ap.add_argument("-o", "--out", required=True, help="the new file (.jpg .png .webp)")
    ap.add_argument("--crop"), ap.add_argument("--aspect"), ap.add_argument("--focus", default="0.5,0.5")
    ap.add_argument("--rotate", type=float, default=0), ap.add_argument("--flip", action="store_true")
    ap.add_argument("--resize"), ap.add_argument("--auto", action="store_true")
    for n in ("brightness", "contrast", "saturation", "sharpen"):
        ap.add_argument(f"--{n}", type=float, default=None)
    ap.add_argument("--watermark"), ap.add_argument("--wm-pos", default="br", choices=["br", "bl", "tr", "tl"])
    ap.add_argument("--wm-size", type=float, default=0.15), ap.add_argument("--quality", type=int, default=92)
    a = ap.parse_args(argv)
    src, out = Path(a.image), Path(a.out)
    if not src.is_file():
        sys.exit(f"no such file: {src}")
    if out.resolve() == src.resolve():
        sys.exit("the output must be a new file - the source is never overwritten")
    try:
        im, done = edit(Image.open(src), a)
    except ValueError as exc:
        sys.exit(f"bad option value: {exc}")
    ext = out.suffix.lower()
    out.parent.mkdir(parents=True, exist_ok=True)
    if ext in (".jpg", ".jpeg"):
        im.convert("RGB").save(out, quality=a.quality, optimize=True)
    elif ext == ".webp":
        im.save(out, quality=a.quality)
    else:
        im.save(out)
    print(f"{out}  {im.width}x{im.height}  ({', '.join(done) or 'no change'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
