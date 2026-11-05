"""
Product photos, wherever they live.

* Supabase Storage configured -> each upload is turned into the two shop frames (card + large, see images.py)
  once, at upload time, and stored in the public product bucket. The shop then links straight to Supabase's CDN.
* Not configured -> photos stay on the local disk and are framed on demand, exactly as before.
* Old photos that are still on the local disk keep working either way.
"""
import io
import logging
import os

from flask import current_app, url_for

from . import images, storage

log = logging.getLogger(__name__)


def _stem(filename):
    return os.path.splitext(os.path.basename(filename))[0]


def variant_key(size, filename):
    return f"{size}/{_stem(filename)}.jpg"


def _local_path(filename):
    return os.path.join(current_app.config["UPLOAD_FOLDER"], os.path.basename(filename))


def store_product_photo(stream, filename):
    """Save an already-validated upload. Raises storage.StorageError if cloud storage is on and fails."""
    if storage.enabled():
        storage.ensure_buckets()
        stream.seek(0)
        data = stream.read()
        for size in images.SIZES:
            storage.upload(storage.bucket_products(), variant_key(size, filename),
                           images.frame_bytes(io.BytesIO(data), size), "image/jpeg")
        return
    os.makedirs(current_app.config["UPLOAD_FOLDER"], exist_ok=True)
    stream.seek(0)
    with open(_local_path(filename), "wb") as fh:
        fh.write(stream.read())


def photo_src(filename, size="card"):
    """Address for an <img>, or None when the photo can't be found anywhere. Also used by the staff templates."""
    if not filename:
        return None
    if os.path.isfile(_local_path(filename)):
        return url_for("shop.media", size=size, filename=os.path.basename(filename))
    if storage.enabled() and size in images.SIZES:
        return storage.public_url(storage.bucket_products(), variant_key(size, filename))
    return None


def card_file(filename):
    """A local file path of the card-size frame (for PDFs), downloading it from Supabase if needed."""
    if not filename:
        return None
    cache = os.path.join(current_app.instance_path, "thumbs")
    path = images.get_frame_path(current_app.config["UPLOAD_FOLDER"], cache, "card", filename)
    if path:
        return path
    if storage.enabled():
        target = images._cache_path(cache, "card", filename)
        if os.path.isfile(target):
            return target
        data = storage.download_public(storage.bucket_products(), variant_key("card", filename))
        if data:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(data)
            return target
    return None


def remove_product_photo(filename):
    """Delete a photo everywhere it might be (local disk, cached frames, Supabase)."""
    if not filename:
        return
    name = os.path.basename(filename)
    paths = [_local_path(name)] + [images._cache_path(os.path.join(current_app.instance_path, "thumbs"), s, name)
                                   for s in images.SIZES]
    for path in paths:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            log.warning("Could not remove %s", path)
    if storage.enabled():
        try:
            storage.delete(storage.bucket_products(), [variant_key(s, name) for s in images.SIZES])
        except storage.StorageError as exc:
            log.warning("Could not remove %s from Supabase: %s", name, exc)
