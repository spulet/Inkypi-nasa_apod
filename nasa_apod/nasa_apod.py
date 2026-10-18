# nasa_apod/nasa_apod.py
import io
import random
import re
from datetime import datetime
import requests
from urllib.parse import urlparse, parse_qs
from PIL import Image, ImageDraw, ImageFont
from plugins.base_plugin.base_plugin import BasePlugin

DEFAULT_SIZE = (800, 480)
MIN_IMAGE_SIDE = 640  # smaller than this is almost certainly a video thumbnail
VIDEO_HINTS = re.compile(
    r"(featured video|this video|the video|video shows|time-?lapse|animation shows|watch the)",
    re.IGNORECASE,
)
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
        return title.replace("&#8217;", "'").replace("&#8220;", '"').replace("&#8221;", '"')

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

    def _is_static_image(self, post):
        """True only for genuine static photos. NASA labels some video days as
        media_type "image" and supplies a small thumbnail, so check more than that."""
        img_url = self._get_image_url(post)
        if not img_url:
            return False

        # 1. Explicit media type (when present, anything other than "image" is out)
        media_type = post.get("media_type")
        if media_type and media_type != "image":
            return False
        if not media_type and not img_url.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg")):
            return False

        # 2. Video days are mislabeled as "image": look for video wording in the visible text
        text = re.sub(r"<[^>]+>", " ", post.get("explanation", "") or "")
        if VIDEO_HINTS.search(text):
            return False

        # 3. Thumbnail-sized asset (dimensions are in the URL query, e.g. ?w=512&h=512)
        try:
            q = parse_qs(urlparse(img_url).query)
            w = int(q.get("w", ["0"])[0])
            h = int(q.get("h", ["0"])[0])
            if w > 0 and h > 0 and max(w, h) < MIN_IMAGE_SIDE:
                return False
        except (ValueError, TypeError):
            pass

        return True

    def _download_image(self, post):
        img_response = requests.get(self._get_image_url(post), timeout=20)
        img_response.raise_for_status()
        return Image.open(io.BytesIO(img_response.content)).convert("RGB")

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

            # Today's post first; if it isn't a static photo, try random previous days
            candidates = [posts[0]]
            previous = [p for p in posts[1:] if self._is_static_image(p)]
            random.shuffle(previous)
            if not self._is_static_image(posts[0]):
                candidates = previous
            else:
                candidates = [posts[0]] + previous  # previous days only used if download fails

            chosen_post, raw_img = None, None
            for post in candidates:
                try:
                    img = self._download_image(post)
                    if max(img.size) < MIN_IMAGE_SIDE:
                        continue  # tiny image = video thumbnail, try another day
                    raw_img = img
                    chosen_post = post
                    break
                except Exception:
                    continue  # bad link/corrupt file: try another day

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
