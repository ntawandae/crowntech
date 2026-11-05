"""
Helper for Supabase Storage. Run from the project folder with your Supabase settings in the environment
(the same SUPABASE_URL / SUPABASE_SERVICE_KEY you set on Render):

    python tools/supabase_storage.py check      # creates the buckets if needed and tests upload / link / delete
    python tools/supabase_storage.py migrate    # copies photos + contracts still on this machine's disk to Supabase

`migrate` only has something to copy if the files are on the machine you run it from (for example your own
computer with a copy of the project's static/uploads and instance/contracts folders). It is safe to run twice.
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402
from PIL import Image  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import Product, ProductImage, UserProfile  # noqa: E402
from app.services import images, photos, storage  # noqa: E402


def check():
    if not storage.enabled():
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY are not set - nothing to check.")
        return 1
    storage.ensure_buckets(force=True)
    ok = True
    # product bucket: upload a tiny picture, open it through its public address, delete it
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (242, 176, 30)).save(buf, "JPEG")
    key = "card/_storage_check.jpg"
    try:
        storage.upload(storage.bucket_products(), key, buf.getvalue(), "image/jpeg", cache_seconds=0)
        resp = requests.get(storage.public_url(storage.bucket_products(), key), timeout=15)
        public_ok = resp.status_code == 200
        print(f"[{'ok' if public_ok else 'FAIL'}] {storage.bucket_products()}: upload worked, public link "
              f"{'opens' if public_ok else 'does NOT open (is the bucket set to Public?)'}")
        ok &= public_ok
        storage.delete(storage.bucket_products(), [key])
    except storage.StorageError as exc:
        print(f"[FAIL] {storage.bucket_products()}: {exc}")
        ok = False
    # documents bucket: must NOT be readable without a signed link
    key = "contracts/_storage_check.txt"
    try:
        storage.upload(storage.bucket_docs(), key, b"private test", "text/plain", cache_seconds=0)
        leaked = requests.get(storage.public_url(storage.bucket_docs(), key), timeout=15).status_code == 200
        signed = requests.get(storage.signed_url(storage.bucket_docs(), key, 30), timeout=15).status_code == 200
        print(f"[{'FAIL' if leaked else 'ok'}] {storage.bucket_docs()}: public link "
              f"{'WORKS - the bucket is public, make it Private!' if leaked else 'is refused (private, good)'}")
        print(f"[{'ok' if signed else 'FAIL'}] {storage.bucket_docs()}: signed link {'opens the file' if signed else 'did not work'}")
        ok &= (not leaked) and signed
        storage.delete(storage.bucket_docs(), [key])
    except storage.StorageError as exc:
        print(f"[FAIL] {storage.bucket_docs()}: {exc}")
        ok = False
    print("\nAll good." if ok else "\nSomething needs attention (see above).")
    return 0 if ok else 1


def migrate():
    if not storage.enabled():
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY are not set.")
        return 1
    storage.ensure_buckets(force=True)
    upload_dir = flask_app.config["UPLOAD_FOLDER"]
    names = {p.image_filename for p in Product.query.all() if p.image_filename}
    names |= {i.filename for i in ProductImage.query.all() if i.filename}
    done = missing = 0
    for name in sorted(names):
        path = os.path.join(upload_dir, os.path.basename(name))
        if not os.path.isfile(path):
            missing += 1
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        for size in images.SIZES:
            storage.upload(storage.bucket_products(), photos.variant_key(size, name),
                           images.frame_bytes(io.BytesIO(data), size), "image/jpeg")
        done += 1
        print("photo copied:", name)
    print(f"Photos: {done} copied, {missing} not found on this machine (upload those again in the admin).")

    import mimetypes
    from app.views.auth import contract_key, contracts_dir
    c_done = c_missing = 0
    for prof in UserProfile.query.filter(UserProfile.contract_file.isnot(None)).all():
        path = os.path.join(contracts_dir(), os.path.basename(prof.contract_file))
        if not os.path.isfile(path):
            c_missing += 1
            continue
        with open(path, "rb") as fh:
            storage.upload(storage.bucket_docs(), contract_key(prof.contract_file), fh.read(),
                           mimetypes.guess_type(path)[0] or "application/octet-stream", cache_seconds=0)
        c_done += 1
        print("contract copied:", prof.contract_file)
    print(f"Contracts: {c_done} copied, {c_missing} not found on this machine.")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in ("check", "migrate"):
        print(__doc__)
        sys.exit(2)
    flask_app = create_app()
    with flask_app.app_context():
        sys.exit(check() if command == "check" else migrate())
