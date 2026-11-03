"""
Sending email without ever slowing down or breaking a page.

* With MAIL_SERVER set, mail goes out over SMTP (e.g. Aliyun Direct Mail) on a background thread.
* With it empty (development), mail is printed to the console and saved under instance/outbox/.
Failures are logged and swallowed: a customer's order must never fail because an email did.
"""
import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from datetime import datetime

log = logging.getLogger(__name__)


def _clean_header(value):
    """No newlines in headers (blocks header injection)."""
    return " ".join(str(value or "").split())


def build_message(cfg, to, subject, text, html=None):
    msg = EmailMessage()
    msg["Subject"] = _clean_header(subject)[:200]
    msg["From"] = formataddr((_clean_header(cfg["MAIL_FROM_NAME"]), _clean_header(cfg["MAIL_FROM"])))
    msg["To"] = _clean_header(to)
    msg["Message-ID"] = make_msgid(domain=_clean_header(cfg["MAIL_FROM"]).split("@")[-1] or None)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _deliver_smtp(cfg, msg):
    if cfg["MAIL_USE_SSL"]:
        server = smtplib.SMTP_SSL(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=20, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=20)
        if cfg["MAIL_USE_TLS"]:
            server.starttls(context=ssl.create_default_context())
    try:
        if cfg["MAIL_USERNAME"]:
            server.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _deliver_console(instance_path, msg):
    outbox = os.path.join(instance_path, "outbox")
    os.makedirs(outbox, exist_ok=True)
    name = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".eml"
    with open(os.path.join(outbox, name), "wb") as fh:
        fh.write(bytes(msg))
    body = msg.get_body(preferencelist=("plain",))
    print(f"\n----- EMAIL (not sent - MAIL_SERVER is empty) -----\nTo: {msg['To']}\nSubject: {msg['Subject']}\n\n"
          f"{body.get_content() if body else ''}\n---------------------------------------------------\n")


def send_email(to, subject, text, html=None):
    """Queue an email. Returns immediately."""
    from flask import current_app
    app = current_app._get_current_object()
    cfg = dict(app.config)
    if not to or "@" not in to:
        return
    msg = build_message(cfg, to, subject, text, html)

    def job():
        try:
            if cfg["MAIL_SERVER"]:
                _deliver_smtp(cfg, msg)
            else:
                _deliver_console(app.instance_path, msg)
        except Exception:
            log.exception("Could not send email to %s", to)

    threading.Thread(target=job, daemon=True).start()


def abs_url(path):
    """Absolute link for use in emails (honours PUBLIC_BASE_URL)."""
    from flask import current_app, request
    base = current_app.config["PUBLIC_BASE_URL"] or request.url_root.rstrip("/")
    return base + path


def send_template(to, subject, template, **ctx):
    """Render email/<template>.txt and .html and queue them."""
    from flask import current_app, render_template
    ctx.setdefault("biz_name", current_app.config["BUSINESS_NAME"])
    ctx.setdefault("biz_address", current_app.config["BUSINESS_ADDRESS"])
    ctx.setdefault("biz_phone", current_app.config["BUSINESS_PHONE"])
    ctx.setdefault("biz_whatsapp", current_app.config["BUSINESS_WHATSAPP"])
    send_email(to, subject, render_template(f"email/{template}.txt", **ctx),
               render_template(f"email/{template}.html", **ctx))
