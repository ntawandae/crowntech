"""Public storefront: browse, cart, checkout, payments (Paynow + PayPal), customer accounts."""
import os
import re
import time
from decimal import Decimal
from io import BytesIO

from flask import (Blueprint, abort, current_app, flash, redirect, render_template,
                   request, send_file, session, url_for)
from flask_login import current_user, login_required, login_user
from sqlalchemy.orm import selectinload

from .. import db
from ..models import CustomerAddress, Order, Payment, Product, ProductImage, Sale, User
from ..utils.timeutil import zim_now
from ..security import (clear_failures, client_key, csrf_token, protect_blueprint, record_failure,
                        safe_next, throttled)
from ..services import orders as order_svc
from ..services.photos import photo_src
from ..services.orders import OrderError
from ..services.paynow import PaynowClient, PaynowError, is_failed, is_paid, parse_message, verify_pairs
from ..services.paypal import PayPalClient, PayPalError
import hmac
import secrets
from datetime import timedelta

from ..services import tokens
from ..services.google_auth import GoogleAuthError, build_auth_url, fetch_profile
from ..services.mailer import abs_url, send_template
from ..services.notify import notify_order
from ..services.specs import CATEGORY_ICON, CATEGORY_ORDER, CATEGORY_TONE, extract_specs, normalize_category, short_title, clean_text

shop_bp = Blueprint("shop", __name__)
protect_blueprint(shop_bp, exempt=("shop.paynow_result",))  # Paynow's server callback has no session

PAGE_SIZE = 24
_last_sweep = {"t": 0.0}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def public_base():
    return current_app.config["PUBLIC_BASE_URL"] or request.url_root.rstrip("/")


def paynow_enabled():
    c = current_app.config
    return bool(c["PAYNOW_INTEGRATION_ID"] and c["PAYNOW_INTEGRATION_KEY"])


def paypal_enabled():
    c = current_app.config
    return bool(c["PAYPAL_CLIENT_ID"] and c["PAYPAL_CLIENT_SECRET"])


def paynow_client():
    c = current_app.config
    return PaynowClient(c["PAYNOW_INTEGRATION_ID"], c["PAYNOW_INTEGRATION_KEY"])


def paypal_client():
    c = current_app.config
    return PayPalClient(c["PAYPAL_CLIENT_ID"], c["PAYPAL_CLIENT_SECRET"], c["PAYPAL_MODE"])


def sweep_expired():
    """Release held stock from abandoned orders (at most once a minute per process)."""
    if time.time() - _last_sweep["t"] > 60:
        _last_sweep["t"] = time.time()
        try:
            order_svc.release_expired_orders()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Could not release expired orders")


def photo_url(filename, size="card"):
    """Uniform 4:3 frame for one photo: from the local disk (older photos) or Supabase Storage. None if not found."""
    return photo_src(filename, size)


def image_url(product, size="card"):
    """The product's main photo (None if the photo file is missing)."""
    return photo_url(product.image_filename, size)


def present(p, size="card"):
    """Storefront view of a Product. Never exposes cost price or profit."""
    qty = p.quantity or 0
    category = normalize_category(p.category)
    return {
        "id": p.id,
        "title": short_title(p.name),
        "full_name": clean_text(p.name),
        "brand": p.brand,
        "category": category,
        "tone": CATEGORY_TONE.get(category, "steel"),
        "price": p.price,
        "qty": qty,
        "in_stock": qty > 0,
        "low_stock": 0 < qty <= 2,
        "image": image_url(p, size),
        # every photo that exists (main first): cards rotate through them, the product page shows a gallery
        "images": [u for u in (photo_url(f, size) for f in p.all_images) if u],
        "thumbs": [u for u in (photo_url(f, "card") for f in p.all_images) if u],
        "specs": extract_specs(p.name),
        "added": p.date_added,
    }


def visible_products():
    return Product.query.options(selectinload(Product.extra_images)).filter(Product.price > 0)


def hot_products(limit=10, days=120):
    """Best sellers over the last `days` (till + online), in stock only. Padded with the newest stock."""
    since = zim_now() - timedelta(days=days)
    rows = (db.session.query(Sale.product_id, db.func.sum(Sale.quantity))
            .join(Product, Product.id == Sale.product_id)
            .filter(Sale.timestamp >= since, Product.quantity > 0, Product.price > 0)
            .group_by(Sale.product_id)
            .order_by(db.func.sum(Sale.quantity).desc(), Sale.product_id)
            .limit(limit).all())
    ids = [r[0] for r in rows]
    by_id = {p.id: p for p in Product.query.options(selectinload(Product.extra_images)).filter(Product.id.in_(ids or [0])).all()}
    ordered = [by_id[i] for i in ids if i in by_id]
    if len(ordered) < limit:
        extra = (visible_products().filter(Product.quantity > 0, ~Product.id.in_(ids or [0]))
                 .order_by(Product.date_added.desc()).limit(limit - len(ordered)).all())
        ordered += extra
    return ordered


_nav_cache = {"t": 0.0, "v": []}


