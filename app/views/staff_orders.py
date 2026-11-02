"""Staff (admin + cashier) order management for online orders."""
from functools import wraps

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import db
from ..models import Order
from ..security import protect_blueprint
from ..services import orders as order_svc
from ..services.orders import OrderError
from ..services.paynow import PaynowError

staff_orders_bp = Blueprint("staff_orders", __name__)
protect_blueprint(staff_orders_bp)

PER_PAGE = 25
FILTERS = [
    ("action", "Needs action"),
    ("pending_payment", "Waiting for payment"),
    ("reserved", "Pay at shop"),
    ("paid", "Paid"),
    ("ready", "Ready"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
    ("all", "All"),
]


def staff_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if current_user.role not in ("admin", "cashier"):
            abort(403)
        return f(*args, **kwargs)
    return wrapper


@staff_orders_bp.route("/")
@staff_required
def order_list():
    order_svc.release_expired_orders()
    flt = request.args.get("filter", "action")
    q = (request.args.get("q") or "").strip()
    page = max(1, request.args.get("page", 1, type=int))

    query = Order.query
    if flt == "action":
        query = query.filter(Order.status.in_(["paid", "reserved"]))
    elif flt != "all":
        query = query.filter(Order.status == flt)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Order.order_number.ilike(like), Order.customer_name.ilike(like),
                                    Order.customer_email.ilike(like), Order.customer_phone.ilike(like)))
    pagination = query.order_by(Order.created_at.desc()).paginate(page=page, per_page=PER_PAGE, error_out=False)

    counts = dict(db.session.query(Order.status, db.func.count(Order.id)).group_by(Order.status).all())
    counts["action"] = counts.get("paid", 0) + counts.get("reserved", 0)
    return render_template("staff/orders.html", pagination=pagination, filters=FILTERS, flt=flt, q=q, counts=counts)


@staff_orders_bp.route("/<int:order_id>")
@staff_required
def order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template("staff/order_detail.html", order=order)


@staff_orders_bp.route("/<int:order_id>/action", methods=["POST"])
@staff_required
def order_action(order_id):
    order = Order.query.get_or_404(order_id)
    action = request.form.get("action", "")
    who = current_user.full_name or current_user.username
    try:
        if action == "mark_paid":
            if order.payment_status == "paid":
                raise OrderError("This order is already paid.")
            method = request.form.get("method", "pay_at_shop")
            if method not in ("pay_at_shop", "manual"):
                method = "manual"
            order_svc.mark_paid(order, method, note=f"Marked paid by {who}")
            flash("Payment recorded and added to sales.", "success")
        elif action == "ready":
            if order.payment_status != "paid":
                raise OrderError("Record the payment before marking the order ready.")
            order.status = "ready"
            db.session.commit()
            flash("Order marked ready.", "success")
        elif action == "complete":
            if order.payment_status != "paid":
                raise OrderError("Record the payment before completing the order.")
            order.status = "completed"
            db.session.commit()
            flash("Order completed.", "success")
        elif action == "cancel":
            reason = (request.form.get("reason") or "Cancelled by staff").strip()
            if order.payment_status == "paid":
                if request.form.get("confirm_refund") != "yes":
                    raise OrderError("Tick the box to confirm you have refunded the customer.")
                order_svc.cancel_paid_order(order, f"{reason} ({who})")
            else:
                order_svc.cancel_order(order, f"{reason} ({who})")
            flash("Order cancelled and stock returned.", "info")
        elif action == "note":
            order.staff_note = (request.form.get("note") or "").strip()[:500] or None
            db.session.commit()
            flash("Note saved.", "success")
        elif action == "recheck":
            from .shop import _refresh_paynow
            _refresh_paynow(order)
            flash("Checked with Paynow." if order.payment_status != "paid" else "Payment confirmed.", "info")
        else:
            abort(400)
    except (OrderError, PaynowError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("staff_orders.order_detail", order_id=order.id))
