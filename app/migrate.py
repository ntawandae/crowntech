"""
Tiny, safe schema upgrade: adds new nullable/defaulted columns to existing tables on startup.
Only ever ADDs columns that are missing; never drops, renames or rewrites data.
"""
import logging

from sqlalchemy import inspect, text

log = logging.getLogger(__name__)

# table -> [(column, SQL type + default)]
NEW_COLUMNS = {
    "user": [
        ("email_verified", "BOOLEAN DEFAULT {TRUE}"),          # everyone who already exists counts as verified
        ("must_change_password", "BOOLEAN DEFAULT {FALSE}"),
        ("google_sub", "VARCHAR(64)"),
    ],
    "receipt": [
        ("discount", "FLOAT DEFAULT 0"),
        ("delivery_fee", "FLOAT DEFAULT 0"),
        ("delivery_address", "VARCHAR(300)"),
    ],
}


def upgrade_schema(db):
    inspector = inspect(db.engine)
    sqlite = db.engine.dialect.name == "sqlite"
    true, false = ("1", "0") if sqlite else ("TRUE", "FALSE")
    for table, columns in NEW_COLUMNS.items():
        if table not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in columns:
            if name not in existing:
                ddl = ddl.replace("{TRUE}", true).replace("{FALSE}", false)
                db.session.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {name} {ddl}'))
                log.info("Added column %s.%s", table, name)
    db.session.commit()


def move_legacy_contracts(app, db):
    """Old uploads lived under static/uploads/contracts (publicly downloadable by URL).
    Move any that still exist into instance/contracts and point the profile at the private copy."""
    import os
    import shutil

    from .models import UserProfile

    target = os.path.join(app.instance_path, "contracts")
    homes = [os.path.join(app.static_folder, "uploads", "contracts"),
             os.path.join(os.path.dirname(app.root_path), "static", "uploads", "contracts"),
             os.path.join(os.getcwd(), "static", "uploads", "contracts")]
    moved = 0
    for prof in UserProfile.query.filter(UserProfile.contract_file.isnot(None)).all():
        name = os.path.basename(prof.contract_file)
        if os.path.isfile(os.path.join(target, name)):
            prof.contract_file = name
            continue
        for home in homes:
            src = os.path.join(home, name)
            if os.path.isfile(src):
                os.makedirs(target, exist_ok=True)
                shutil.move(src, os.path.join(target, name))
                prof.contract_file = name
                moved += 1
                break
    db.session.commit()
    if moved:
        log.info("Moved %s contract file(s) to private storage", moved)
