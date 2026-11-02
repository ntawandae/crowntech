"""
Order business rules: reserving stock, confirming payment, cancelling, expiring.

Stock is RESERVED when an order is created (many items here are single units, so
two people must not be able to pay for the same laptop) and given back if the
order is cancelled, fails or expires. When payment is confirmed we also write a
Receipt + Sale rows so online sales show up in the existing sales, profit and
daily reports without any change to them.
"""
import secrets
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy.exc import IntegrityError

from .. import db
from ..models import ExtraIncome, Order, OrderItem, Payment, Product, Receipt, Sale, User

from ..utils.timeutil import zim_now  # noqa: E402  (same Zimbabwe-local convention as the till)
_ORDER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


class OrderError(Exception):
    """A problem the customer can understand and fix (shown on screen)."""



def _new_order_number():
    for _ in range(20):
        number = "CTH-" + "".join(secrets.choice(_ORDER_ALPHABET) for _ in range(6))
        if not Order.query.filter_by(order_number=number).first():
            return number
    raise OrderError("Could not create an order number. Please try again.")


def get_system_user():
    """Internal account that owns online-sale receipts. It cannot log in."""
    user = User.query.filter_by(username="online@system.local").first()
    if not user:
        from werkzeug.security import generate_password_hash
        user = User(
            username="online@system.local",
            password=generate_password_hash(secrets.token_urlsafe(32)),
            role="system",
            full_name="Online shop",
        )
        db.session.add(user)
        db.session.commit()
    return user


# ----------------------------------------------------------------------
# creating an order
# ----------------------------------------------------------------------
def create_order(cart, customer, delivery_method, payment_method, user=None, notes=None):
    """
    cart: {product_id(int): quantity(int)}
    customer: dict(name, email, phone, address)
    Reserves stock atomically and returns the new Order. Raises OrderError.
    """
    cfg = current_app.config
    if not cart:
        raise OrderError("Your cart is empty.")
    if delivery_method == "delivery" and not cfg["DELIVERY_ENABLED"]:
        raise OrderError("Delivery is not available right now.")
    if delivery_method == "delivery" and not (customer.get("address") or "").strip():
        raise OrderError("Please enter a delivery address.")

    products = {p.id: p for p in Product.query.filter(Product.id.in_(list(cart))).all()}
    lines = []
    subtotal = 0.0
    for pid, qty in cart.items():
        product = products.get(pid)
        if not product or (product.price or 0) <= 0:
            raise OrderError("One of the items in your cart is no longer available.")
        if qty < 1:
            continue
        lines.append((product, qty))
        subtotal += product.price * qty
    if not lines:
        raise OrderError("Your cart is empty.")

    fee = cfg["DELIVERY_FEE"] if delivery_method == "delivery" else 0.0
    now = datetime.utcnow()
    hold = (timedelta(hours=cfg["PICKUP_HOLD_HOURS"]) if payment_method == "pay_at_shop"
            else timedelta(minutes=cfg["ONLINE_PAYMENT_HOLD_MINUTES"]))

    order = Order(
        order_number=_new_order_number(),
        access_token=secrets.token_urlsafe(16),
        user_id=user.id if user else None,
        customer_name=customer["name"].strip(),
        customer_email=customer["email"].strip().lower(),
        customer_phone=customer["phone"].strip(),
        delivery_method=delivery_method,
        delivery_address=(customer.get("address") or "").strip() or None,
        notes=(notes or "").strip()[:500] or None,
        subtotal=round(subtotal, 2),
        delivery_fee=round(fee, 2),
        total=round(subtotal + fee, 2),
        currency=cfg["SHOP_CURRENCY"],
        status="reserved" if payment_method == "pay_at_shop" else "pending_payment",
        payment_status="unpaid",
        payment_method=payment_method,
        expires_at=now + hold,
        stock_held=True,
    )
    try:
        for product, qty in lines:
            # Atomic "take stock only if enough remains" - safe against two buyers at once.
            taken = (Product.query
                     .filter(Product.id == product.id, Product.quantity >= qty)
                     .update({Product.quantity: Product.quantity - qty}, synchronize_session=False))
            if not taken:
                raise OrderError(f"Sorry, {product.name.split('|')[0].strip()[:60]} just sold out "
                                 f"or doesn't have {qty} in stock.")
            order.items.append(OrderItem(product_id=product.id, product_name=product.name,
                                         unit_price=product.price, quantity=qty))
        db.session.add(order)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return order


# ----------------------------------------------------------------------
# stock release / cancel / expire
# ----------------------------------------------------------------------
def _release_stock(order):
    if not order.stock_held:
        return
    for item in order.items:
        if item.product_id:
            Product.query.filter_by(id=item.product_id).update(
                {Product.quantity: Product.quantity + item.quantity}, synchronize_session=False)
    order.stock_held = False


