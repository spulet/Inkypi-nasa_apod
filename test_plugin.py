# test_plugin.py
import logging
import os
import sys

# 1. Inject the NASA WordPress schema map into nasa_apod.py (now display-size aware)
modern_api_code = r'''# nasa_apod/nasa_apod.py
import html
import io
import random
import re
from datetime import datetime
import requests
import logging
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from PIL import Image, ImageDraw, ImageFont
from plugins.base_plugin.base_plugin import BasePlugin

DEFAULT_SIZE = (800, 480)
MIN_IMAGE_SIDE = 640  # smaller than this is almost certainly a video thumbnail
VIDEO_HINTS = re.compile(
    r"\b(featured video|this video|the video|video shows|time-?lapse|animation shows|watch the)\b",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)

FONT_CANDIDATES = [
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "Arial.ttf",
]


class NasaApod(BasePlugin):
    def __init__(self, config):
        super().__init__(config)

    # ---------- display sizing ----------
    def _get_display_size(self, device_config, inky_display=None):
        """Resolve target size: physical display > device config > default."""
        width = height = None

        # 1. Physical Inky display (inky exposes .resolution and/or .width/.height)
        if inky_display is not None:
            res = getattr(inky_display, "resolution", None)
            if res and len(res) == 2:
                width, height = res
            else:
                width = getattr(inky_display, "width", None)
                height = getattr(inky_display, "height", None)

        # 2. Device config (object with get_resolution(), or plain dict)
        if not (width and height) and device_config is not None:
            if hasattr(device_config, "get_resolution"):
                width, height = device_config.get_resolution()
            elif hasattr(device_config, "get"):
                width = device_config.get("width")
                height = device_config.get("height")

        # 3. Fallback
        if not (width and height):
            width, height = DEFAULT_SIZE

        width, height = int(width), int(height)

        # Honor vertical orientation if configured
        orientation = None
        if hasattr(device_config, "get_config"):
            orientation = device_config.get_config("orientation")
        elif hasattr(device_config, "get"):
            orientation = device_config.get("orientation")
        if orientation == "vertical" and width > height:
            width, height = height, width

        return width, height

    # ---------- post helpers ----------
    def _get_title(self, post):
        raw_title = post.get("title", "NASA APOD")
        if isinstance(raw_title, dict):
            title = raw_title.get("rendered", "NASA APOD")
        else:
            title = str(raw_title)
        # Decode every HTML entity (&amp; &#8211; &#039; ...), drop stray tags, tidy spaces
        title = html.unescape(title)
        title = re.sub(r"<[^>]+>", "", title)
        return " ".join(title.split()) or "NASA APOD"

    def _get_date_text(self, post):
        """Format the post date (YYYY-MM-DD) as e.g. 'Oct 15, 2026'."""
        raw = post.get("date")
        if not raw:
            return ""
        try:
            return datetime.strptime(str(raw)[:10], "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
        except ValueError:
            return str(raw)

    def _get_image_url(self, post):
        return post.get("hdurl") or post.get("url")

    def _static_image_reason(self, post):
        """Return None if the post is a usable static photo, else a short reason why not."""
        img_url = self._get_image_url(post)
        if not img_url:
            return "no image URL"

        media_type = post.get("media_type")
        if media_type and media_type != "image":
            return f"media_type is {media_type!r}"
        if not media_type and not img_url.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg")):
            return "URL is not an image file"

        # Video days are sometimes mislabeled "image": look for video wording in the visible text
        text = re.sub(r"<[^>]+>", " ", post.get("explanation", "") or "")
        match = VIDEO_HINTS.search(text)
        if match:
            return f"explanation mentions video ({match.group(0)!r})"

        # Thumbnail-sized asset (dimensions are in the URL query, e.g. ?w=512&h=512)
        try:
            q = parse_qs(urlparse(img_url).query)
            w = int(q.get("w", ["0"])[0])
            h = int(q.get("h", ["0"])[0])
            if w > 0 and h > 0 and max(w, h) < MIN_IMAGE_SIDE:
                return f"thumbnail-sized image ({w}x{h})"
        except (ValueError, TypeError):
            pass

        return None

    def _is_static_image(self, post):
        return self._static_image_reason(post) is None

    def _sized_url(self, url, box):
        """NASA's image service honors w/h, so ask for a display-sized copy
        instead of the multi-megabyte original (avoids timeouts on slow devices)."""
        parts = urlparse(url)
        if "/dynamicimage/" not in parts.path:
            return None
        query = parse_qs(parts.query)
        query["w"] = [str(box)]
        query["h"] = [str(box)]
        query.setdefault("fit", ["clip"])
        return urlunparse(parts._replace(query=urlencode(query, doseq=True)))

    def _download_image(self, post, box=None):
        url = self._get_image_url(post)
        attempts = []
        if box:
            sized = self._sized_url(url, box)
            if sized:
                attempts.append(sized)
        attempts.append(url)  # original as a fallback

        last_error = None
        for attempt in attempts:
            try:
                resp = requests.get(attempt, timeout=(10, 60))
                resp.raise_for_status()
                return Image.open(io.BytesIO(resp.content)).convert("RGB")
            except Exception as e:
                last_error = e
                logger.warning("Image download failed (%s): %s", attempt, e)
        raise last_error

    # ---------- main entry ----------
    def generate_image(self, settings, device_config, inky_display=None):
        settings = settings or {}
        width, height = self._get_display_size(device_config, inky_display)

        url = "https://science.nasa.gov/wp-json/wp/v2/apod-basic"

        try:
            # Request a batch of recent posts so we have previous days to fall back on
            response = requests.get(url, params={"per_page": 30}, timeout=15)
            response.raise_for_status()
            posts = response.json()

            if not posts or not isinstance(posts, list):
                raise RuntimeError("Invalid data array returned from the NASA WordPress API.")

            # Today's post first; if it isn't usable, fall back to random previous days
            reason = self._static_image_reason(posts[0])
            if reason:
                logger.warning("Latest post %r (%s) skipped: %s",
                               self._get_title(posts[0]), self._get_date_text(posts[0]), reason)
            previous = [p for p in posts[1:] if self._is_static_image(p)]
            random.shuffle(previous)
            candidates = previous if reason else [posts[0]] + previous

            # Ask NASA's image service for roughly 2x the display size, not the full original
            box = max(1000, 2 * max(width, height))

            chosen_post, raw_img = None, None
            for post in candidates:
                label = f"{self._get_title(post)!r} ({self._get_date_text(post)})"
                try:
                    img = self._download_image(post, box)
                except Exception as e:
                    logger.warning("Skipping %s: download failed: %s", label, e)
                    continue
                if max(img.size) < MIN_IMAGE_SIDE:
                    logger.warning("Skipping %s: image is only %dx%d", label, img.size[0], img.size[1])
                    continue
                raw_img, chosen_post = img, post
                logger.info("Using %s", label)
                break

            if raw_img is None:
                return self._create_error_fallback(
                    "No static APOD photograph available right now.", width, height)

            title = self._get_title(chosen_post)

            # settings["fit_mode"]: "fill" (crop to cover, default) or "fit" (letterbox, no crop)
            fit_mode = settings.get("fit_mode", "fill")
            if fit_mode == "fit":
                processed_img = self._fit_image_letterbox(raw_img, width, height)
            else:
                processed_img = self._fit_image(raw_img, width, height)

            return self._overlay_text(processed_img, title, self._get_date_text(chosen_post))

        except Exception as e:
            raise RuntimeError(f"NASA WordPress REST API processing error: {str(e)}")

    # ---------- image fitting ----------
    def _fit_image(self, img, target_w, target_h):
        """Scale to cover the display, then center-crop."""
        img_w, img_h = img.size
        ratio = max(target_w / img_w, target_h / img_h)
        new_w = max(target_w, int(round(img_w * ratio)))
        new_h = max(target_h, int(round(img_h * ratio)))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        return img.crop((left, top, left + target_w, top + target_h))

    def _fit_image_letterbox(self, img, target_w, target_h):
        """Scale to fit entirely inside the display, padding with black."""
        img_w, img_h = img.size
        ratio = min(target_w / img_w, target_h / img_h)
        new_w = max(1, int(round(img_w * ratio)))
        new_h = max(1, int(round(img_h * ratio)))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (target_w, target_h), (0, 0, 0))
        canvas.paste(img, ((target_w - new_w) // 2, (target_h - new_h) // 2))
        return canvas

    # ---------- text ----------
    def _load_font(self, size):
        for name in FONT_CANDIDATES:
            try:
                return ImageFont.truetype(name, size)
            except (IOError, OSError):
                continue
        try:
            return ImageFont.load_default(size=size)  # Pillow >= 10.1
        except TypeError:
            return ImageFont.load_default()

    def _overlay_text(self, img, text, date_text=""):
        """Draw a semi-transparent banner (title + date) that scales with the display."""
        w, h = img.size
        banner_h = max(24, int(h * 0.09))
        font_size = max(10, int(banner_h * 0.5))
        pad = max(6, int(w * 0.02))
        font = self._load_font(font_size)

        base = img.convert("RGBA")
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        odraw.rectangle([(0, h - banner_h), (w, h)], fill=(0, 0, 0, 180))
        base = Image.alpha_composite(base, overlay)

        draw = ImageDraw.Draw(base)
        max_text_w = w - 2 * pad
        suffix = f" - {date_text}" if date_text else ""

        # Truncate only the title so the date always stays visible
        if draw.textlength(text + suffix, font=font) > max_text_w:
            while text and draw.textlength(text + "..." + suffix, font=font) > max_text_w:
                text = text[:-1]
            text = text.rstrip() + "..."
        full_text = text + suffix

        text_h = font.getbbox("Ag")[3] if hasattr(font, "getbbox") else font_size
        y = h - banner_h + (banner_h - text_h) // 2
        draw.text((pad, y), full_text, fill=(255, 255, 255), font=font)
        return base.convert("RGB")

    def _create_error_fallback(self, message, width, height):
        img = Image.new("RGB", (width, height), color=(20, 20, 30))
        draw = ImageDraw.Draw(img)
        font = self._load_font(max(12, int(height * 0.04)))
        draw.text((int(width * 0.03), height // 2), message, fill=(255, 255, 255), font=font)
        return img
'''

# Push the corrected configuration down onto the local drive tracking folder
target_path = os.path.join("nasa_apod", "nasa_apod.py")
with open(target_path, "w", encoding="utf-8") as f:
    f.write(modern_api_code)
print("FORCE OVERWRITE COMPLETE: nasa_apod.py mapping code corrected.")

# 2. Trigger the local validation test execution
sys.dont_write_bytecode = True
from nasa_apod.nasa_apod import NasaApod

# Try different display sizes here to verify scaling
mock_settings = {}  # e.g. {"fit_mode": "fit"}
mock_device_config = {"width": 800, "height": 480}


class MockInkyDisplay:
    """Stand-in for a real Inky display; set to None to use device config instead."""
    resolution = (800, 480)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("Executing clean local WordPress API test run...")
    plugin = NasaApod(config={})
    try:
        output_image = plugin.generate_image(mock_settings, mock_device_config, inky_display=MockInkyDisplay())
        output_image.save("test_output.png")
        print(f"Success! 'test_output.png' saved at {output_image.size[0]}x{output_image.size[1]}.")
    except Exception as e:
        print(f"Error running plugin execution: {e}")