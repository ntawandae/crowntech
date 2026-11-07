"""PDF helpers: receipts (new sales, reprints, online orders) and a safe logo/static loader."""
import os
from io import BytesIO

from flask import current_app, render_template, url_for


def static_link_callback(uri, rel):
    """Let xhtml2pdf read /static/... files straight from disk instead of calling the server back
    (a self-request would hang a single-worker server, and breaks behind some proxies)."""
    marker = "/static/"
    if marker in uri:
        path = os.path.join(current_app.static_folder, uri.split(marker, 1)[1].split("?")[0])
        if os.path.isfile(path):
            return path
    return uri


def render_pdf(template, **context):
    """Render a template to PDF. Returns a BytesIO, or None if the PDF engine reported errors."""
    from xhtml2pdf import pisa
    html = render_template(template, **context)
    buf = BytesIO()
    result = pisa.CreatePDF(html, dest=buf, link_callback=static_link_callback)
    if result.err:
        return None
    buf.seek(0)
    return buf


def receipt_items(receipt):
    """Receipt lines rebuilt from stored Sale rows (for reprints)."""
    items = []
    for s in receipt.sales:
        qty = s.quantity or 0
        net = float(s.total_price or 0)
        # gross = what the item was priced at before its share of the discount
        gross = float(s.product.price or 0) * qty if s.product else net
        items.append({"name": s.product.name if s.product else "Item", "price": float(s.product.price or 0) if s.product else 0,
                      "quantity": qty, "original_total_price": gross, "total_price": net})
    return items


def receipt_pdf(receipt, items, printed_by):
    from .pdf_docs import company
    cfg = current_app.config
    return render_pdf(
        "receipt_template.html",
        co=company(),
        items=items,
        subtotal=receipt.subtotal or 0,
        discount=receipt.discount or 0,
        delivery_fee=receipt.delivery_fee or 0,
        delivery_address=receipt.delivery_address,
        tax=receipt.tax or 0,
        total=receipt.total or 0,
        receipt_number=receipt.receipt_number,
        customer_name=receipt.customer_name,
        customer_email=receipt.customer_email,
        customer_phone=receipt.customer_phone,
        printed_by=printed_by,
        business_name=cfg["BUSINESS_NAME"],
        business_address=cfg["BUSINESS_ADDRESS"],
        logo_path=url_for("static", filename="images/cthlogo.jpg"),
        current_year=receipt.timestamp.year if receipt.timestamp else "",
        timestamp=receipt.timestamp,
    )


def quotation_pdf(quotation, prepared_by):
    """The quotation as a PDF (BytesIO), or None if the PDF engine failed."""
    from datetime import timedelta

    from .pdf_docs import company
    days = int(current_app.config.get("QUOTATION_VALID_DAYS") or 0)
    valid_until = (quotation.created_at + timedelta(days=days)) if (days and quotation.created_at) else None
    return render_pdf("print_quotation.html", co=company(), quotation=quotation, prepared_by=prepared_by,
                      valid_until=valid_until, valid_days=days)


def catalog_pdf(products):
    """Customer catalogue: in-stock products first, grouped by category, with photos and key specs."""
    from ..services.specs import CATEGORY_ORDER, extract_specs, normalize_category, short_title
    from .pdf_docs import company
    from .timeutil import zim_now
    from ..services import photos

    groups = {}
    for p in products:
        if (p.price or 0) <= 0:
            continue
        cat = normalize_category(p.category)
        groups.setdefault(cat, []).append({
            "title": short_title(p.name, 62), "price": p.price, "in_stock": (p.quantity or 0) > 0,
            "specs": extract_specs(p.name)[:3], "image": photos.card_file((p.image_filename or "").strip()),
        })
    ordered = sorted(groups, key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else len(CATEGORY_ORDER))
    sections = []
    for cat in ordered:
        items = sorted(groups[cat], key=lambda i: (not i["in_stock"], i["title"].lower()))
        sections.append({"name": cat, "rows": [items[i:i + 3] for i in range(0, len(items), 3)], "count": len(items)})
    now = zim_now()
    return render_pdf("catalog.html", co=company(), sections=sections, total=sum(s["count"] for s in sections),
                      today=now, current_year=now.year)


def stock_list_pdf(products):
    """Internal stock list: grouped by category with photo, quantity and selling price, plus totals."""
    from ..services.specs import CATEGORY_ORDER, normalize_category, short_title
    from .pdf_docs import company
    from .timeutil import zim_now
    from ..services import photos

    groups = {}
    units = value = 0
    for p in products:
        qty = int(p.quantity or 0)
        units += qty
        value += qty * float(p.price or 0)
        groups.setdefault(normalize_category(p.category), []).append({
            "title": short_title(p.name, 70), "qty": qty, "price": float(p.price or 0),
            "image": photos.card_file((p.image_filename or "").strip()),
        })
    ordered = sorted(groups, key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else len(CATEGORY_ORDER))
    sections = [{"name": c, "lines": sorted(groups[c], key=lambda i: (-i["qty"], i["title"].lower()))} for c in ordered]
    now = zim_now()
    return render_pdf("stock_list.html", co=company(), sections=sections, products=sum(len(s["lines"]) for s in sections),
                      units=units, value=value, in_stock=sum(1 for s in sections for i in s["lines"] if i["qty"] > 0),
                      out_of_stock=sum(1 for s in sections for i in s["lines"] if i["qty"] <= 0), today=now)
