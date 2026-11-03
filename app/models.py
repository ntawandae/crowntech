from flask_login import UserMixin
from datetime import datetime
from . import db, login_manager
from .utils.timeutil import zim_now  # sale/expense/request times are stored in Zimbabwe local time

class User(UserMixin, db.Model):
    __tablename__ = "user"  # make sure table name matches the FK
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    full_name = db.Column(db.String(100))  # your new column
    email_verified = db.Column(db.Boolean, default=True)           # customers start False until they click the emailed link
    must_change_password = db.Column(db.Boolean, default=False)    # staff must pick their own password after an admin sets/resets it
    google_sub = db.Column(db.String(64), nullable=True, index=True)  # Google's id for the linked Google account

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    brand = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(100))
    quantity = db.Column(db.Integer, default=0)
    price = db.Column(db.Float, nullable=False)         # selling price
    cost_price = db.Column(db.Float, nullable=False)    # purchase price
    image_filename = db.Column(db.String(255))
    date_added = db.Column(db.DateTime, default=datetime.utcnow)

    def profit_per_item(self):
        return round(self.price - self.cost_price, 2)

class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Integer)
    total_price = db.Column(db.Float)  # renamed from `total` to `total_price` to match usage
    profit = db.Column(db.Float)       # added profit field to use in queries
    timestamp = db.Column(db.DateTime, default=zim_now)
    cashier_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    receipt_id = db.Column(db.Integer, db.ForeignKey('receipt.id'), nullable=True)  # Link to Receipt
    product = db.relationship('Product', backref='sales')
    cashier = db.relationship('User', backref='sales')

class Receipt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    receipt_number = db.Column(db.String(20), unique=True, nullable=False)
    subtotal = db.Column(db.Float, nullable=False)
    tax = db.Column(db.Float, nullable=False)
    total = db.Column(db.Float, nullable=False)
    customer_name = db.Column(db.String(100), nullable=True)
    customer_email = db.Column(db.String(120), nullable=True)
    customer_phone = db.Column(db.String(20), nullable=True)
    timestamp = db.Column(db.DateTime, default=zim_now)
    cashier_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    discount = db.Column(db.Float, default=0.0)          # money taken off the subtotal
    delivery_fee = db.Column(db.Float, default=0.0)      # delivery charged on this sale (total includes it)
    delivery_address = db.Column(db.String(300), nullable=True)
    sales = db.relationship('Sale', backref='receipt', lazy=True)

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=zim_now)
    recorded_by = db.Column(db.Integer, db.ForeignKey('user.id'))

class StockRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    action = db.Column(db.String(20), nullable=False, default='add')  # 'add' or 'deduct'
    quantity = db.Column(db.Integer)
    reason = db.Column(db.String(255))
    status = db.Column(db.String(20), default='pending')  # pending/approved/rejected
    requested_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    timestamp = db.Column(db.DateTime, default=zim_now)

    product = db.relationship('Product', backref='stock_requests')

class DefectRequests(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
    quantity = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255))
    status = db.Column(db.String(50), default='Pending')  # Pending / Approved / Rejected
    timestamp = db.Column(db.DateTime, default=zim_now)
    approved_at = db.Column(db.DateTime, nullable=True)
    rejected_at = db.Column(db.DateTime, nullable=True)
    returned_at = db.Column(db.DateTime, nullable=True)
    submitted_by = db.Column(db.Integer, db.ForeignKey('user.id'))

    product = db.relationship('Product', backref='defect_requests')
    submitted_user = db.relationship('User', backref='defects')



class Quotation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    quotation_number = db.Column(db.String(20), unique=True, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=zim_now)
    items = db.relationship('QuotationItem', backref='quotation', cascade='all, delete-orphan')
    discount = db.Column(db.Float, default=0)   # <---- Add this line
    customer_name = db.Column(db.String(100))  # Optional customer info
    created_by = db.relationship('User')
    converted = db.Column(db.Boolean, default=False)
    @property
    def total_price(self):
        return sum(item.total_price for item in self.items)

class QuotationItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    quotation_id = db.Column(db.Integer, db.ForeignKey('quotation.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=True)  # nullable because admin can add free-text
    product_name = db.Column(db.String(150))  # store product name here for non-product items
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    total_price = db.Column(db.Float, nullable=False)

    product = db.relationship('Product')

class ExtraIncome(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    recorded_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=zim_now)

class DailyCashBalance(db.Model):
    __tablename__ = 'daily_cash_balance'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, unique=True)
    opening_balance = db.Column(db.Float, nullable=False, default=0.0)
    closing_balance = db.Column(db.Float, nullable=False, default=0.0)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f'<DailyCashBalance {self.date}: Opening={self.opening_balance}, Closing={self.closing_balance}>'

class SaleReversal(db.Model):
    __tablename__ = 'sale_reversal'
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id', ondelete='SET NULL'))
    product_id = db.Column(db.Integer, db.ForeignKey('product.id', ondelete='SET NULL'))
    quantity = db.Column(db.Integer, nullable=False)
    amount_reversed = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(255))
    reversed_by = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='SET NULL'))
    timestamp = db.Column(db.DateTime, default=zim_now)

    sale = db.relationship('Sale', backref='reversal', lazy=True)
    product = db.relationship('Product', lazy=True)