def nav_categories():
    """[(name, tone, icon)] for categories that have something in stock (cached for a minute)."""
    if time.time() - _nav_cache["t"] < 60:
        return _nav_cache["v"]
    counts = {}
    for (cat,) in db.session.query(Product.category).filter(Product.price > 0, Product.quantity > 0):
        name = normalize_category(cat)
        counts[name] = counts.get(name, 0) + 1
    value = [(n, CATEGORY_TONE.get(n, "steel"), CATEGORY_ICON.get(n, "box"), counts[n])
             for n in sorted(counts, key=category_rank)]
    _nav_cache.update(t=time.time(), v=value)
    return value


def category_rank(label):
    return CATEGORY_ORDER.index(label) if label in CATEGORY_ORDER else len(CATEGORY_ORDER)


def get_cart():
    raw = session.get("shop_cart") or {}
    cart = {}
    for k, v in raw.items():
        try:
            cart[int(k)] = max(0, int(v))
        except (TypeError, ValueError):
            continue
    return cart


def save_cart(cart):
    session["shop_cart"] = {str(k): v for k, v in cart.items() if v > 0}
    session.modified = True


def cart_detail(cart):
    """Lines for the cart/checkout pages, with quantities clamped to live stock."""
    products = {p.id: p for p in Product.query.options(selectinload(Product.extra_images)).filter(Product.id.in_(list(cart) or [0])).all()}
    lines, subtotal, changed = [], 0.0, False
    for pid, qty in cart.items():
        p = products.get(pid)
        if not p or (p.price or 0) <= 0:
            cart[pid] = 0
            changed = True
            continue
        stock = p.quantity or 0
        if qty > stock:
            qty = cart[pid] = stock
            changed = True
        if qty < 1:
            changed = True
            continue
        view = present(p)
        total = round(p.price * qty, 2)
        subtotal += total
        lines.append({"p": view, "qty": qty, "total": total})
    if changed:
        save_cart(cart)
    return lines, round(subtotal, 2)


def order_for_request(order_number):
    """Fetch an order the current visitor is allowed to see, or 404."""
    order = Order.query.filter_by(order_number=order_number.upper()).first_or_404()
    token = request.values.get("t", "")
    owner = current_user.is_authenticated and order.user_id == current_user.id
    staff = current_user.is_authenticated and current_user.role in ("admin", "cashier")
    import hmac
    if not (owner or staff or (token and hmac.compare_digest(token, order.access_token))):
        abort(404)
    return order


def order_url(order, **kw):
    return url_for("shop.order_view", order_number=order.order_number, t=order.access_token, **kw)


# ----------------------------------------------------------------------
# browsing
# ----------------------------------------------------------------------
@shop_bp.route("/")
def home():
    sweep_expired()
    hot = [present(p) for p in hot_products(10)]
    shown = {p["id"] for p in hot}
    fresh = [present(p) for p in visible_products().filter(Product.quantity > 0)
             .order_by(Product.date_added.desc()).limit(14).all() if p.id not in shown][:5]
    total = visible_products().filter(Product.quantity > 0).count()
    return render_template("shop/home.html", hot=hot, fresh=fresh, total=total)


@shop_bp.route("/media/<size>/<path:filename>")
def media(size, filename):
    """Cached, uniformly framed product photos."""
    from ..services.images import get_frame_path
    cache = os.path.join(current_app.instance_path, "thumbs")
    path = get_frame_path(current_app.config["UPLOAD_FOLDER"], cache, size, filename)
    if not path:
        abort(404)
    return send_file(path, mimetype="image/jpeg", max_age=60 * 60 * 24 * 7)


@shop_bp.route("/shop")
def catalog():
    sweep_expired()
    q = (request.args.get("q") or "").strip()
    category = request.args.get("category") or ""
    sort = request.args.get("sort") or "newest"
    show_all = request.args.get("all") == "1"
    page = max(1, request.args.get("page", 1, type=int))

    query = visible_products()
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Product.name.ilike(like), Product.brand.ilike(like)))
    if not show_all:
        query = query.filter(Product.quantity > 0)
    rows = [present(p) for p in query.all()]

    cat_counts = {}
    for r in rows:
        cat_counts[r["category"]] = cat_counts.get(r["category"], 0) + 1
    if category:
        rows = [r for r in rows if r["category"] == category]

    if sort == "price_asc":
        rows.sort(key=lambda r: (not r["in_stock"], r["price"]))
    elif sort == "price_desc":
        rows.sort(key=lambda r: (not r["in_stock"], -r["price"]))
    else:
        rows.sort(key=lambda r: (not r["in_stock"], -(r["added"].timestamp() if r["added"] else 0)))

    total = len(rows)
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, pages)
    rows = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    categories = [(n, c, CATEGORY_TONE.get(n, "steel"))
                  for n, c in sorted(cat_counts.items(), key=lambda kv: category_rank(kv[0]))]
    return render_template("shop/catalog.html", products=rows, q=q, category=category, sort=sort,
                           show_all=show_all, categories=categories, total=total, page=page, pages=pages)


@shop_bp.route("/product/<int:product_id>")
def product(product_id):
    p = visible_products().filter(Product.id == product_id).first_or_404()
    view = present(p, size="large")
    related = [present(r) for r in visible_products()
               .filter(Product.id != p.id, Product.quantity > 0).limit(60).all()
               if normalize_category(r.category) == view["category"]][:4]
    return render_template("shop/product.html", p=view, related=related)


