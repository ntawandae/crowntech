"""Public storefront: browse, cart, checkout, payments (Paynow + PayPal), customer accounts."""
import os
import re
import time
from decimal import Decimal
from io import BytesIO

from flask import (Blueprint, abort, current_app, flash, redirect, render_template,
                   request, send_file, session, url_for)
from flask_login import current_user, login_required, login_user

from .. import db
from ..models import Order, Payment, Product, User
from ..security import (clear_failures, client_key, csrf_token, protect_blueprint, record_failure,
                        safe_next, throttled)
from ..services import orders as order_svc
from ..services.orders import OrderError
from ..services.paynow import PaynowClient, PaynowError, is_failed, is_paid, parse_message, verify_pairs
from ..services.paypal import PayPalClient, PayPalError
from ..services.specs import CATEGORY_ORDER, CATEGORY_TONE, extract_specs, normalize_category, short_title, clean_text

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


def image_url(product, size="card"):
    """Uniform 4:3 frame for a product photo (None if the photo file is missing)."""
    if product.image_filename:
        path = os.path.join(current_app.config["UPLOAD_FOLDER"], os.path.basename(product.image_filename))
        if os.path.exists(path):
            return url_for("shop.media", size=size, filename=os.path.basename(product.image_filename))
    return None


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
        "specs": extract_specs(p.name),
        "added": p.date_added,
    }


def visible_products():
    return Product.query.filter(Product.price > 0)


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
    products = {p.id: p for p in Product.query.filter(Product.id.in_(list(cart) or [0])).all()}
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
    rows = [present(p) for p in visible_products().filter(Product.quantity > 0).all()]
    rows.sort(key=lambda r: (category_rank(r["category"]), -(r["added"].timestamp() if r["added"] else 0)))
    featured = [r for r in rows if r["category"] in ("Laptops", "Gaming laptops", "Phones")][:8] or rows[:8]
    counts = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    categories = [(n, c, CATEGORY_TONE.get(n, "steel"))
                  for n, c in sorted(counts.items(), key=lambda kv: category_rank(kv[0]))]
    return render_template("shop/home.html", featured=featured, categories=categories, total=len(rows))


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
    form = {
        "name": getattr(current_user, "full_name", "") if current_user.is_authenticated and current_user.role == "customer" else "",
        "email": current_user.username if current_user.is_authenticated and current_user.role == "customer" else "",
        "phone": "", "address": "", "notes": "",
        "delivery": "pickup", "payment": methods[0][0] if methods else "",
    }

    if request.method == "POST":
        form.update({k: (request.form.get(k) or "").strip() for k in ("name", "email", "phone", "address", "notes")})
        form["delivery"] = request.form.get("delivery", "pickup")
        form["payment"] = request.form.get("payment", "")
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
            if order.payment_method == "pay_at_shop":
                flash("Order placed. We're holding your items for you.", "success")
                return redirect(order_url(order))
            return start_payment(order, order.payment_method)
        for e in errors:
            flash(e, "danger")

    delivery_fee = cfg["DELIVERY_FEE"]
    return render_template("shop/checkout.html", lines=lines, subtotal=subtotal, form=form, methods=methods,
                           delivery_enabled=cfg["DELIVERY_ENABLED"], delivery_fee=delivery_fee)


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
            error = "An account with that email already exists. Try signing in."
        if error:
            flash(error, "danger")
        else:
            from werkzeug.security import generate_password_hash
            user = User(username=email, password=generate_password_hash(password), role="customer", full_name=name)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash("Welcome to Crown Tech Hub. Your account is ready.", "success")
            return redirect(safe_next(request.args.get("next"), url_for("shop.account")))
    return render_template("shop/register.html")


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
            clear_failures(key)
            login_user(user)
            return redirect(safe_next(request.args.get("next"), home_for(user)))
        else:
            record_failure(key)
            flash("That email and password don't match.", "danger")
    return render_template("shop/login.html")


@shop_bp.route("/account")
@login_required
def account():
    if current_user.role != "customer":
        return redirect(home_for(current_user))
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    return render_template("shop/account.html", orders=orders)