def cancel_order(order, reason=None):
    """Cancel an unpaid order and return its stock. Paid orders must be refunded by staff first."""
    if order.payment_status == "paid":
        raise OrderError("This order is already paid. Refund it in Paynow/PayPal, then cancel it here.")
    if order.status == "cancelled":
        return order
    _release_stock(order)
    order.status = "cancelled"
    if order.payment_status == "unpaid":
        order.payment_status = "failed" if order.payments else "unpaid"
    if reason:
        order.staff_note = ((order.staff_note + " | ") if order.staff_note else "") + reason[:200]
    db.session.commit()
    return order


def cancel_paid_order(order, reason):
    """Staff cancel an order that was paid (after refunding outside the system)."""
    _release_stock(order)
    order.status = "cancelled"
    order.payment_status = "refunded"
    order.staff_note = ((order.staff_note + " | ") if order.staff_note else "") + f"Refunded/cancelled: {reason}"[:200]
    db.session.commit()


def release_expired_orders():
    """Give back stock held by unpaid orders whose hold has run out."""
    now = datetime.utcnow()
    expired = (Order.query
               .filter(Order.status.in_(["pending_payment", "reserved"]),
                       Order.payment_status == "unpaid",
                       Order.expires_at < now)
               .all())
    for order in expired:
        # A customer may still be mid-payment on the gateway; only expire if not recently touched.
        recent = [p for p in order.payments if p.updated_at and p.updated_at > now - timedelta(minutes=15)
                  and p.status == "initiated"]
        if recent:
            continue
        _release_stock(order)
        order.status = "cancelled"
        order.staff_note = "Expired - stock released"
    if expired:
        db.session.commit()
    return len(expired)


# ----------------------------------------------------------------------
# confirming payment
# ----------------------------------------------------------------------
def mark_paid(order, method, reference=None, note=None):
    """
    Idempotent. Records the money as received, writes the Receipt/Sale rows and
    moves the order forward. Safe to call twice (e.g. return page + callback).
    """
    if order.payment_status == "paid" and order.receipt_id:
        return order
    get_system_user()  # make sure the internal account exists before we change the order

    # If the hold lapsed and stock was released, take it again or flag for staff.
    if not order.stock_held:
        for item in order.items:
            ok = (Product.query
                  .filter(Product.id == item.product_id, Product.quantity >= item.quantity)
                  .update({Product.quantity: Product.quantity - item.quantity}, synchronize_session=False))
            if not ok:
                db.session.rollback()
                order.staff_note = ((order.staff_note + " | ") if order.staff_note else "") + \
                    "PAID but stock ran out - contact customer / refund"
                order.payment_status = "paid"
                order.status = "paid"
                order.paid_at = datetime.utcnow()
                db.session.commit()
                return order
        order.stock_held = True

    now_utc = datetime.utcnow()
    order.payment_status = "paid"
    order.payment_method = method or order.payment_method
    order.paid_at = order.paid_at or now_utc
    if order.status in ("pending_payment", "reserved", "cancelled"):
        order.status = "paid"
    if note:
        order.staff_note = ((order.staff_note + " | ") if order.staff_note else "") + note[:200]

    for payment in order.payments:
        if payment.provider_ref == reference or (reference is None and payment.status == "initiated"):
            payment.status = "paid"

    try:
        if not order.receipt_id:
            _write_receipt(order)
        db.session.commit()
    except IntegrityError:
        # The other request (Paynow callback vs. customer return) finished first.
        db.session.rollback()
        order = Order.query.filter_by(id=order.id).first()
    else:
        try:
            from . import pos
            pos.recompute_day(zim_now().date())
        except Exception:
            db.session.rollback()
    return order


def _write_receipt(order):
    system_user = get_system_user()
    ts = zim_now()
    receipt = Receipt(
        receipt_number=f"WEB-{order.order_number}"[:20],
        subtotal=order.subtotal,
        tax=0.0,
        total=order.total,
        discount=0.0,
        delivery_fee=order.delivery_fee or 0.0,
        delivery_address=order.delivery_address if order.delivery_method == "delivery" else None,
        customer_name=order.customer_name,
        customer_email=order.customer_email,
        customer_phone=order.customer_phone,
        timestamp=ts,
        cashier_id=system_user.id,
    )
    db.session.add(receipt)
    db.session.flush()
    for item in order.items:
        cost = float(item.product.cost_price or 0) if item.product else 0.0
        db.session.add(Sale(
            product_id=item.product_id,
            quantity=item.quantity,
            total_price=item.line_total,
            profit=(item.unit_price - cost) * item.quantity,
            cashier_id=system_user.id,
            timestamp=ts,
            receipt_id=receipt.id,
        ))
    if order.delivery_fee:
        db.session.add(ExtraIncome(description=f"Delivery fee - {receipt.receipt_number}",
                                   amount=float(order.delivery_fee), recorded_by=system_user.id, timestamp=ts))
    order.receipt_id = receipt.id


def record_payment_attempt(order, provider, provider_ref, poll_url=None):
    payment = Payment(order_id=order.id, provider=provider, provider_ref=provider_ref,
                      poll_url=poll_url, amount=order.total, currency=order.currency)
    db.session.add(payment)
    db.session.commit()
    return payment