# ----------------------------------------------------------------------
# cart
# ----------------------------------------------------------------------
@shop_bp.route("/cart")
def cart():
    sweep_expired()
    lines, subtotal = cart_detail(get_cart())
    return render_template("shop/cart.html", lines=lines, subtotal=subtotal)


@shop_bp.route("/cart/add/<int:product_id>", methods=["POST"])
def cart_add(product_id):
    p = visible_products().filter(Product.id == product_id).first_or_404()
    qty = max(1, request.form.get("qty", 1, type=int) or 1)
    cart = get_cart()
    have = cart.get(p.id, 0)
    stock = p.quantity or 0
    if stock < 1:
        flash("Sorry, that item is out of stock.", "warning")
    elif have + qty > stock:
        cart[p.id] = stock
        save_cart(cart)
        flash(f"Only {stock} in stock - we've set your cart to the maximum.", "warning")
    else:
        cart[p.id] = have + qty
        save_cart(cart)
        flash("Added to your cart.", "success")
    if request.form.get("buy_now"):
        return redirect(url_for("shop.checkout"))
    return redirect(safe_next(request.form.get("next"), url_for("shop.cart")))


@shop_bp.route("/cart/set/<int:product_id>", methods=["POST"])
def cart_set(product_id):
    cart = get_cart()
    qty = request.form.get("qty", 0, type=int) or 0
    p = Product.query.get(product_id)
    stock = (p.quantity or 0) if p else 0
    cart[product_id] = max(0, min(qty, stock))
    save_cart(cart)
    return redirect(url_for("shop.cart"))


@shop_bp.route("/cart/remove/<int:product_id>", methods=["POST"])
def cart_remove(product_id):
    cart = get_cart()
    cart.pop(product_id, None)
    save_cart(cart)
    return redirect(url_for("shop.cart"))


# ----------------------------------------------------------------------
# checkout
# ----------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def available_payment_methods():
    methods = []
    if paynow_enabled():
        methods.append(("paynow", "Paynow", "EcoCash, OneMoney, InnBucks, Visa/Mastercard and more"))
    if paypal_enabled():
        methods.append(("paypal", "PayPal", "Pay with your PayPal balance or a card"))
    if current_app.config["PAY_ON_PICKUP_ENABLED"]:
        methods.append(("pay_at_shop", "Pay at the shop", "We hold your items; pay when you collect"))
    return methods


@shop_bp.route("/checkout", methods=["GET", "POST"])
def checkout():
    cart = get_cart()
    lines, subtotal = cart_detail(cart)
    if not lines:
        flash("Your cart is empty.", "info")
        return redirect(url_for("shop.catalog"))

    cfg = current_app.config
    methods = available_payment_methods()
    customer = current_user if current_user.is_authenticated and current_user.role == "customer" else None
    saved = list(customer.addresses) if customer else []
    default_addr = next((a for a in saved if a.is_default), saved[0] if saved else None)
    form = {
        "name": (customer.full_name or "") if customer else "",
        "email": customer.username if customer else "",
        "phone": (customer.phone or "") if customer else "",
        "address": "", "notes": "",
        "delivery": "pickup", "payment": methods[0][0] if methods else "",
        # which saved address is selected ("new" = type a different one)
        "address_choice": str(default_addr.id) if default_addr else "new",
        "save_address": bool(customer),
    }

    if request.method == "POST":
        form.update({k: (request.form.get(k) or "").strip() for k in ("name", "email", "phone", "address", "notes")})
        form["delivery"] = request.form.get("delivery", "pickup")
        form["payment"] = request.form.get("payment", "")
        form["address_choice"] = request.form.get("address_choice") or "new"
        form["save_address"] = bool(request.form.get("save_address"))
        chosen_saved = None
        if form["delivery"] == "delivery" and customer and form["address_choice"] != "new":
            # only ever one of this customer's own addresses
            chosen_saved = next((a for a in saved if str(a.id) == form["address_choice"]), None)
            if chosen_saved:
                form["address"] = chosen_saved.full
            else:
                form["address_choice"] = "new"
        errors = []
        if len(form["name"]) < 2:
            errors.append("Enter your full name.")
        if not EMAIL_RE.match(form["email"]):
            errors.append("Enter a valid email address - we send your receipt there.")
        if len(re.sub(r"\D", "", form["phone"])) < 7:
            errors.append("Enter a phone number we can reach you on.")
        if form["delivery"] not in ("pickup", "delivery"):
            errors.append("Choose pickup or delivery.")
        if form["delivery"] == "delivery" and not form["address"]:
            errors.append("Enter a delivery address.")
        if form["payment"] not in [m[0] for m in methods]:
            errors.append("Choose how you'd like to pay.")

        if not errors:
            try:
                order = order_svc.create_order(
                    cart={l["p"]["id"]: l["qty"] for l in lines},
                    customer={"name": form["name"], "email": form["email"],
                              "phone": form["phone"], "address": form["address"]},
                    delivery_method=form["delivery"], payment_method=form["payment"],
                    user=current_user if current_user.is_authenticated and current_user.role == "customer" else None,
                    notes=form["notes"],
                )
            except OrderError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("shop.cart"))
            save_cart({})
            if customer:
                _remember_checkout_details(customer, form, saved, chosen_saved)
            if order.payment_method == "pay_at_shop":
                notify_order(order, "received")
                flash("Order placed. We're holding your items for you.", "success")
                return redirect(order_url(order))
            return start_payment(order, order.payment_method)
        for e in errors:
            flash(e, "danger")

    delivery_fee = cfg["DELIVERY_FEE"]
    return render_template("shop/checkout.html", lines=lines, subtotal=subtotal, form=form, methods=methods,
                           delivery_enabled=cfg["DELIVERY_ENABLED"], delivery_fee=delivery_fee,
                           saved_addresses=saved, is_customer=bool(customer))


