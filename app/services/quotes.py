"""Quotation rules shared by admin and cashier: parsing, saving, numbering, converting to a sale."""
import json
import re

from . import pos
from .pos import D, PricingError, SaleError


def generate_quotation_number():
    """Next number after the highest existing CTHQ### (case-insensitive) - never reuses a number."""
    from ..models import Quotation
    highest = 0
    for (num,) in Quotation.query.with_entities(Quotation.quotation_number).all():
        m = re.fullmatch(r"cthq(\d+)", (num or "").strip().lower())
        if m:
            highest = max(highest, int(m.group(1)))
    return f"CTHQ{highest + 1:03d}"


def parse_quote_form(form, max_discount_percent=0):
    """
    Read the quotation form. Returns (customer_name, items, discount_amount).
    items: [{"product_id": int|None, "name": str, "quantity": int, "unit_price": Decimal}]
    Raises PricingError with a message the user can act on.
    """
    from .. import db
    from ..models import Product

    customer = (form.get("customer_name") or "").strip()
    if not customer:
        raise PricingError("Enter the customer's name.")
    try:
        raw = json.loads(form.get("items") or "[]")
    except json.JSONDecodeError:
        raise PricingError("The item list could not be read. Please re-add the items.")
    if not isinstance(raw, list) or not raw:
        raise PricingError("Add at least one item.")

    items = []
    for r in raw:
        try:
            qty = int(r.get("quantity", 1))
        except (TypeError, ValueError):
            raise PricingError("Quantities must be whole numbers.")
        if qty < 1:
            raise PricingError("Quantities must be at least 1.")
        price = D(r.get("unit_price"))
        if price < 0:
            raise PricingError("Prices can't be negative.")
        pid = r.get("product_id")
        if pid in (None, "", "__custom__"):
            name = (r.get("product_name") or "").strip()
            if not name:
                raise PricingError("Give each custom item a name.")
            items.append({"product_id": None, "name": name[:150], "quantity": qty, "unit_price": price})
        else:
            product = db.session.get(Product, int(pid))
            if not product:
                raise PricingError("One of the products no longer exists.")
            items.append({"product_id": product.id, "name": product.name[:150], "quantity": qty, "unit_price": price})

    subtotal = sum((i["unit_price"] * i["quantity"] for i in items), D(0))
    pricing = pos.price_sale(subtotal, form.get("discount_mode", "amount"), form.get("discount_value") or 0,
                             0, max_discount_percent)
    return customer, items, pricing["discount"]


_EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UNSET = object()


def parse_contact(form):
    """The optional customer email and phone on the quotation form. Returns (email or None, phone or None)."""
    email = (form.get("customer_email") or "").strip().lower()
    phone = " ".join((form.get("customer_phone") or "").split())
    if email and not (_EMAIL_OK.match(email) and len(email) <= 120):
        raise PricingError("That customer email address doesn't look right.")
    if len(phone) > 30:
        raise PricingError("The customer phone number is too long.")
    return email or None, phone or None


def save_quotation(user, customer, items, discount, quotation=None, email=_UNSET, phone=_UNSET):
    """Create a quotation, or replace the items/discount of an existing one."""
    from .. import db
    from ..models import Quotation, QuotationItem
    from ..utils.timeutil import zim_now

    if quotation is None:
        quotation = Quotation(quotation_number=generate_quotation_number(), created_by_id=user.id,
                              created_at=zim_now())
        db.session.add(quotation)
    quotation.customer_name = customer
    if email is not _UNSET:
        quotation.customer_email = email
    if phone is not _UNSET:
        quotation.customer_phone = phone
    quotation.discount = float(discount)
    quotation.items.clear()
    db.session.flush()
    for i in items:
        quotation.items.append(QuotationItem(
            product_id=i["product_id"], product_name=i["name"], quantity=i["quantity"],
            unit_price=float(i["unit_price"]), total_price=float(i["unit_price"] * i["quantity"])))
    db.session.commit()
    return quotation


def quotation_edit_payload(quotation):
    """What the edit form needs to pre-fill itself."""
    return {
        "customer": quotation.customer_name or "",
        "email": quotation.customer_email or "",
        "phone": quotation.customer_phone or "",
        "discount": float(quotation.discount or 0),
        "items": [{"product_id": it.product_id, "product_name": it.product_name,
                   "quantity": it.quantity, "unit_price": float(it.unit_price)} for it in quotation.items],
    }


def convert_to_sale(quotation, user, customer_email="", customer_phone="", delivery_fee=0):
    """Turn a quotation into a recorded sale at the quoted prices and discount."""
    from .. import db
    from ..models import Product

    if quotation.converted:
        raise SaleError("This quotation has already been converted to a sale.")
    lines = []
    for it in quotation.items:
        if not it.product_id:
            raise SaleError(f"'{it.product_name}' is a custom item, so it can't be sold from stock. "
                            "Add it as a product first.")
        product = db.session.get(Product, it.product_id)
        if not product:
            raise SaleError(f"Product '{it.product_name}' no longer exists.")
        lines.append({"product": product, "qty": it.quantity, "price": it.unit_price})
    subtotal = sum((D(l["price"]) * l["qty"] for l in lines), D(0))
    pricing = pos.price_sale(subtotal, "amount", quotation.discount or 0, delivery_fee)
    return pos.complete_sale(lines, user, pricing,
                             customer={"name": quotation.customer_name, "email": customer_email, "phone": customer_phone},
                             quotation=quotation)
