"""
Uniform product images.

Shop photos come in every shape (tall phone shots to wide landscapes) and up to ~4 MB. Every photo is
cropped to fill a 4:3 frame edge to edge - no bars and no blurred background - so cards, the product page,
catalogue PDFs and stock lists all look tidy. The crop is centred, nudged slightly upward (products usually
sit a little above the middle of the shot). Results are cached on disk, so each photo is processed once.
"""
import os

from PIL import Image, ImageOps

SIZES = {"card": (640, 480), "large": (1100, 825)}  # 4:3 frames
QUALITY = 82
FRAME_VERSION = 2          # bump when the framing style changes: old cached frames are then ignored
ORIGINAL_MAX = 1600        # longest side of the kept original (used if the framing is ever changed again)
CENTERING = (0.5, 0.45)


def _cache_path(cache_dir, size, filename):
    return os.path.join(cache_dir, f"v{FRAME_VERSION}", size, os.path.splitext(os.path.basename(filename))[0] + ".jpg")


def build_frame(src_path, size):
    """The photo cropped to fill the whole frame (nothing added around it)."""
    w, h = SIZES[size]
    with Image.open(src_path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")      # honour phone rotation
        return ImageOps.fit(im, (w, h), Image.LANCZOS, centering=CENTERING)


def original_bytes(src):
    """A tidy, size-limited copy of the uploaded photo (JPEG bytes) kept so it can be re-framed later."""
    import io
    buf = io.BytesIO()
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((ORIGINAL_MAX, ORIGINAL_MAX), Image.LANCZOS)
        im.save(buf, "JPEG", quality=85, optimize=True, progressive=True)
    return buf.getvalue()


def frame_bytes(src, size):
    """The finished frame as JPEG bytes (src: a path or an open file). Used when photos go to cloud storage."""
    import io
    buf = io.BytesIO()
    build_frame(src, size).save(buf, "JPEG", quality=QUALITY, optimize=True, progressive=True)
    return buf.getvalue()


def get_frame_path(upload_dir, cache_dir, size, filename):
    """Return the path of the cached frame, building it first if needed. None if the source is missing."""
    if size not in SIZES:
        return None
    src = os.path.join(upload_dir, os.path.basename(filename))
    if not os.path.isfile(src):
        return None
    out = _cache_path(cache_dir, size, filename)
    if not os.path.isfile(out) or os.path.getmtime(out) < os.path.getmtime(src):
        os.makedirs(os.path.dirname(out), exist_ok=True)
        try:
            build_frame(src, size).save(out, "JPEG", quality=QUALITY, optimize=True, progressive=True)
        except Exception:
            return None
    return out