def _remember_checkout_details(customer, form, saved, chosen_saved):
    """After an order is placed: keep the phone number, and save a newly typed address if they ticked the box."""
    try:
        changed = False
        if not customer.phone and form["phone"]:
            customer.phone = form["phone"][:30]
            changed = True
        if form["delivery"] == "delivery" and not chosen_saved and form["save_address"] and form["address"]:
            text = form["address"][:220]
            if not any(a.full.lower() == text.lower() or a.address.lower() == text.lower() for a in saved):
                db.session.add(CustomerAddress(user_id=customer.id, label=_next_label(saved), address=text,
                                               is_default=not saved))
                changed = True
        if changed:
            db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Could not save checkout details to the customer's account")


def _next_label(saved):
    """First free label from Home / Work / Other, so new addresses don't all look the same."""
    used = {a.label.lower() for a in saved}
    for label in ("Home", "Work", "Other"):
        if label.lower() not in used:
            return label
    return f"Address {len(saved) + 1}"


# ----------------------------------------------------------------------
# payments
# ----------------------------------------------------------------------
def start_payment(order, provider):
    """Create a payment attempt with the gateway and send the customer there."""
    if order.status != "pending_payment" or order.payment_status == "paid":
        flash("This order can't be paid online any more.", "warning")
        return redirect(order_url(order))
    attempts = len([p for p in order.payments if p.provider == provider])
    try:
        if provider == "paynow" and paynow_enabled():
            reference = order.order_number if attempts == 0 else f"{order.order_number}-{attempts + 1}"
            payer_email = current_app.config["PAYNOW_TEST_EMAIL"] or order.customer_email
            info = paynow_client().initiate(
                reference=reference, amount=order.total,
                additional_info=f"{current_app.config['BUSINESS_NAME']} order {order.order_number}",
                return_url=public_base() + url_for("shop.paynow_return", order_number=order.order_number, t=order.access_token),
                result_url=public_base() + url_for("shop.paynow_result"),
                payer_email=payer_email,
            )
            order_svc.record_payment_attempt(order, "paynow", reference, info["pollurl"])
            return redirect(info["browserurl"])
        if provider == "paypal" and paypal_enabled():
            approve_ref, approve_url = paypal_client().create_order(
                reference=order.order_number, amount=order.total, currency=order.currency,
                description=f"{current_app.config['BUSINESS_NAME']} order {order.order_number}",
                return_url=public_base() + url_for("shop.paypal_return", order_number=order.order_number, t=order.access_token),
                cancel_url=public_base() + order_url(order),
                brand_name=current_app.config["BUSINESS_NAME"], attempt=attempts + 1,
            )
            order_svc.record_payment_attempt(order, "paypal", approve_ref)
            return redirect(approve_url)
        flash("That payment method isn't available right now.", "danger")
    except (PaynowError, PayPalError) as exc:
        current_app.logger.warning("Payment start failed for %s: %s", order.order_number, exc)
        flash(f"We couldn't start the payment: {exc} Your items are still held - try again or choose another method.", "danger")
    return redirect(order_url(order))


@shop_bp.route("/order/<order_number>/pay/<provider>", methods=["POST"])
def pay_again(order_number, provider):
    order = order_for_request(order_number)
    if provider not in ("paynow", "paypal"):
        abort(404)
    return start_payment(order, provider)


def _apply_paynow_status(order, payment, data):
    """data = verified Paynow status fields. Returns True if the order became paid."""
    status = data.get("status", "")
    payment.detail = f"Paynow: {status}"[:500]
    if is_paid(status):
        try:
            same_amount = Decimal(data.get("amount", "0")) == Decimal(str(payment.amount)).quantize(Decimal("0.01"))
        except Exception:
            same_amount = False
        if not same_amount:
            payment.detail = f"Paynow paid but amount mismatch ({data.get('amount')}) - check manually"
            db.session.commit()
            return False
        order_svc.mark_paid(order, "paynow", reference=payment.provider_ref,
                            note=f"Paynow ref {data.get('paynowreference', '')}")
        return True
    if is_failed(status):
        payment.status = "failed"
    db.session.commit()
    return False


def _refresh_paynow(order):
    payment = next((p for p in reversed(order.payments) if p.provider == "paynow" and p.poll_url), None)
    if not payment or order.payment_status == "paid":
        return
    try:
        data = paynow_client().poll(payment.poll_url)
        _apply_paynow_status(order, payment, data)
    except PaynowError as exc:
        current_app.logger.warning("Paynow poll failed for %s: %s", order.order_number, exc)


