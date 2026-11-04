"""
Sending email without ever slowing down or breaking a page.

* With MAIL_SERVER set, mail goes out over SMTP (e.g. Aliyun Direct Mail) on a background thread.
* With it empty (development), mail is printed to the console and saved under instance/outbox/.
Failures are logged and swallowed: a customer's order must never fail because an email did.
"""
import logging
import os
import smtplib
import socket
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


def backend_name(cfg):
    """aliyun_api (HTTPS, preferred) > smtp > console (development)."""
    if cfg.get("ALIYUN_DM_ACCESS_KEY_ID"):
        return "aliyun_api"
    if cfg.get("MAIL_SERVER"):
        return "smtp"
    return "console"


def _deliver_aliyun(cfg, to, subject, text, html):
    from . import aliyun_dm
    return aliyun_dm.send_single(
        cfg["ALIYUN_DM_ACCESS_KEY_ID"], cfg["ALIYUN_DM_ACCESS_KEY_SECRET"], cfg["ALIYUN_DM_REGION"],
        _clean_header(cfg["MAIL_FROM"]), _clean_header(to), _clean_header(subject), html=html, text=text,
        from_alias=_clean_header(cfg["MAIL_FROM_NAME"]), reply_to=cfg.get("ALIYUN_DM_REPLY_TO", False),
        timeout=cfg.get("MAIL_TIMEOUT", 20), endpoint=cfg.get("ALIYUN_DM_ENDPOINT") or None)


def _tls_contexts():
    """Normal TLS first; if the handshake fails, retry with the library's broader cipher list
    (Alibaba's docs note some Python builds need this to talk to Direct Mail)."""
    yield ssl.create_default_context()
    relaxed = ssl.create_default_context()
    try:
        relaxed.set_ciphers("DEFAULT")
        yield relaxed
    except ssl.SSLError:
        return


def _deliver_smtp_once(cfg, msg, context):
    timeout = cfg.get("MAIL_TIMEOUT", 20)
    if cfg["MAIL_USE_SSL"]:
        server = smtplib.SMTP_SSL(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=timeout, context=context)
    else:
        server = smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=timeout)
        if cfg["MAIL_USE_TLS"]:
            server.starttls(context=context)
    try:
        if cfg["MAIL_USERNAME"]:
            server.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _deliver_smtp(cfg, msg):
    last = None
    for context in _tls_contexts():
        try:
            return _deliver_smtp_once(cfg, msg, context)
        except ssl.SSLError as exc:
            last = exc
    raise last


def friendly_smtp_error(exc, cfg):
    """Turn a mail failure into something an admin can act on."""
    host, port = cfg.get("MAIL_SERVER"), cfg.get("MAIL_PORT")
    if type(exc).__name__ == "AliyunDMError":
        code, detail = getattr(exc, "code", "") or "", getattr(exc, "message", "") or ""
        if code == "ConnectionError":
            return f"Could not reach Alibaba Direct Mail ({detail}). Check the region setting and that the site has internet access."
        if "InvalidAccessKeyId" in code:
            return "Alibaba does not recognise the AccessKey ID. Re-copy ALIYUN_DM_ACCESS_KEY_ID with no spaces."
        if code.startswith("SignatureDoesNotMatch"):
            return "The AccessKey Secret does not match the AccessKey ID. Re-copy ALIYUN_DM_ACCESS_KEY_SECRET with no spaces."
        if "Timestamp" in code or "TimeStamp" in code:
            return "Alibaba rejected the request time. The server clock may be wrong; try again in a minute."
        if "InvalidMailAddress" in code or "SenderAddress" in code:
            return ("Alibaba says the sender address is not valid for this region. MAIL_FROM must be a sender address created "
                    "in the Direct Mail console under the same region as ALIYUN_DM_REGION.")
        if code.startswith("Forbidden") or "NoPermission" in code or "NotAuthorized" in code:
            return ("The AccessKey is not allowed to send mail. Give its RAM user permission for dm:SingleSendMail "
                    "(for example the AliyunDirectMailFullAccess policy).")
        if "Quota" in code or "Throttling" in code:
            return f"Alibaba is limiting sending right now ({code}). Check the daily quota in the Direct Mail console."
        return f"Alibaba Direct Mail replied: {code}{(' - ' + detail) if detail else ''}"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("The mail server rejected the login. The username must be the full sender address "
                f"({cfg.get('MAIL_USERNAME') or 'not set'}) and the password must be the SMTP password set for that "
                "address in the Direct Mail console (Sender Addresses > Set SMTP Password), not your Alibaba Cloud login.")
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return ("The server refused the sender address. MAIL_FROM must be a sender address created and verified in "
                "Direct Mail, and it must match MAIL_USERNAME.")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The server refused the recipient address. Check it is typed correctly."
    if isinstance(exc, smtplib.SMTPException):
        return f"The mail server answered with an error: {exc}"
    if isinstance(exc, ssl.SSLError):
        return (f"The secure connection to {host}:{port} failed. Port 465 needs MAIL_USE_SSL=true; "
                "port 80 or 25 needs MAIL_USE_SSL=false and MAIL_USE_TLS=true.")
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionRefusedError)):
        return (f"Could not connect to {host} on port {port} (timed out). If this site runs on Render's free plan, "
                "outbound ports 25, 465 and 587 are blocked: use port 80 with MAIL_USE_TLS=true and MAIL_USE_SSL=false, "
                "or move to a paid Render instance.")
    if isinstance(exc, socket.gaierror):
        return f"The mail server name '{host}' could not be found. Check MAIL_SERVER for typos."
    return f"{type(exc).__name__}: {exc}"


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
            backend = backend_name(cfg)
            if backend == "aliyun_api":
                _deliver_aliyun(cfg, to, subject, text, html)
            elif backend == "smtp":
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


def send_email_sync(to, subject, text, html=None):
    """Send right now and report the outcome: (ok, message). Used by the admin 'Email setup' page."""
    from flask import current_app
    cfg = dict(current_app.config)
    if not to or "@" not in to:
        return False, "Enter a valid email address."
    backend = backend_name(cfg)
    if backend == "console":
        msg = build_message(cfg, to, subject, text, html)
        _deliver_console(current_app.instance_path, msg)
        return True, "No email service is configured, so nothing was sent. A copy was saved in instance/outbox/ instead."
    try:
        if backend == "aliyun_api":
            _deliver_aliyun(cfg, to, subject, text, html)
        else:
            _deliver_smtp(cfg, build_message(cfg, to, subject, text, html))
    except Exception as exc:
        log.warning("Test email failed: %s", exc)
        return False, friendly_smtp_error(exc, cfg)
    return True, f"Sent to {to} through {'Alibaba Direct Mail (API)' if backend == 'aliyun_api' else 'SMTP'}. Check the inbox and the spam folder."
