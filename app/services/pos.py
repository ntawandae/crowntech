"""
Point-of-sale rules shared by the till, quotation conversion and reports.

* price_sale()        - validates discount (amount or percent) + delivery fee and returns exact totals
* allocate_discount() - spreads a discount over the lines to the cent, so lines add up to the receipt
* complete_sale()     - one place that writes Receipt + Sale rows, moves stock, books delivery income
* recompute_day()     - rebuilds a day's cash balance from the records (never drifts)

Money maths uses Decimal; the database columns are floats, so values are rounded to cents first.
"""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
MAX_DELIVERY_FEE = Decimal("10000")


class PricingError(Exception):
    """Something the cashier can fix (shown on screen)."""


def D(value):
    """Parse a user/DB number into a 2-decimal Decimal."""
    if value is None or str(value).strip() == "":
        value = 0
    try:
        return Decimal(str(value).strip().replace(",", "")).quantize(CENT, ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise PricingError("Enter amounts as plain numbers, for example 12.50.")


def price_sale(subtotal, mode="amount", value=0, delivery_fee=0, max_discount_percent=0):
    """
    subtotal: sum of line totals before discount.
    mode: "amount" (a $ figure) or "percent" (0-100).
    max_discount_percent: 0 means no limit; otherwise the largest discount allowed (as % of subtotal).
    Returns Decimals: subtotal, discount, discount_percent, delivery_fee, total.
    """
    subtotal, value, fee = D(subtotal), D(value), D(delivery_fee)
    if mode not in ("amount", "percent"):
        raise PricingError("Choose whether the discount is an amount or a percentage.")
    if value < 0:
        raise PricingError("A discount can't be negative.")
    if fee < 0:
        raise PricingError("The delivery fee can't be negative.")
    if fee > MAX_DELIVERY_FEE:
        raise PricingError("That delivery fee looks too high. Check the amount.")

    if mode == "percent":
        if value > 100:
            raise PricingError("A percentage discount can't be more than 100%.")
        discount = (subtotal * value / 100).quantize(CENT, ROUND_HALF_UP)
    else:
        discount = value
    if discount > subtotal:
        raise PricingError("The discount can't be more than the subtotal.")

    percent = (discount * 100 / subtotal).quantize(CENT, ROUND_HALF_UP) if subtotal > 0 else Decimal("0.00")
    if max_discount_percent and percent > Decimal(str(max_discount_percent)):
        raise PricingError(f"Discounts above {max_discount_percent}% need an admin.")

    return {
        "subtotal": subtotal,
        "discount": discount,
        "discount_percent": percent,
        "delivery_fee": fee,
        "total": subtotal - discount + fee,
    }


def allocate_discount(line_totals, discount):
    """Split `discount` across lines in proportion to their value; shares add up exactly."""
    line_totals = [D(t) for t in line_totals]
    discount = D(discount)
    base = sum(line_totals, Decimal("0"))
    if base <= 0 or discount <= 0:
        return [Decimal("0.00") for _ in line_totals]
    exact = [t * discount / base for t in line_totals]
    shares = [e.quantize(CENT, ROUND_DOWN) for e in exact]
    leftover_cents = int((discount - sum(shares, Decimal("0"))) / CENT)
    order = sorted(range(len(exact)), key=lambda i: exact[i] - shares[i], reverse=True)
    for i in order[:leftover_cents]:
        shares[i] += CENT
    return shares


# ----------------------------------------------------------------------
# everything below needs the database / Flask
# ----------------------------------------------------------------------
class SaleError(Exception):
    """Problem completing a sale (stock, missing product...). Message is safe to show."""


def recompute_day(day):
    """Rebuild one day's cash balance from the actual records."""
    from datetime import timedelta
    from sqlalchemy import func
    from .. import db
    from ..models import DailyCashBalance, Expense, ExtraIncome, Sale
    from ..utils.timeutil import day_range, zim_now

    start, end = day_range(day)
    sales = db.session.query(func.sum(Sale.total_price)).filter(Sale.timestamp.between(start, end)).scalar() or 0.0
    income = db.session.query(func.sum(ExtraIncome.amount)).filter(ExtraIncome.timestamp.between(start, end)).scalar() or 0.0
    spent = db.session.query(func.sum(Expense.amount)).filter(Expense.timestamp.between(start, end)).scalar() or 0.0

    prev = DailyCashBalance.query.filter_by(date=day - timedelta(days=1)).first()
    opening = round(prev.closing_balance if prev else 0.0, 2)
    row = DailyCashBalance.query.filter_by(date=day).first()
    if row:
        opening = round(row.opening_balance, 2) if prev is None else opening
    else:
        row = DailyCashBalance(date=day, opening_balance=opening, closing_balance=opening)
        db.session.add(row)
    row.opening_balance = opening
    row.closing_balance = round(opening + sales + income - spent, 2)
    row.updated_at = zim_now()
    db.session.commit()
    return row


def next_receipt_number(prefix, day):
    from ..models import Receipt
    head = f"{prefix}-{day.strftime('%Y%m%d')}-"
    last = (Receipt.query.filter(Receipt.receipt_number.like(f"{head}%"))
            .order_by(Receipt.receipt_number.desc()).first())
    n = int(last.receipt_number.split("-")[-1]) + 1 if last else 1
    while Receipt.query.filter_by(receipt_number=f"{head}{n:04d}").first():
        n += 1
    return f"{head}{n:04d}"


# ----------------------------------------------------------------------
# double-entry guard
# ----------------------------------------------------------------------
def duplicate_window_minutes():
    """How far back to look for a repeat (config DUPLICATE_WINDOW_MINUTES; 0 switches the check off)."""
    from flask import current_app
    try:
        return max(0, int(current_app.config.get("DUPLICATE_WINDOW_MINUTES", 10) or 0))
    except (TypeError, ValueError):
        return 10


def ago_text(seconds):
    """'just now', '3 minutes ago' ... for the confirmation screen."""
    seconds = max(0, int(seconds))
    if seconds < 45:
        return "just now"
    minutes = round(seconds / 60)
    return "1 minute ago" if minutes <= 1 else f"{minutes} minutes ago"


def find_recent_duplicate_sale(lines, minutes=None):
    """
    Look for a till sale made in the last `minutes` with exactly the same products and quantities
    (by any cashier). Online-shop receipts (WEB-...) are ignored. Returns None or a dict:
    {receipt, who, ago, seconds}.
    """
    from datetime import timedelta
    from collections import Counter
    from ..models import Receipt, User
    from ..utils.timeutil import zim_now

    minutes = duplicate_window_minutes() if minutes is None else minutes
    if not minutes or not lines:
        return None
    wanted = Counter()
    for ln in lines:
        wanted[ln["product"].id] += int(ln["qty"])

    now = zim_now()
    recent = (Receipt.query
              .filter(Receipt.receipt_number.like("SALE-%"), Receipt.timestamp >= now - timedelta(minutes=minutes))
              .order_by(Receipt.timestamp.desc()).limit(50).all())
    for receipt in recent:
        have = Counter()
        for sale in receipt.sales:               # reversed lines are deleted, so a reversed sale never matches
            have[sale.product_id] += int(sale.quantity or 0)
        if have == wanted:
            seconds = (now - receipt.timestamp).total_seconds()
            who = User.query.get(receipt.cashier_id)
            return {"receipt": receipt, "who": (who.full_name or who.username) if who else "another cashier",
                    "ago": ago_text(seconds), "seconds": seconds}
    return None


def find_recent_duplicate_entry(model, description, amount, minutes=None):
    """Same idea for an Expense / ExtraIncome row: same description (ignoring case) and same amount."""
    from datetime import timedelta
    from sqlalchemy import func
    from ..models import User
    from ..utils.timeutil import zim_now

    minutes = duplicate_window_minutes() if minutes is None else minutes
    if not minutes:
        return None
    now = zim_now()
    row = (model.query
           .filter(model.timestamp >= now - timedelta(minutes=minutes),
                   func.lower(func.trim(model.description)) == (description or "").strip().lower(),
                   func.abs(model.amount - float(amount)) < 0.005)
           .order_by(model.timestamp.desc()).first())
    if not row:
        return None
    seconds = (now - row.timestamp).total_seconds()
    who_id = getattr(row, "recorded_by", None)
    who = User.query.get(who_id) if who_id else None
    return {"row": row, "who": (who.full_name or who.username) if who else "someone",
            "ago": ago_text(seconds), "seconds": seconds}


def duplicate_prompt(headline, details, confirm_label, back_url, back_label="No, go back"):
    """
    The 'this looks like a repeat - record it anyway?' screen. It re-posts exactly what was submitted
    (every form field) plus confirm_duplicate=1, to the same address.
    details: [(label, value)] describing the earlier record.
    """
    from flask import render_template, request
    fields = [(k, v) for k, v in request.form.items(multi=True) if k not in ("confirm_duplicate",)]
    return render_template("staff/confirm_duplicate.html", headline=headline, details=details, fields=fields,
                           post_url=request.path, confirm_label=confirm_label, back_url=back_url, back_label=back_label)


def complete_sale(lines, user, pricing, customer=None, delivery_address=None, quotation=None):
    """
    Record a finished sale.

    lines:   [{"product": Product, "qty": int, "price": float}]  (price = unit price charged)
    pricing: the dict from price_sale()
    Returns {"receipt": Receipt, "items": [...for the receipt template...], "pricing": pricing}
    Raises SaleError and leaves the database untouched if anything is wrong.
    """
    from .. import db
    from ..models import ExtraIncome, Product, Receipt, Sale
    from ..utils.timeutil import zim_now

    customer = customer or {}
    if not lines:
        raise SaleError("There is nothing to sell.")
    ts = zim_now()
    try:
        # 1. take the stock - only succeeds if enough is left right now
        for ln in lines:
            taken = (Product.query.filter(Product.id == ln["product"].id, Product.quantity >= ln["qty"])
                     .update({Product.quantity: Product.quantity - ln["qty"]}, synchronize_session=False))
            if not taken:
                raise SaleError(f"Not enough stock for {ln['product'].name.split('|')[0].strip()[:60]}.")

        # 2. receipt
        number = next_receipt_number("SALE", ts.date())
        receipt = Receipt(
            receipt_number=number,
            subtotal=float(pricing["subtotal"]), tax=0.0, total=float(pricing["total"]),
            discount=float(pricing["discount"]), delivery_fee=float(pricing["delivery_fee"]),
            delivery_address=(delivery_address or "").strip()[:300] or None,
            customer_name=(customer.get("name") or "").strip()[:100],
            customer_email=(customer.get("email") or "").strip()[:120],
            customer_phone=(customer.get("phone") or "").strip()[:20],
            cashier_id=user.id, timestamp=ts,
        )
        db.session.add(receipt)
        db.session.flush()

        # 3. one Sale row per line, carrying its share of the discount (so profit is true profit)
        gross = [D(ln["price"]) * ln["qty"] for ln in lines]
        shares = allocate_discount(gross, pricing["discount"])
        items = []
        for ln, g, share in zip(lines, gross, shares):
            net = g - share
            cost = D(ln["product"].cost_price or 0) * ln["qty"]
            db.session.add(Sale(
                product_id=ln["product"].id, quantity=ln["qty"], total_price=float(net),
                profit=float(net - cost), cashier_id=user.id, timestamp=ts, receipt_id=receipt.id,
            ))
            items.append({
                "id": ln["product"].id, "name": ln["product"].name, "price": float(D(ln["price"])),
                "quantity": ln["qty"], "original_total_price": float(g), "total_price": float(net),
            })

        # 4. delivery fee is money received that isn't a product: book it as income so every
        #    existing report and the cash balance pick it up without changes
        if pricing["delivery_fee"] > 0:
            db.session.add(ExtraIncome(
                description=f"Delivery fee - {number}", amount=float(pricing["delivery_fee"]),
                recorded_by=user.id, timestamp=ts))

        if quotation is not None:
            quotation.converted = True
        db.session.commit()
    except SaleError:
        db.session.rollback()
        raise
    except Exception as exc:  # unique-number clash, DB error...
        db.session.rollback()
        raise SaleError("The sale could not be saved. Nothing was charged - please try again.") from exc

    recompute_day(ts.date())
    return {"receipt": receipt, "items": items, "pricing": pricing}