@shop_bp.route("/pay/paynow/return/<order_number>")
def paynow_return(order_number):
    order = order_for_request(order_number)
    _refresh_paynow(order)   # never trust the browser; ask Paynow directly
    if order.payment_status == "paid":
        flash("Payment received. Thank you!", "success")
    else:
        flash("We haven't received confirmation from Paynow yet. This page updates once the payment clears.", "info")
    return redirect(order_url(order))


@shop_bp.route("/pay/paynow/result", methods=["POST"])
def paynow_result():
    """Server-to-server update from Paynow. Authenticated by the hash, not by a session."""
    if not paynow_enabled():
        abort(404)
    pairs = [(k.lower(), v) for k, v in request.form.items(multi=True)]
    if not verify_pairs(pairs, current_app.config["PAYNOW_INTEGRATION_KEY"]):
        current_app.logger.warning("Paynow callback with bad hash")
        abort(400)
    data = dict(pairs)
    payment = Payment.query.filter_by(provider="paynow", provider_ref=data.get("reference", "")).first()
    if payment:
        _apply_paynow_status(payment.order, payment, data)
    return "OK", 200


@shop_bp.route("/pay/paypal/return/<order_number>")
def paypal_return(order_number):
    order = order_for_request(order_number)
    token = request.args.get("token", "")
    payment = Payment.query.filter_by(order_id=order.id, provider="paypal", provider_ref=token).first()
    if not payment:
        abort(404)
    if order.payment_status == "paid":
        return redirect(order_url(order))
    try:
        summary = paypal_client().capture(token)
    except PayPalError as exc:
        payment.status = "failed"
        payment.detail = str(exc)[:500]
        db.session.commit()
        flash(f"PayPal couldn't complete the payment: {exc} You haven't been charged. Please try again.", "danger")
        return redirect(order_url(order))

    ok = (PayPalClient.is_paid(summary)
          and summary["reference"] == order.order_number
          and summary["currency"] == order.currency
          and summary["amount"] == Decimal(str(order.total)).quantize(Decimal("0.01")))
    if ok:
        payment.detail = f"PayPal capture {summary['capture_id']}"
        order_svc.mark_paid(order, "paypal", reference=token, note=f"PayPal capture {summary['capture_id']}")
        flash("Payment received. Thank you!", "success")
    else:
        payment.detail = f"PayPal status {summary.get('order_status')}/{summary.get('capture_status')} - check manually"[:500]
        db.session.commit()
        flash("PayPal hasn't confirmed this payment yet. If you were charged, contact us with your order number.", "warning")
    return redirect(order_url(order))


@shop_bp.route("/order/<order_number>/check", methods=["POST"])
def check_payment(order_number):
    order = order_for_request(order_number)
    _refresh_paynow(order)
    if order.payment_status == "paid":
        flash("Payment received. Thank you!", "success")
    else:
        flash("Still waiting for the payment to clear.", "info")
    return redirect(order_url(order))


# ----------------------------------------------------------------------
# order page, receipt, tracking
# ----------------------------------------------------------------------
@shop_bp.route("/order/<order_number>")
def order_view(order_number):
    order = order_for_request(order_number)
    can_pay = order.status == "pending_payment" and order.payment_status == "unpaid"
    return render_template("shop/order.html", order=order, can_pay=can_pay,
                           paynow_on=paynow_enabled(), paypal_on=paypal_enabled(),
                           token=order.access_token)


@shop_bp.route("/order/<order_number>/cancel", methods=["POST"])
def order_cancel(order_number):
    order = order_for_request(order_number)
    try:
        order_svc.cancel_order(order, "Cancelled by customer")
        flash("Order cancelled. Your items are back in stock.", "info")
    except OrderError as exc:
        flash(str(exc), "danger")
    return redirect(order_url(order))


@shop_bp.route("/order/<order_number>/receipt.pdf")
def order_receipt(order_number):
    order = order_for_request(order_number)
    if order.payment_status != "paid" or not order.receipt:
        abort(404)
    from ..utils.receipts import receipt_pdf
    items = [{"name": clean_text(i.product_name)[:90], "price": i.unit_price, "quantity": i.quantity,
              "original_total_price": i.line_total, "total_price": i.line_total} for i in order.items]
    pdf = receipt_pdf(order.receipt, items, "Online shop")
    if pdf is None:
        abort(500)
    return send_file(pdf, as_attachment=True, download_name=f"receipt_{order.order_number}.pdf",
                     mimetype="application/pdf")


@shop_bp.route("/track", methods=["GET", "POST"])
def track():
    if request.method == "POST":
        number = (request.form.get("order_number") or "").strip().upper()
        email = (request.form.get("email") or "").strip().lower()
        order = Order.query.filter_by(order_number=number).first()
        if order and order.customer_email == email:
            return redirect(order_url(order))
        flash("We couldn't find an order with that number and email.", "danger")
    return render_template("shop/track.html")


# ----------------------------------------------------------------------
# customer accounts
# ----------------------------------------------------------------------
def home_for(user):
    if user.role == "admin":
        return url_for("admin.admin_dashboard")
    if user.role == "cashier":
        return url_for("cashier.cashier_dashboard")
    return url_for("shop.account")


