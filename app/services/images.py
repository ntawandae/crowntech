"""
Uniform product images.

Shop photos come in every shape (tall phone shots to wide landscapes) and up to ~4 MB. Showing them
raw makes some fill the frame and others look tiny. This builds one consistent frame per size:
the whole product stays visible, and any spare space is filled with a soft blurred copy of the same
photo (the way marketplaces do it). Results are cached on disk, so each photo is processed once.
"""
import os

from PIL import Image, ImageFilter, ImageOps

SIZES = {"card": (640, 480), "large": (1100, 825)}  # 4:3 frames
QUALITY = 82


def _cache_path(cache_dir, size, filename):
    return os.path.join(cache_dir, size, os.path.splitext(os.path.basename(filename))[0] + ".jpg")


def build_frame(src_path, size):
    w, h = SIZES[size]
    with Image.open(src_path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")      # honour phone rotation
        # soft backdrop: same photo scaled to fill, blurred and lightened
        backdrop = ImageOps.fit(im, (w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(22))
        backdrop = Image.blend(backdrop, Image.new("RGB", (w, h), "white"), 0.35)
        # the product itself, whole, centred
        fg = ImageOps.contain(im, (w, h), Image.LANCZOS)
        backdrop.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
        return backdrop


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