class UserProfile(db.Model):
    __tablename__ = "user_profile"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)

    dob = db.Column(db.Date, nullable=True)
    id_number = db.Column(db.String(50), nullable=True)
    address = db.Column(db.String(200), nullable=True)
    sex = db.Column(db.String(10), nullable=True)
    phone_number = db.Column(db.String(20), nullable=True)
    qualifications = db.Column(db.String(200), nullable=True)
    next_of_kin = db.Column(db.String(100), nullable=True)
    next_of_kin_contact = db.Column(db.String(20), nullable=True)
    next_of_kin_address = db.Column(db.String(200), nullable=True)
    contract_file = db.Column(db.String(200), nullable=True)  # path to uploaded signed contract

    # Track if the profile is editable by the user
    editable = db.Column(db.Boolean, default=True)

    # Relationship to User
    user = db.relationship("User", backref=db.backref("profile", uselist=False))




# ======================================================================
# ONLINE ORDERS (customer storefront)
# New tables only - existing tables and data are untouched.
# ======================================================================
class Order(db.Model):
    """An order placed through the online shop."""
    __tablename__ = "shop_order"

    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(20), unique=True, nullable=False, index=True)
    access_token = db.Column(db.String(64), nullable=False)  # lets guests open their order page
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)  # null = guest checkout

    customer_name = db.Column(db.String(120), nullable=False)
    customer_email = db.Column(db.String(120), nullable=False)
    customer_phone = db.Column(db.String(30), nullable=False)
    delivery_method = db.Column(db.String(20), nullable=False, default="pickup")  # pickup / delivery
    delivery_address = db.Column(db.String(300))
    notes = db.Column(db.String(500))

    subtotal = db.Column(db.Float, nullable=False)
    delivery_fee = db.Column(db.Float, nullable=False, default=0.0)
    total = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="USD")

    # pending_payment -> paid -> ready -> completed   |  reserved (pay at shop)  |  cancelled
    status = db.Column(db.String(20), nullable=False, default="pending_payment", index=True)
    payment_status = db.Column(db.String(20), nullable=False, default="unpaid")  # unpaid / paid / failed
    payment_method = db.Column(db.String(20))  # paynow / paypal / pay_at_shop / manual
    stock_held = db.Column(db.Boolean, nullable=False, default=True)  # stock currently reserved for this order

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime)  # unpaid orders release their stock after this
    paid_at = db.Column(db.DateTime)
    receipt_id = db.Column(db.Integer, db.ForeignKey("receipt.id"), nullable=True)
    staff_note = db.Column(db.String(500))

    items = db.relationship("OrderItem", backref="order", cascade="all, delete-orphan")
    payments = db.relationship("Payment", backref="order", cascade="all, delete-orphan",
                               order_by="Payment.id")
    customer = db.relationship("User", foreign_keys=[user_id], backref="orders")
    receipt = db.relationship("Receipt", foreign_keys=[receipt_id])

    STATUS_LABELS = {
        "pending_payment": "Waiting for payment",
        "reserved": "Reserved - pay at shop",
        "paid": "Paid - being prepared",
        "ready": "Ready",
        "completed": "Completed",
        "cancelled": "Cancelled",
    }

    @property
    def status_label(self):
        return self.STATUS_LABELS.get(self.status, self.status)

    @property
    def is_open(self):
        return self.status in ("pending_payment", "reserved", "paid", "ready")


class OrderItem(db.Model):
    __tablename__ = "shop_order_item"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("shop_order.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=True)
    product_name = db.Column(db.String(300), nullable=False)  # snapshot at time of order
    unit_price = db.Column(db.Float, nullable=False)           # snapshot at time of order
    quantity = db.Column(db.Integer, nullable=False)

    product = db.relationship("Product")

    @property
    def line_total(self):
        return round(self.unit_price * self.quantity, 2)


class Payment(db.Model):
    """One payment attempt for an order (an order can have several attempts)."""
    __tablename__ = "shop_payment"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("shop_order.id"), nullable=False)
    provider = db.Column(db.String(20), nullable=False)  # paynow / paypal / manual
    provider_ref = db.Column(db.String(120))   # Paynow reference or PayPal order id
    poll_url = db.Column(db.String(500))       # Paynow status URL
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    status = db.Column(db.String(30), nullable=False, default="initiated")  # initiated / paid / failed / cancelled
    detail = db.Column(db.String(500))         # last status text from the provider (for staff)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AuthToken(db.Model):
    """One-time links sent by email (verify address / reset password). Only a hash is stored."""
    __tablename__ = "auth_token"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    purpose = db.Column(db.String(20), nullable=False)   # verify_email / reset_password
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)

    user = db.relationship("User", foreign_keys=[user_id])
