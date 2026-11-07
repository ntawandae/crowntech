"""Shared pieces for every downloadable document (receipt, quotation, catalogue, stock list)."""
import re
import textwrap

from flask import current_app, url_for

from ..services.specs import clean_text

_PLAIN = re.compile(r"\s+")


def address_lines(address, width=60):
    """Wrap a comma separated address into tidy lines without breaking inside a part."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    lines, current = [], ""
    for part in parts:
        candidate = f"{current}, {part}" if current else part
        if current and len(candidate) > width:
            lines.append(current + ",")
            current = part
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def company():
    """Everything the document header and footer need, taken from the app settings."""
    cfg = current_app.config
    phones = [p.strip() for p in (cfg.get("BUSINESS_PHONES") or cfg.get("BUSINESS_PHONE") or "").split(",") if p.strip()]
    return {
        "name": cfg["BUSINESS_NAME"],
        "address_lines": address_lines(cfg["BUSINESS_ADDRESS"]),
        "phones": phones,
        "email": cfg.get("BUSINESS_EMAIL") or "",
        "website": cfg.get("BUSINESS_WEBSITE") or "",
        "logo": url_for("static", filename="images/cthlogo_pdf.png"),
    }


def pdftext(value, limit=None):
    """Text that the PDF's built-in font can print: emoji and decorative symbols are removed, spacing tidied."""
    text = clean_text(str(value or ""))
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u2014", "-").replace("\u2013", "-")
    text = text.encode("cp1252", "ignore").decode("cp1252")
    text = _PLAIN.sub(" ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip(" ,.-|") + "..."
    return text


def pdfmoney(value):
    try:
        return f"${float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def register(app):
    app.jinja_env.filters["pdftext"] = pdftext
    app.jinja_env.filters["pdfmoney"] = pdfmoney
