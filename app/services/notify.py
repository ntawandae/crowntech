"""Order emails. Never raises: a failed email must not break checkout, payment or staff actions."""
import logging

from flask import url_for

from .mailer import abs_url, send_template

log = logging.getLogger(__name__)

SUBJECTS = {
    "received": "We've reserved your order {n}",
    "paid": "Payment received for order {n}",
    "ready": "Your order {n} is ready",
}


def notify_order(order, kind):
    try:
        link = abs_url(url_for("shop.order_view", order_number=order.order_number, t=order.access_token))
        receipt = abs_url(url_for("shop.order_receipt", order_number=order.order_number, t=order.access_token)) \
            if order.payment_status == "paid" else None
        send_template(order.customer_email, SUBJECTS[kind].format(n=order.order_number),
                      f"order_{kind}", order=order, link=link, receipt_link=receipt)
    except Exception:
        log.exception("Could not queue %s email for %s", kind, getattr(order, "order_number", "?"))