def send_verification(user):
    raw = tokens.issue(user, tokens.VERIFY, timedelta(hours=current_app.config["EMAIL_LINK_HOURS_VERIFY"]))
    send_template(user.username, f"Confirm your email for {current_app.config['BUSINESS_NAME']}", "verify",
                  name=user.full_name, link=abs_url(url_for("shop.verify_email", token=raw)),
                  hours=current_app.config["EMAIL_LINK_HOURS_VERIFY"])


def attach_guest_orders(user):
    """Orders placed as a guest with this (now verified) email become part of the account."""
    Order.query.filter(Order.user_id.is_(None), Order.customer_email == user.username).update(
        {Order.user_id: user.id}, synchronize_session=False)
    db.session.commit()


def throttle_email_request(kind, email, limit=4, window=3600):
    key = client_key(f"{kind}:{email}")
    if throttled(key, limit=limit, window=window):
        return True
    record_failure(key)
    return False


@shop_bp.route("/account/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        error = None
        if len(name) < 2:
            error = "Enter your full name."
        elif not EMAIL_RE.match(email):
            error = "Enter a valid email address."
        elif len(password) < 8:
            error = "Choose a password with at least 8 characters."
        elif User.query.filter_by(username=email).first():
            error = "An account with that email already exists. Sign in, or use \"Forgot password\"."
        if error:
            flash(error, "danger")
        else:
            from werkzeug.security import generate_password_hash
            user = User(username=email, password=generate_password_hash(password), role="customer",
                        full_name=name, email_verified=False)
            db.session.add(user)
            db.session.commit()
            send_verification(user)
            return render_template("shop/check_email.html", email=email, kind="verify")
    return render_template("shop/register.html")


@shop_bp.route("/account/verify/<token>")
def verify_email(token):
    row = tokens.find(token, tokens.VERIFY)
    if not row:
        flash("That confirmation link has expired or was already used. Request a new one below.", "warning")
        return redirect(url_for("shop.resend_verification"))
    user = row.user
    user.email_verified = True
    tokens.consume(row)
    attach_guest_orders(user)
    flash("Email confirmed. You can sign in now.", "success")
    return redirect(url_for("shop.account_login"))


@shop_bp.route("/account/verify/resend", methods=["GET", "POST"])
def resend_verification():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if EMAIL_RE.match(email) and not throttle_email_request("verify", email):
            user = User.query.filter_by(username=email, role="customer").first()
            if user and not user.email_verified:
                send_verification(user)
        # same answer whether or not the address exists, so this can't be used to probe for accounts
        return render_template("shop/check_email.html", email=email, kind="verify")
    return render_template("shop/resend.html")


@shop_bp.route("/account/forgot", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if EMAIL_RE.match(email) and not throttle_email_request("reset", email):
            user = User.query.filter_by(username=email, role="customer").first()
            if user:
                raw = tokens.issue(user, tokens.RESET, timedelta(minutes=current_app.config["EMAIL_LINK_MINUTES_RESET"]))
                send_template(user.username, f"Reset your {current_app.config['BUSINESS_NAME']} password", "reset",
                              name=user.full_name, link=abs_url(url_for("shop.reset_password", token=raw)),
                              minutes=current_app.config["EMAIL_LINK_MINUTES_RESET"])
        return render_template("shop/check_email.html", email=email, kind="reset")
    return render_template("shop/forgot.html")


@shop_bp.route("/account/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    row = tokens.find(token, tokens.RESET)
    if not row:
        flash("That reset link has expired or was already used. Request a new one.", "warning")
        return redirect(url_for("shop.forgot_password"))
    if request.method == "POST":
        new, confirm = request.form.get("password", ""), request.form.get("confirm", "")
        if len(new) < 8:
            flash("Choose a password with at least 8 characters.", "danger")
        elif new != confirm:
            flash("The two passwords don't match.", "danger")
        else:
            from werkzeug.security import generate_password_hash
            user = row.user
            user.password = generate_password_hash(new)
            user.email_verified = True     # they just proved they control this inbox
            tokens.consume(row)
            attach_guest_orders(user)
            flash("Password updated. Sign in with your new password.", "success")
            return redirect(url_for("shop.account_login"))
    return render_template("shop/reset.html", token=token)


@shop_bp.route("/account/login", methods=["GET", "POST"])
def account_login():
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        key = client_key(email)
        if throttled(key):
            flash("Too many attempts. Please wait a few minutes and try again.", "danger")
            return render_template("shop/login.html"), 429
        from werkzeug.security import check_password_hash
        user = User.query.filter_by(username=email).first()
        if user and user.role in ("disabled", "system"):
            flash("This account is disabled.", "danger")
        elif user and check_password_hash(user.password, request.form.get("password") or ""):
            if user.role == "customer" and not user.email_verified:
                flash("Please confirm your email first. We sent you a link when you signed up.", "warning")
                return render_template("shop/login.html", unverified_email=email)
            clear_failures(key)
            login_user(user)
            return redirect(safe_next(request.args.get("next"), home_for(user)))
        else:
            record_failure(key)
            flash("That email and password don't match.", "danger")
    return render_template("shop/login.html")


# ---------------- Sign in with Google (customers only) ----------------

def google_enabled():
    c = current_app.config
    return bool(
        c.get("GOOGLE_CLIENT_ID")
        and c.get("GOOGLE_CLIENT_SECRET")
        and c.get("GOOGLE_REDIRECT_URI")
    )


def google_redirect_uri():
    """
    Use exactly the same redirect URI when requesting authorization
    and exchanging the authorization code.
    """
    uri = (current_app.config.get("GOOGLE_REDIRECT_URI") or "").strip()

    if not uri.startswith("https://"):
        raise GoogleAuthError(
            "Google sign-in is not configured correctly. Please try again later."
        )

    return uri.rstrip("/")


@shop_bp.route("/account/google")
def google_start():
    if not google_enabled():
        current_app.logger.error(
            "Google sign-in unavailable: OAuth settings are missing."
        )
        abort(404)

    try:
        state = secrets.token_urlsafe(32)

        session["g_state"] = state
        session["g_next"] = safe_next(request.args.get("next"), "")

        auth_url = build_auth_url(
            current_app.config["GOOGLE_CLIENT_ID"],
            google_redirect_uri(),
            state,
        )

        return redirect(auth_url)

    except GoogleAuthError:
        current_app.logger.exception("Google OAuth configuration error.")
        flash("Google sign-in is temporarily unavailable.", "danger")
        return redirect(url_for("shop.account_login"))


@shop_bp.route("/account/google/callback")
def google_callback():
    if not google_enabled():
        abort(404)

    # Validate OAuth state.
    expected = session.pop("g_state", None)
    received = request.args.get("state", "")

    if (
        not expected
        or not received
        or not hmac.compare_digest(expected.encode(), received.encode())
    ):
        current_app.logger.warning("Google OAuth state validation failed.")
        session.pop("g_next", None)
        flash(
            "Google sign-in could not be verified. Please try again.",
            "warning",
        )
        return redirect(url_for("shop.account_login"))

    # Handle cancellation or denial.
    if request.args.get("error"):
        current_app.logger.info(
            "Google sign-in cancelled or denied: %s",
            request.args.get("error"),
        )
        session.pop("g_next", None)
        flash("Google sign-in was cancelled.", "warning")
        return redirect(url_for("shop.account_login"))

    code = request.args.get("code")

    if not code:
        session.pop("g_next", None)
        flash("Google did not provide an authorization code.", "danger")
        return redirect(url_for("shop.account_login"))

    # Exchange authorization code for Google profile.
    try:
        profile = fetch_profile(
            current_app.config["GOOGLE_CLIENT_ID"],
            current_app.config["GOOGLE_CLIENT_SECRET"],
            google_redirect_uri(),
            code,
        )

    except GoogleAuthError as exc:
        current_app.logger.warning(
            "Google authentication failed: %s", exc
        )
        session.pop("g_next", None)
        flash(str(exc), "danger")
        return redirect(url_for("shop.account_login"))

    # Normalise the email the same way login/registration do.
    email = (profile["email"] or "").strip().lower()

    try:
        # Find an existing account linked to this Google identity.
        user = User.query.filter_by(
            google_sub=profile["sub"]
        ).first()

        if user:
            if user.role in ("disabled", "system"):
                flash("This account is disabled.", "danger")
                return redirect(url_for("shop.account_login"))

            if user.role != "customer":
                flash(
                    "This account cannot use customer Google sign-in.",
                    "danger",
                )
                return redirect(url_for("shop.account_login"))

        else:
            # Check whether this email already has an account.
            user = User.query.filter_by(
                username=email
            ).first()

            if user and user.role in ("disabled", "system"):
                flash("This account is disabled.", "danger")
                return redirect(url_for("shop.account_login"))

            # Never link Google automatically to staff accounts.
            if user and user.role != "customer":
                flash(
                    "That email belongs to a staff account. "
                    "Staff must sign in using their work password.",
                    "danger",
                )
                return redirect(url_for("auth.login"))

            if user:
                # Link Google to the existing customer account.
                if user.google_sub and user.google_sub != profile["sub"]:
                    flash(
                        "This email is already linked to another Google account.",
                        "danger",
                    )
                    return redirect(url_for("shop.account_login"))

                user.google_sub = profile["sub"]
                user.email_verified = True

                if not user.full_name:
                    user.full_name = profile["name"]

            else:
                # Create a new customer account.
                from werkzeug.security import generate_password_hash

                user = User(
                    username=email,
                    password=generate_password_hash(
                        secrets.token_urlsafe(48)
                    ),
                    role="customer",
                    full_name=profile["name"],
                    email_verified=True,
                    google_sub=profile["sub"],
                )

                db.session.add(user)

            db.session.commit()

            # Attach previous guest orders to the verified customer.
            attach_guest_orders(user)

    except Exception:
        db.session.rollback()

        current_app.logger.exception(
            "Google customer account creation/linking failed."
        )

        session.pop("g_next", None)
        flash(
            "We couldn't finish setting up your account. Please try again.",
            "danger",
        )
        return redirect(url_for("shop.account_login"))

    if user.role != "customer":
        flash("This account cannot use customer Google sign-in.", "danger")
        return redirect(url_for("shop.account_login"))

    login_user(user)

    next_url = session.pop("g_next", "")

    return redirect(
        safe_next(next_url, home_for(user))
    )


def customer_required(view):
    """Account pages are for customers; staff who wander in are sent to their own area."""
    from functools import wraps

    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if current_user.role != "customer":
            return redirect(home_for(current_user))
        return view(*args, **kwargs)
    return wrapper


MAX_ADDRESSES = 10


@shop_bp.route("/account")
@customer_required
def account():
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    return render_template("shop/account.html", orders=orders, active="orders")


@shop_bp.route("/account/profile", methods=["GET", "POST"])
@customer_required
def account_profile():
    if request.method == "POST":
        name = " ".join((request.form.get("name") or "").split())
        phone = (request.form.get("phone") or "").strip()
        if len(name) < 2 or len(name) > 100:
            flash("Enter your full name.", "danger")
        elif phone and len(re.sub(r"\D", "", phone)) < 7:
            flash("Enter a phone number we can reach you on, or leave it empty.", "danger")
        elif len(phone) > 30:
            flash("That phone number is too long.", "danger")
        else:
            current_user.full_name = name
            current_user.phone = phone or None
            db.session.commit()
            flash("Your details have been saved.", "success")
            return redirect(url_for("shop.account_profile"))
    return render_template("shop/account_profile.html", active="profile")


@shop_bp.route("/account/password", methods=["POST"])
@customer_required
def account_password():
    from werkzeug.security import check_password_hash, generate_password_hash
    key = client_key(f"pw:{current_user.id}")
    if throttled(key):
        flash("Too many attempts. Please wait a few minutes and try again.", "danger")
        return redirect(url_for("shop.account_profile"))
    current, new, confirm = (request.form.get("current_password") or "", request.form.get("new_password") or "",
                             request.form.get("confirm_password") or "")
    if not check_password_hash(current_user.password, current):
        record_failure(key)
        flash("Your current password isn't right. Signed up with Google? Use \"Forgot your password?\" on the sign-in page to set one.", "danger")
    elif len(new) < 8:
        flash("Choose a new password with at least 8 characters.", "danger")
    elif new != confirm:
        flash("The two new passwords don't match.", "danger")
    else:
        current_user.password = generate_password_hash(new)
        db.session.commit()
        clear_failures(key)
        flash("Password changed.", "success")
    return redirect(url_for("shop.account_profile"))


@shop_bp.route("/account/addresses")
@customer_required
def account_addresses():
    addresses = list(current_user.addresses)
    editing = None
    edit_id = request.args.get("edit", type=int)
    if edit_id:
        editing = next((a for a in addresses if a.id == edit_id), None)
    return render_template("shop/account_addresses.html", addresses=addresses, editing=editing, active="addresses",
                           next_url=safe_next(request.args.get("next"), ""), max_addresses=MAX_ADDRESSES)


@shop_bp.route("/account/addresses/save", methods=["POST"])
@customer_required
def account_address_save():
    next_url = safe_next(request.form.get("next"), "")
    back = url_for("shop.account_addresses", next=next_url or None)
    addresses = list(current_user.addresses)
    address_id = request.form.get("address_id", type=int)
    label = " ".join((request.form.get("label") or "").split())[:40] or "Home"
    line = " ".join((request.form.get("address") or "").split())
    city = " ".join((request.form.get("city") or "").split())[:80]
    make_default = bool(request.form.get("is_default"))

    target = next((a for a in addresses if a.id == address_id), None) if address_id else None
    if address_id and not target:
        abort(404)
    if len(line) < 5:
        flash("Enter the street address, suburb or landmark so the driver can find you.", "danger")
        return redirect(back)
    if len(line) > 220:
        flash("That address is too long. Please shorten it a little.", "danger")
        return redirect(back)
    if not target and len(addresses) >= MAX_ADDRESSES:
        flash(f"You can save up to {MAX_ADDRESSES} addresses. Remove one to add another.", "warning")
        return redirect(back)

    if target is None:
        target = CustomerAddress(user_id=current_user.id)
        db.session.add(target)
        make_default = make_default or not addresses      # the first address is always the default
    target.label, target.address, target.city = label, line, city or None
    if make_default:
        for a in addresses:
            a.is_default = False
        target.is_default = True
    db.session.commit()
    flash("Address saved.", "success")
    return redirect(next_url or url_for("shop.account_addresses"))


@shop_bp.route("/account/addresses/<int:address_id>/default", methods=["POST"])
@customer_required
def account_address_default(address_id):
    addresses = list(current_user.addresses)
    target = next((a for a in addresses if a.id == address_id), None) or abort(404)
    for a in addresses:
        a.is_default = (a.id == target.id)
    db.session.commit()
    flash("Default address updated.", "success")
    return redirect(url_for("shop.account_addresses"))


@shop_bp.route("/account/addresses/<int:address_id>/delete", methods=["POST"])
@customer_required
def account_address_delete(address_id):
    addresses = list(current_user.addresses)
    target = next((a for a in addresses if a.id == address_id), None) or abort(404)
    was_default = target.is_default
    db.session.delete(target)
    db.session.flush()
    if was_default:
        rest = [a for a in addresses if a.id != address_id]
        if rest:
            rest[0].is_default = True
    db.session.commit()
    flash("Address removed.", "info")
    return redirect(url_for("shop.account_addresses"))
