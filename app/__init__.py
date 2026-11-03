import os
from datetime import datetime, timedelta

from flask import Flask, render_template
from flask_login import LoginManager, current_user
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"

STAFF_ROLES = ("admin", "cashier")


def create_app(config_object=None):
    if config_object is None:
        from config import Config as config_object

    app = Flask(__name__)
    app.config.from_object(config_object)
    app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "static", "uploads")

    if os.environ.get("BEHIND_PROXY", "").lower() in ("1", "true", "yes"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    login_manager.init_app(app)

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        user = db.session.get(User, int(user_id))
        # Disabled staff and the internal system account lose access immediately,
        # even if they still have an open session.
        if user and user.role in ("disabled", "system"):
            return None
        return user

    from .views.auth import auth_bp
    from .views.admin import admin_bp
    from .views.cashier import cashier_bp
    from .views.shop import shop_bp
    from .views.staff_orders import staff_orders_bp

    app.register_blueprint(shop_bp)                                   # public storefront at /
    app.register_blueprint(auth_bp, url_prefix="/auth")               # staff + shared login
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(cashier_bp, url_prefix="/cashier")
    app.register_blueprint(staff_orders_bp, url_prefix="/staff/orders")

    from .security import csrf_token

    @app.template_filter("money")
    def money(value):
        try:
            return f"{app.config['SHOP_CURRENCY_SYMBOL']}{float(value):,.2f}"
        except (TypeError, ValueError):
            return value

    @app.template_filter("when")
    def when(value, fmt="%d %b %Y, %H:%M"):
        """Show a stored UTC time in the shop's local time."""
        if not value:
            return ""
        return (value + timedelta(hours=app.config["SHOP_UTC_OFFSET_HOURS"])).strftime(fmt)

    @app.context_processor
    def inject_globals():
        from flask import session
        cart = session.get("shop_cart") or {}
        pending = 0
        if current_user.is_authenticated and current_user.role in STAFF_ROLES:
            from .models import Order
            pending = Order.query.filter(Order.status.in_(["paid", "reserved"])).count()
        return {
            "csrf_token": csrf_token,
            "biz": {
                "name": app.config["BUSINESS_NAME"],
                "address": app.config["BUSINESS_ADDRESS"],
                "phone": app.config["BUSINESS_PHONE"],
                "email": app.config["BUSINESS_EMAIL"],
                "whatsapp": app.config["BUSINESS_WHATSAPP"],
            },
            "cart_count": sum(int(q) for q in cart.values()),
            "google_on": bool(app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"]),
            "staff_pending_orders": pending,
            "current_year": datetime.utcnow().year,
        }

    @app.before_request
    def force_password_change():
        """Staff whose password was set or reset by an admin must choose their own before doing anything else."""
        from flask import redirect, request, url_for
        if (current_user.is_authenticated and getattr(current_user, "must_change_password", False)
                and request.endpoint not in ("auth.change_password_first", "auth.logout", "static")
                and not request.path.startswith("/static/")):
            return redirect(url_for("auth.change_password_first"))

    @app.before_request
    def block_public_contracts():
        from flask import abort, request
        if request.path.startswith("/static/uploads/contracts"):
            abort(404)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return resp

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def friendly_error(err):
        code = getattr(err, "code", 500)
        messages = {
            400: (getattr(err, "description", None) or "That request didn't look right."),
            403: "You don't have access to that page.",
            404: "We couldn't find that page.",
        }
        return render_template("shop/error.html", code=code, message=messages.get(code, "Something went wrong.")), code

    with app.app_context():
        db.create_all()  # adds new tables; existing tables and data are untouched
        from .migrate import upgrade_schema
        upgrade_schema(db)  # adds new columns (e.g. receipt.discount / delivery_fee) if missing
        from .migrate import move_legacy_contracts
        move_legacy_contracts(app, db)  # contracts are private now

    return app
