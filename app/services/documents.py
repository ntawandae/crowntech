"""
Quotation and receipt emails.

Every email carries a ready-written message, a secure link to the PDF (works for the customer without logging in),
and the PDF itself as an attachment when the mail service can carry one (SMTP). Alibaba's HTTPS mail API cannot
attach files, so on that service the link is how the customer gets the PDF.
"""
import re

from flask import current_app, render_template, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..utils.timeutil import zim_now
from . import mailer

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LINK_DAYS = {"q": 60, "r": 365}           # how long a quotation / receipt link keeps working


def valid_email(value):
    value = (value or "").strip()
    return bool(EMAIL_RE.match(value)) and len(value) <= 120


# ---------------------------------------------------------------- secure PDF links
def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="crown-document-link")


def make_link(kind, obj_id):
    token = _serializer().dumps([kind, int(obj_id)])
    return mailer.abs_url(url_for("shop.document_pdf", token=token))


def read_link(token):
    """(kind, id) for a good, unexpired link; otherwise None."""
    try:
        data, issued = _serializer().loads(token, max_age=max(LINK_DAYS.values()) * 86400, return_timestamp=True)
        kind, obj_id = data
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None
    from datetime import datetime, timezone
    age_days = (datetime.now(timezone.utc) - issued.replace(tzinfo=issued.tzinfo or timezone.utc)).days
    if kind not in LINK_DAYS or age_days > LINK_DAYS[kind]:
        return None
    return kind, int(obj_id)


# ---------------------------------------------------------------- email building
def _context(**extra):
    from ..utils.pdf_docs import company
    cfg = current_app.config
    co = company()
    ctx = {"biz_name": cfg["BUSINESS_NAME"], "biz_address": cfg["BUSINESS_ADDRESS"], "biz_phone": cfg["BUSINESS_PHONE"],
           "biz_whatsapp": cfg["BUSINESS_WHATSAPP"], "biz_email": cfg.get("BUSINESS_EMAIL", ""), "phones": co["phones"]}
    ctx.update(extra)
    return ctx


def _served_by(receipt):
    from ..models import User
    user = User.query.get(receipt.cashier_id) if receipt.cashier_id else None
    return (user.full_name or user.username) if user else current_app.config["BUSINESS_NAME"]


def email_quotation(quotation, to_email, sender, note=""):
    """Email a quotation now. Returns (ok, message for the person who clicked)."""
    from .. import db
    from ..utils.receipts import quotation_pdf
    to = (to_email or quotation.customer_email or "").strip()
    if not valid_email(to):
        return False, "Enter a valid email address for the customer."
    cfg = current_app.config
    prepared_by = (quotation.created_by.full_name if quotation.created_by else None) or sender.full_name
    pdf = quotation_pdf(quotation, prepared_by)
    attachments = None
    if pdf is not None and mailer.supports_attachments(cfg):
        attachments = [(f"Quotation_{quotation.quotation_number}.pdf", pdf.getvalue(), "application/pdf")]
    days = int(cfg.get("QUOTATION_VALID_DAYS") or 0)
    valid_until = (quotation.created_at + __import__("datetime").timedelta(days=days)) if (days and quotation.created_at) else None
    ctx = _context(q=quotation, name=(quotation.customer_name or "").strip(), link=make_link("q", quotation.id),
                   total=quotation.total_price - (quotation.discount or 0), valid_until=valid_until,
                   sender_name=sender.full_name or sender.username, note=(note or "").strip()[:600],
                   attached=bool(attachments))
    subject = f"Quotation {quotation.quotation_number} from {cfg['BUSINESS_NAME']}"
    ok, message = mailer.send_email_now(to, subject, render_template("email/quotation.txt", **ctx),
                                        render_template("email/quotation.html", **ctx), attachments)
    if ok:
        quotation.emailed_at = zim_now()
        if not quotation.customer_email:
            quotation.customer_email = to
        db.session.commit()
    return ok, message


def email_receipt(receipt, to_email=None, background=False):
    """
    Email a receipt. background=True queues it (used right after a sale so the till is never slowed down).
    Returns (ok, message).
    """
    from .. import db
    from ..utils.receipts import receipt_items, receipt_pdf
    to = (to_email or receipt.customer_email or "").strip()
    if not valid_email(to):
        return False, "Enter a valid email address for the customer."
    cfg = current_app.config
    items = receipt_items(receipt)
    served_by = _served_by(receipt)
    attachments = None
    if mailer.supports_attachments(cfg):
        pdf = receipt_pdf(receipt, items, served_by)
        if pdf is not None:
            attachments = [(f"Receipt_{receipt.receipt_number}.pdf", pdf.getvalue(), "application/pdf")]
    ctx = _context(receipt=receipt, items=items, name=(receipt.customer_name or "").strip(),
                   link=make_link("r", receipt.id), attached=bool(attachments))
    subject = f"Your receipt {receipt.receipt_number} from {cfg['BUSINESS_NAME']}"
    text, html = render_template("email/receipt.txt", **ctx), render_template("email/receipt.html", **ctx)
    if background:
        mailer.send_email(to, subject, text, html, attachments)
        ok, message = True, f"Receipt is on its way to {to}."
    else:
        ok, message = mailer.send_email_now(to, subject, text, html, attachments)
    if ok:
        receipt.emailed_at = zim_now()
        if not receipt.customer_email:
            receipt.customer_email = to
        db.session.commit()
    return ok, message


def auto_email_receipt(receipt):
    """After a sale: if the customer gave an email address, send their receipt. Never raises. Returns a message or None."""
    if not receipt or not valid_email(receipt.customer_email):
        return None
    try:
        ok, message = email_receipt(receipt, background=True)
        return message if ok else None
    except Exception:
        current_app.logger.exception("Could not queue the receipt email")
        return None
