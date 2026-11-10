"""Logo watermark on channel photos."""

import io
import json

import pytest
from PIL import Image

from affiliate_scraper.branding import DEFAULT_LOGO, MIN_SIDE, BrandingError, download_image, load_logo, watermark
from affiliate_scraper.telegram import TelegramPublisher

RED = (220, 20, 20)


def red_jpeg(w=800, h=600, fmt="JPEG"):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), RED).save(buf, fmt)
    return buf.getvalue()


def close(a, b, tol=40):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_logo_is_in_the_project_and_cut_to_a_circle():
    assert DEFAULT_LOGO.is_file()
    logo = load_logo()
    w, h = logo.size
    assert logo.getpixel((0, 0))[3] == 0                # square corner is transparent
    assert logo.getpixel((w // 2, h // 2))[3] == 255    # centre is opaque


def test_watermark_bottom_left_and_keeps_rest_of_image():
    out = Image.open(io.BytesIO(watermark(red_jpeg())))
    assert out.format == "JPEG" and out.size == (800, 600)
    side, m = round(600 * 0.2), round(600 * 0.03)
    cx, cy = m + side // 2, 600 - m - side // 2
    assert not close(out.getpixel((cx, cy)), RED)               # logo centre painted
    assert close(out.getpixel((m + 2, 600 - m - side + 2)), RED)  # logo square corner stays photo (circle mask)
    assert close(out.getpixel((790, 10)), RED)                  # far corner untouched


def test_small_and_transparent_images_handled():
    out = Image.open(io.BytesIO(watermark(red_jpeg(200, 150))))
    assert max(out.size) == MIN_SIDE
    buf = io.BytesIO()
    Image.new("RGBA", (600, 600), (0, 0, 0, 0)).save(buf, "PNG")
    out = Image.open(io.BytesIO(watermark(buf.getvalue())))
    assert out.mode == "RGB" and close(out.getpixel((590, 10)), (255, 255, 255))  # transparency -> white


def test_bad_input_rejected():
    with pytest.raises(BrandingError):
        watermark(b"not an image")
    with pytest.raises(BrandingError):
        download_image("http://127.0.0.1/x.jpg")          # SSRF guard
    with pytest.raises(BrandingError):
        download_image("file:///etc/passwd")


def test_download_requires_image_content(local_site):
    local_site.add("/page", "<html>hi</html>")
    with pytest.raises(BrandingError):
        download_image(local_site.base + "/page", allow_private=True)
    local_site.add("/big.jpg", "x" * 10, content_type="image/jpeg")
    assert download_image(local_site.base + "/big.jpg", allow_private=True) == b"x" * 10


class Bot:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None, data=None, files=None):
        self.calls.append({"method": url.rsplit("/", 1)[-1], "json": json, "data": data, "files": files})

        class R:
            status_code = 200

            def json(self):
                return {"ok": True, "result": {}}
        return R()


def _payload(image):
    return {"content": {"title": "Kettle", "claims": [{"text": "מחיר: ₪90", "basis": "verified_price"}],
                        "link": "https://a.example/k", "disclosure": "גילוי נאות", "cta": "לפרטים"},
            "affiliate_url": "https://a.example/k?ref=me", "image": image}


def test_non_https_image_is_not_posted_as_photo():
    bot = Bot()
    TelegramPublisher(token="1:T", channel="@c", session=bot, min_interval=0, env={}).post_deal(
        _payload("http://cdn.example/k.jpg"))
    assert [c["method"] for c in bot.calls] == ["sendMessage"]


def test_telegram_branded_photo_upload_https(monkeypatch):
    import affiliate_scraper.branding as br
    monkeypatch.setattr(br, "download_image", lambda url, **kw: red_jpeg())
    bot = Bot()
    tg = TelegramPublisher(token="1:T", channel="@c", session=bot, min_interval=0, env={})
    assert tg.watermark_logo is not None
    tg.post_deal(_payload("https://cdn.example/k.jpg"))
    call = bot.calls[0]
    assert call["method"] == "sendPhoto" and call["json"] is None
    name, data, ctype = call["files"]["photo"]
    assert ctype == "image/jpeg" and data[:2] == b"\xff\xd8"
    assert json.loads(call["data"]["reply_markup"])["inline_keyboard"][0][0]["url"] == "https://a.example/k?ref=me"
    assert "photo" not in call["data"] and call["data"]["parse_mode"] == "HTML"


def test_watermark_failure_falls_back_to_image_url(monkeypatch):
    import affiliate_scraper.branding as br

    def boom(url, **kw):
        raise BrandingError("HTTP 404")
    monkeypatch.setattr(br, "download_image", boom)
    bot = Bot()
    TelegramPublisher(token="1:T", channel="@c", session=bot, min_interval=0, env={}).post_deal(
        _payload("https://cdn.example/missing.jpg"))
    assert bot.calls[0]["method"] == "sendPhoto" and bot.calls[0]["json"]["photo"] == "https://cdn.example/missing.jpg"


def test_watermark_can_be_disabled():
    assert TelegramPublisher(token="1:T", channel="@c", env={"TELEGRAM_WATERMARK": "0"}).watermark_logo is None
