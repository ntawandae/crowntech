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
    cfg = current_app.config
    return render_pdf(
        "receipt_template.html",
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
