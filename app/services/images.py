"""Image normalize / quality gate / thumbnails (architecture §5a, E12)."""
from __future__ import annotations

import io
import logging

from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

from ..errors import DecodeFailed, ImageTooLarge, ImageTooSmall, UnsupportedType

log = logging.getLogger(__name__)

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:  # pragma: no cover - pillow-heif is a hard dependency
    HEIC_SUPPORTED = False

Image.MAX_IMAGE_PIXELS = 60_000_000  # pixel-bomb guard (E12)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MIN_QUERY_SIDE = 200          # query uploads (US-1)
CATALOG_MIN_SIDE = 400        # catalog quality gate (§5a)
MAX_EMBED_SIDE = 2048         # downscale before embed (E12)
THUMB_SIDE = 256              # thumbs for LLM rerank (§5b)
DARK_LUMINANCE = 45.0         # mean luminance below this -> too_dark (flagged, still indexed)


def sniff_image_type(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (
        b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"heif",
    ):
        return "heic"
    return None


def load_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise DecodeFailed("We couldn't read this image. Try a JPEG, PNG or WebP file.") from exc


def normalize_jpeg(img: Image.Image, max_side: int = MAX_EMBED_SIDE, quality: int = 85) -> bytes:
    """EXIF-stripped, auto-rotated, downscaled JPEG bytes (the canonical stored form)."""
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def make_thumb(img: Image.Image, side: int = THUMB_SIDE) -> bytes:
    thumb = img.copy()
    thumb.thumbnail((side, side), Image.LANCZOS)
    if thumb.mode != "RGB":
        thumb = thumb.convert("RGB")
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def mean_luminance(img: Image.Image) -> float:
    return float(ImageStat.Stat(img.convert("L").resize((64, 64))).mean[0])


def quality_gate(img: Image.Image) -> tuple[str, dict]:
    """Catalog-side gate (§5a). too_small -> not embedded; too_dark -> flagged, still indexed."""
    w, h = img.size
    if min(w, h) < CATALOG_MIN_SIDE:
        return "too_small", {"width": w, "height": h, "min_side": CATALOG_MIN_SIDE}
    lum = mean_luminance(img)
    if lum < DARK_LUMINANCE:
        return "too_dark", {"mean_luminance": round(lum, 2), "threshold": DARK_LUMINANCE}
    return "ok", {"mean_luminance": round(lum, 2)}


def validate_query_upload(data: bytes) -> tuple[bytes, int, int]:
    """Server-side re-validation for shopper uploads (AC1.1, AC2.2, E12).

    Returns (normalized_jpeg_bytes, width, height).
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageTooLarge("JPEG, PNG or WebP under 10 MB.", details={"max_mb": 10})
    kind = sniff_image_type(data)
    if kind is None:
        raise UnsupportedType("JPEG, PNG or WebP under 10 MB.", details={"accepted": ["jpeg", "png", "webp", "heic"]})
    if kind == "heic" and not HEIC_SUPPORTED:
        raise DecodeFailed("HEIC images need server-side conversion which is unavailable.")
    img = load_image(data)
    w, h = img.size
    if min(w, h) < MIN_QUERY_SIDE:
        raise ImageTooSmall(
            f"Photo is too small — shortest side must be at least {MIN_QUERY_SIDE}px.",
            details={"width": w, "height": h, "min_side": MIN_QUERY_SIDE},
        )
    return normalize_jpeg(img), img.width, img.height
