# -*- coding: utf-8 -*-
from flask import Blueprint, render_template, request, redirect, send_file, flash, url_for, abort, session, jsonify
from flask_login import current_user, login_required
from functools import wraps
import json
import logging
from datetime import datetime, timedelta, timezone
from io import BytesIO
from xhtml2pdf import pisa
from ..models import Product, Sale, db, StockRequest, DefectRequests, Expense, QuotationItem, Quotation, Receipt, ExtraIncome, DailyCashBalance
from sqlalchemy import func
import hashlib

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Define Blueprint
cashier_bp = Blueprint('cashier', __name__, url_prefix='/cashier')

# Custom decorator for cashier-only access
def cashier_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.role != 'cashier':
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

# ----------------------------------------------------------------------
# TIMEZONE HELPER – Zimbabwe (UTC+2, no DST)
# ----------------------------------------------------------------------
ZIM_OFFSET = timedelta(hours=2)

def get_zim_now():
    """Current time in Zimbabwe local time (UTC+2)"""
    return datetime.now(timezone.utc) + ZIM_OFFSET

def get_zim_date_now():
    """Current Zimbabwe business date"""
    return get_zim_now().date()

def get_utc_range_for_zim_date(zim_date):
    """
    Returns the UTC datetime range covering 00:00:00 to 23:59:59.999999
    Zimbabwe local time for the given business date.
    """
    start_local = datetime.combine(zim_date, datetime.min.time())
    end_local   = datetime.combine(zim_date, datetime.max.time())
    start_utc   = start_local - ZIM_OFFSET
    end_utc     = end_local   - ZIM_OFFSET
    return start_utc, end_utc

# Helper function to update daily cash balance
def update_cash_balance(sales_amount=0.0, income_amount=0.0, expense_amount=0.0):
    try:
        zim_date = get_zim_date_now()
        cash_balance = DailyCashBalance.query.filter_by(date=zim_date).first()
        if not cash_balance:
            previous_day = zim_date - timedelta(days=1)
            previous_balance = DailyCashBalance.query.filter_by(date=previous_day).first()
            opening_balance = previous_balance.closing_balance if previous_balance else 0.0
            cash_balance = DailyCashBalance(
                date=zim_date,
                opening_balance=opening_balance,
                closing_balance=opening_balance
            )
            db.session.add(cash_balance)

        net_cash_flow = sales_amount + income_amount - expense_amount
        cash_balance.closing_balance = cash_balance.opening_balance + net_cash_flow
        cash_balance.updated_at = datetime.utcnow()  # UTC for audit
        db.session.commit()
        logger.debug(f"Updated Zim balance {zim_date}: Net={net_cash_flow}, Closing={cash_balance.closing_balance}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating cash balance: {str(e)}")


# ----------------------------------------------------------------------
# DASHBOARD – Most sold products first
# ----------------------------------------------------------------------
@cashier_bp.route('/dashboard', methods=['GET'])
@login_required
@cashier_required
def cashier_dashboard():
    sales_subquery = (
        db.session.query(
            Sale.product_id,
            func.coalesce(func.sum(Sale.quantity), 0).label('total_sold')
        )
        .group_by(Sale.product_id)
        .subquery()
    )
    products = (
        Product.query
        .outerjoin(sales_subquery, Product.id == sales_subquery.c.product_id)
        .add_columns(func.coalesce(sales_subquery.c.total_sold, 0).label('total_sold'))
        .order_by(func.coalesce(sales_subquery.c.total_sold, 0).desc(), Product.name.asc())
        .all()
    )
    products = [p[0] for p in products]
    cart = session.get('cart', [])
    return render_template('cashier_dashboard.html', products=products, cart=cart)
# ----------------------------------------------------------------------
# INVENTORY
# ----------------------------------------------------------------------
@cashier_bp.route('/inventory')
@login_required
@cashier_required
def inventory():
    products = Product.query.all()
    logger.debug(f"Rendering inventory page for user {current_user.id}, products found: {len(products)}")
    return render_template('inventory.html', products=products)

# ----------------------------------------------------------------------
# CART OPERATIONS
# ----------------------------------------------------------------------
@cashier_bp.route('/add_one/<int:product_id>', methods=['POST'])
@login_required
@cashier_required
def add_one_to_cart(product_id):
    product = Product.query.get(product_id)
    if not product:
        flash("Product not found.", "danger")
        return redirect(url_for('cashier.cashier_dashboard'))
    if product.quantity < 1:
        flash(f"{product.name} is out of stock.", "warning")
        return redirect(url_for('cashier.cashier_dashboard'))

    if 'cart' not in session:
        session['cart'] = []

    cart = session['cart']
    found = False
    for item in cart:
        if item['id'] == product_id:
            if item['quantity'] < product.quantity:
                item['quantity'] += 1
                flash(f"Added one more {product.name} to cart.", "success")
            else:
                flash(f"Cannot add more than available stock for {product.name}.", "warning")
            found = True
            break

    if not found:
        cart.append({
            'id': product.id,
            'name': product.name,
            'price': product.price,
            'quantity': 1
        })
        flash(f"Added {product.name} to cart.", "success")

    session['cart'] = cart
    session.modified = True
    return redirect(url_for('cashier.cashier_dashboard'))
# ----------------------------------------------------------------------
# SELL + PRINT RECEIPT (original working version + Zim time)
# ----------------------------------------------------------------------
@cashier_bp.route('/sell_and_receipt', methods=['POST'])
@login_required
@cashier_required
def sell_and_receipt():
    try:
        logger.debug(f"Form data received: {dict(request.form)}")
        logger.debug(f"Session cart: {session.get('cart', [])}")
        logger.debug(f"Session discount: {session.get('discount', 0)}")
        cart = session.get('cart', [])
        if not cart:
            logger.warning("Cart is empty in sell_and_receipt")
            flash("Cart is empty.", "warning")
            return redirect(url_for('cashier.cashier_dashboard'))

        sold_items = []
        subtotal = 0
        for item in cart:
            if not isinstance(item, dict) or 'id' not in item or not isinstance(item['id'], int):
                logger.error(f"Invalid cart item: {item}")
                flash(f"Invalid cart item: {item.get('name', 'unknown')}.", "danger")
                return redirect(url_for('cashier.cashier_dashboard'))
            product_id = item['id']
            product = Product.query.get(product_id)
            if not product:
                logger.error(f"Product not found for ID: {product_id}")
                flash(f"Product not found: {item.get('name', 'unknown')}.", "danger")
                return redirect(url_for('cashier.cashier_dashboard'))
            if product.quantity < item['quantity']:
                logger.error(f"Insufficient stock for product ID {product_id}: {product.name}")
                flash(f"Not enough stock for {item['name']}. Available: {product.quantity}.", "danger")
                return redirect(url_for('cashier.cashier_dashboard'))
            item_total = item['price'] * item['quantity']
            subtotal += item_total
            sold_items.append({
                'id': product_id,
                'name': item['name'],
                'price': item['price'],
                'quantity': item['quantity'],
                'original_total_price': item_total,
                'total_price': item_total
            })

        discount_amount = float(session.get('discount', request.form.get('discount_sell_receipt', 0)) or 0.0)
        if discount_amount < 0:
            flash("Discount cannot be negative.", "danger")
            return redirect(url_for('cashier.cashier_dashboard'))
        if discount_amount > subtotal:
            discount_amount = subtotal

        session['discount'] = discount_amount
        session.modified = True

        final_total = subtotal - discount_amount
        tax_rate = 0.0
        tax = final_total * tax_rate
        total = final_total + tax

        zim_now = get_zim_now()
        today_str = zim_now.strftime('%Y%m%d')
        prefix = f"SALE-{today_str}-"

        last_receipt = Receipt.query.filter(Receipt.receipt_number.like(f"{prefix}%")).order_by(Receipt.receipt_number.desc()).first()
        if last_receipt:
            last_number = int(last_receipt.receipt_number.split('-')[-1])
            receipt_number = f"{prefix}{last_number + 1:04d}"
        else:
            receipt_number = f"{prefix}0001"

        max_retries = 5
        retry_count = 0
        while Receipt.query.filter_by(receipt_number=receipt_number).first() and retry_count < max_retries:
            retry_count += 1
            receipt_number = f"{prefix}{int(receipt_number.split('-')[-1]) + 1:04d}"

        if retry_count >= max_retries:
            flash("Unable to generate a unique receipt number.", "danger")
            return redirect(url_for('cashier.cashier_dashboard'))

        receipt = Receipt(
            receipt_number=receipt_number,
            subtotal=subtotal,
            tax=tax,
            total=total,
            customer_name=request.form.get('customer_name', ''),
            customer_email=request.form.get('customer_email', ''),
            customer_phone=request.form.get('customer_phone', ''),
            cashier_id=current_user.id,
            timestamp=zim_now  # Zim time
        )
        db.session.add(receipt)
        db.session.flush()

        for item in sold_items:
            product = Product.query.get(item['id'])
            if not product:
                raise ValueError(f"Product not found for ID {item['id']}")
            product.quantity -= item['quantity']
            item_total = item['original_total_price']
            item_discount = (item_total / subtotal) * discount_amount if subtotal > 0 else 0
            item['total_price'] = item_total - item_discount
            selling_price_per_unit = item['price']
            cost_price = float(product.cost_price or 0.0)
            profit = (selling_price_per_unit - cost_price) * item['quantity']
            sale = Sale(
                product_id=product.id,
                quantity=item['quantity'],
                total_price=item['total_price'],
                profit=profit,
                cashier_id=current_user.id,
                timestamp=zim_now,
                receipt_id=receipt.id
            )
            db.session.add(sale)

        db.session.commit()
        update_cash_balance(sales_amount=total)  # already uses Zim date

        # Generate PDF
        html = render_template(
            'receipt_template.html',
            items=sold_items,
            subtotal=subtotal,
            discount=discount_amount,
            tax=tax,
            total=total,
            receipt_number=receipt.receipt_number,
            customer_name=receipt.customer_name,
            customer_email=receipt.customer_email,
            customer_phone=receipt.customer_phone,
            printed_by=current_user.full_name,
            business_name='Crown Tech Hub',
            business_address='Karimapondo Mall, Corner Robert Mugabe & Leopold Takawira, 1st Floor, Workstation 25, Harare, Zimbabwe',
            logo_path=url_for('static', filename='images/cthlogo.jpg', _external=True),
            current_year=datetime.utcnow().year,
            timestamp=receipt.timestamp
        )
        buffer = BytesIO()
        pisa_status = pisa.CreatePDF(html, dest=buffer)
        if pisa_status.err:
            flash("Failed to generate receipt PDF.", "danger")
            return redirect(url_for('cashier.cashier_dashboard'))
        buffer.seek(0)

        session.pop('cart', None)
        session.pop('discount', None)
        session.modified = True

        flash("Sale completed and receipt generated.", "success")
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"receipt_{receipt.receipt_number}.pdf",
            mimetype='application/pdf'
        )

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error processing sale: {str(e)}")
        flash(f"Error processing sale: {str(e)}", "danger")
        return redirect(url_for('cashier.cashier_dashboard'))


# ----------------------------------------------------------------------
# SELL ONLY (no receipt PDF) – original working version + Zim time
# ----------------------------------------------------------------------
@cashier_bp.route('/sell', methods=['POST'])
@login_required
@cashier_required
def sell_items():
    try:
        cart = session.get('cart', [])
        if not cart:
            flash("Cart is empty.", "warning")
            return redirect(url_for('cashier.cashier_dashboard'))

        subtotal = 0
        sold_items = []
        for item in cart:
            if 'id' not in item:
                flash(f"Invalid cart item: {item.get('name', 'unknown')}.", "danger")
                return redirect(url_for('cashier.cashier_dashboard'))
            product = Product.query.get(item['id'])
            if not product:
                flash(f"Product not found: {item.get('name', 'unknown')}.", "danger")
                return redirect(url_for('cashier.cashier_dashboard'))
            if product.quantity < item['quantity']:
                flash(f"Not enough stock for {item['name']}. Available: {product.quantity}.", "danger")
                return redirect(url_for('cashier.cashier_dashboard'))
            item_total = item['price'] * item['quantity']
            subtotal += item_total
            sold_items.append({
                'id': item['id'],
                'name': item['name'],
                'price': item['price'],
                'quantity': item['quantity'],
                'original_total_price': item_total,
                'total_price': item_total
            })

        discount_amount = float(request.form.get('discount', 0) or 0.0)
        if discount_amount < 0:
            flash("Discount cannot be negative.", "danger")
            return redirect(url_for('cashier.cashier_dashboard'))
        if discount_amount > subtotal:
            discount_amount = subtotal

        final_total = subtotal - discount_amount
        tax_rate = 0.0
        tax = final_total * tax_rate
        total = final_total + tax

        zim_now = get_zim_now()
        today_str = zim_now.strftime('%Y%m%d')
        prefix = f"SALE-{today_str}-"

        last_receipt = Receipt.query.filter(Receipt.receipt_number.like(f"{prefix}%")).order_by(Receipt.receipt_number.desc()).first()
        if last_receipt:
            last_number = int(last_receipt.receipt_number.split('-')[-1])
            receipt_number = f"{prefix}{last_number + 1:04d}"
        else:
            receipt_number = f"{prefix}0001"

        max_retries = 5
        retry_count = 0
        while Receipt.query.filter_by(receipt_number=receipt_number).first() and retry_count < max_retries:
            retry_count += 1
            receipt_number = f"{prefix}{int(receipt_number.split('-')[-1]) + 1:04d}"

        if retry_count >= max_retries:
            flash("Unable to generate a unique receipt number.", "danger")
            return redirect(url_for('cashier.cashier_dashboard'))

        receipt = Receipt(
            receipt_number=receipt_number,
            subtotal=subtotal,
            tax=tax,
            total=total,
            customer_name=request.form.get('customer_name', ''),
            customer_email=request.form.get('customer_email', ''),
            customer_phone=request.form.get('customer_phone', ''),
            cashier_id=current_user.id,
            timestamp=zim_now
        )
        db.session.add(receipt)
        db.session.flush()

        for item in sold_items:
            product = Product.query.get(item['id'])
            product.quantity -= item['quantity']
            item_total = item['original_total_price']
            item_discount = (item_total / subtotal) * discount_amount if subtotal > 0 else 0
            item['total_price'] = item_total - item_discount
            selling_price_per_unit = item['price']
            cost_price = float(product.cost_price or 0.0)
            profit = (selling_price_per_unit - cost_price) * item['quantity']
            sale = Sale(
                product_id=product.id,
                quantity=item['quantity'],
                total_price=item['total_price'],
                profit=profit,
                cashier_id=current_user.id,
                timestamp=zim_now,
                receipt_id=receipt.id
            )
            db.session.add(sale)

        db.session.commit()
        update_cash_balance(sales_amount=total)

        session.pop('cart', None)
        session.pop('discount', None)
        session.modified = True

        flash(f"Sale completed. Receipt #{receipt.receipt_number} recorded.", "success")
        return redirect(url_for('cashier.cashier_dashboard'))

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error processing sale: {str(e)}")
        flash(f"Error processing sale: {str(e)}", "danger")
        return redirect(url_for('cashier.cashier_dashboard'))
    
@cashier_bp.route('/quotations/<int:quotation_id>/convert', methods=['POST'])
@login_required
@cashier_required
def convert_quotation(quotation_id):
    try:
        quotation = Quotation.query.get_or_404(quotation_id)
        if quotation.created_by_id != current_user.id:
            abort(403)
        if quotation.converted:
            flash("This quotation has already been converted to a sale.", "warning")
            return redirect(url_for('cashier.quotations'))

        for q_item in quotation.items:
            product = Product.query.get(q_item.product_id)
            if not product:
                flash(f"Product '{q_item.product_name}' no longer exists.", "danger")
                return redirect(url_for('cashier.quotations'))
            if product.quantity < q_item.quantity:
                flash(f"Not enough stock for '{product.name}'. Requested: {q_item.quantity}, Available: {product.quantity}.", "danger")
                return redirect(url_for('cashier.quotations'))

        sold_items = []
        subtotal = 0
        for q_item in quotation.items:
            item_total = q_item.unit_price * q_item.quantity
            subtotal += item_total
            sold_items.append({
                'id': q_item.product_id,
                'name': q_item.product_name,
                'price': q_item.unit_price,
                'quantity': q_item.quantity,
                'original_total_price': item_total,
                'total_price': item_total
            })

        discount_amount = float(quotation.discount or 0.0)
        logger.debug(f"Discount amount: {discount_amount}")
        if discount_amount < 0:
            flash("Discount cannot be negative.", "danger")
            return redirect(url_for('cashier.quotations'))
        if discount_amount > subtotal:
            discount_amount = subtotal

        final_total = subtotal - discount_amount
        tax_rate = 0.0
        tax = final_total * tax_rate
        total = final_total + tax

        today = datetime.utcnow().strftime('%Y%m%d')
        prefix = f"SALE-{today}-"
        last_receipt = Receipt.query.filter(Receipt.receipt_number.like(f"{prefix}%")).order_by(Receipt.receipt_number.desc()).first()
        if last_receipt:
            last_number = int(last_receipt.receipt_number.split('-')[-1])
            receipt_number = f"{prefix}{last_number + 1:04d}"
        else:
            receipt_number = f"{prefix}0001"

        max_retries = 5
        retry_count = 0
        while Receipt.query.filter_by(receipt_number=receipt_number).first() and retry_count < max_retries:
            retry_count += 1
            last_number = int(receipt_number.split('-')[-1]) + 1
            receipt_number = f"{prefix}{last_number:04d}"
            logger.debug(f"Retrying with receipt_number: {receipt_number}")

        if retry_count >= max_retries:
            logger.error("Failed to generate unique receipt_number")
            flash("Unable to generate a unique receipt number.", "danger")
            return redirect(url_for('cashier.quotations'))

        receipt = Receipt(
            receipt_number=receipt_number,
            subtotal=subtotal,
            tax=tax,
            total=total,
            customer_name=quotation.customer_name,
            customer_email=request.form.get('customer_email', ''),
            customer_phone=request.form.get('customer_phone', ''),
            cashier_id=current_user.id,
            timestamp=datetime.utcnow()
        )
        db.session.add(receipt)
        db.session.flush()

        # === PROFIT LOOP FOR QUOTATION CONVERSION ===
        for item in sold_items:
            product = Product.query.get(item['id'])
            product.quantity -= item['quantity']
            item_total = item['original_total_price']
            item_discount = (item_total / subtotal) * discount_amount if subtotal > 0 else 0
            item['total_price'] = item_total - item_discount

            # === PROFIT ===
            selling_price_per_unit = item['price']
            cost_price = float(product.cost_price or 0.0)
            profit = (selling_price_per_unit - cost_price) * item['quantity']

            sale = Sale(
                product_id=product.id,
                quantity=item['quantity'],
                total_price=item['total_price'],
                profit=profit,
                cashier_id=current_user.id,
                timestamp=datetime.utcnow(),
                receipt_id=receipt.id
            )
            db.session.add(sale)

            logger.debug(f"Profit (from quotation): {product.name} | Qty: {item['quantity']} | "
                         f"Price: ${selling_price_per_unit:.2f} | Cost: ${cost_price:.2f} | "
                         f"Profit: ${profit:.2f}")

        quotation.converted = True
        db.session.commit()

        # Update cash balance
        update_cash_balance(datetime.utcnow(), sales_amount=total)

        try:
            html = render_template(
                'receipt_template.html',
                items=sold_items,
                subtotal=subtotal,
                discount=discount_amount,
                tax=tax,
                total=total,
                receipt_number=receipt_number,
                customer_name=receipt.customer_name,
                customer_email=receipt.customer_email,
                customer_phone=receipt.customer_phone,
                printed_by=current_user.full_name,
                business_name='Crown Tech Hub',
                business_address='Karimapondo Mall, Corner Robert Mugabe & Leopold Takawira, 1st Floor, Workstation 25, Harare, Zimbabwe',
                logo_path=url_for('static', filename='images/cthlogo.jpg', _external=True),
                current_year=datetime.utcnow().year,
                timestamp=receipt.timestamp
            )
            buffer = BytesIO()
            pisa_status = pisa.CreatePDF(html, dest=buffer)
            if pisa_status.err:
                logger.error(f"PDF generation failed: {pisa_status.err}")
                flash("Failed to generate receipt PDF.", "danger")
                return redirect(url_for('cashier.quotations'))
            buffer.seek(0)
        except Exception as e:
            logger.error(f"PDF generation failed: {str(e)}")
            flash(f"PDF generation failed: {str(e)}", "danger")
            return redirect(url_for('cashier.quotations'))

        flash(f"Quotation {quotation.quotation_number} converted to sale. Receipt #{receipt_number} generated.", "success")
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"receipt_{receipt_number}.pdf",
            mimetype='application/pdf'
        )

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error converting quotation: {str(e)}")
        flash(f"Error converting quotation: {str(e)}", "danger")
        return redirect(url_for('cashier.quotations'))


# === ALL OTHER ROUTES UNCHANGED (kept for completeness) ===

@cashier_bp.route('/clear_cart', methods=['POST'])
@login_required
@cashier_required
def clear_cart():
    session.pop('cart', None)
    session.pop('discount', None)
    session.modified = True
    flash("Cart cleared.", "info")
    return redirect(url_for('cashier.cashier_dashboard'))

@cashier_bp.route('/decrement/<int:product_id>', methods=['POST'])
@login_required
@cashier_required
def decrement_cart(product_id):
    cart = session.get('cart', [])
    for item in cart:
        if item['id'] == product_id:
            item['quantity'] -= 1
            if item['quantity'] <= 0:
                cart.remove(item)
            session['cart'] = cart
            session.modified = True
            break
    flash("Item quantity decreased.", "info")
    return redirect(url_for('cashier.cashier_dashboard'))

@cashier_bp.route('/increment/<int:product_id>', methods=['POST'])
@login_required
@cashier_required
def increment_cart(product_id):
    cart = session.get('cart', [])
    product = Product.query.get(product_id)
    if not product:
        flash("Product not found.", "danger")
        return redirect(url_for('cashier.cashier_dashboard'))

    for item in cart:
        if item['id'] == product_id:
            if item['quantity'] < product.quantity:
                item['quantity'] += 1
                session['cart'] = cart
                session.modified = True
                flash(f"Increased quantity of {product.name} in cart.", "success")
            else:
                flash(f"Cannot add more than available stock for {product.name}.", "warning")
            break
    return redirect(url_for('cashier.cashier_dashboard'))

@cashier_bp.route('/stock_request', methods=['POST'])
@login_required
@cashier_required
def stock_request():
    try:
        product_id = int(request.form.get('product_id'))
        quantity = int(request.form.get('quantity'))
        action = request.form.get('action', 'add').strip()
        reason = request.form.get('reason', '').strip()
        if quantity <= 0:
            flash("Quantity must be greater than zero.", "warning")
            return redirect(url_for('cashier.cashier_dashboard'))
        if action not in ['add', 'deduct']:
            flash("Invalid action specified.", "danger")
            return redirect(url_for('cashier.cashier_dashboard'))
        product = Product.query.get(product_id)
        if not product:
            flash("Product not found.", "danger")
            return redirect(url_for('cashier.cashier_dashboard'))
        request_entry = StockRequest(
            product_id=product_id,
            quantity=quantity,
            action=action,
            reason=reason,
            requested_by=current_user.id,
            timestamp=get_zim_now()   # ← Zimbabwe time
        )
        db.session.add(request_entry)
        db.session.commit()
        flash(f"Stock request for {quantity} units of {product.name} ({action}) submitted.", "success")
        return redirect(url_for('cashier.cashier_dashboard'))
    except ValueError:
        flash("Invalid input data.", "danger")
        return redirect(url_for('cashier.cashier_dashboard'))

@cashier_bp.route('/submit_defect', methods=['POST'])
@login_required
@cashier_required
def submit_defect():
    try:
        product_id = int(request.form.get('product_id'))
        quantity = int(request.form.get('quantity'))
        reason = request.form.get('reason', '').strip()
        product = Product.query.get(product_id)
        if not product:
            flash("Product not found.", "danger")
            return redirect(url_for('cashier.cashier_dashboard'))
        if quantity <= 0 or quantity > product.quantity:
            flash("Invalid quantity selected.", "warning")
            return redirect(url_for('cashier.cashier_dashboard'))
        if not reason:
            flash("Reason is required.", "warning")
            return redirect(url_for('cashier.cashier_dashboard'))
        defect = DefectRequests(
            product_id=product_id,
            quantity=quantity,
            reason=reason,
            submitted_by=current_user.id,
            timestamp=get_zim_now()   # ← Zimbabwe time
        )
        db.session.add(defect)
        db.session.commit()
        flash(f"Defect request for {quantity} x {product.name} submitted.", "success")
        return redirect(url_for('cashier.cashier_dashboard'))
    except ValueError:
        flash("Invalid input data.", "danger")
        return redirect(url_for('cashier.cashier_dashboard'))

@cashier_bp.route('/my_requests')
@login_required
@cashier_required
def my_requests():
    defect_requests = DefectRequests.query.filter_by(submitted_by=current_user.id).order_by(DefectRequests.timestamp.desc()).all()
    stock_requests = StockRequest.query.filter_by(requested_by=current_user.id).order_by(StockRequest.timestamp.desc()).all()
    products = Product.query.order_by(Product.name).all()
    return render_template('my_defect_requests.html', defect_requests=defect_requests, stock_requests=stock_requests, products=products)

@cashier_bp.route('/edit_stock_request/<int:request_id>', methods=['POST'])
@login_required
@cashier_required
def edit_stock_request(request_id):
    request_entry = StockRequest.query.get_or_404(request_id)
    if request_entry.requested_by != current_user.id or request_entry.status.lower() != 'pending':
        return jsonify({'error': 'Unauthorized or request not pending'}), 403

    try:
        product_id = int(request.form.get('product_id'))
        quantity = int(request.form.get('quantity'))
        action = request.form.get('action', 'add').strip()

        if quantity <= 0:
            return jsonify({'error': 'Quantity must be greater than zero'}), 400

        if action not in ['add', 'deduct']:
            return jsonify({'error': 'Invalid action specified'}), 400

        product = Product.query.get(product_id)
        if not product:
            return jsonify({'error': 'Product not found'}), 404

        request_entry.product_id = product_id
        request_entry.quantity = quantity
        request_entry.action = action
        db.session.commit()
        return jsonify({'message': f'Stock request for {quantity} units of {product.name} ({action}) updated'})

    except ValueError:
        return jsonify({'error': 'Invalid input data'}), 400

@cashier_bp.route('/edit_defect_request/<int:request_id>', methods=['POST'])
@login_required
@cashier_required
def edit_defect_request(request_id):
    request_entry = DefectRequests.query.get_or_404(request_id)
    if request_entry.submitted_by != current_user.id or request_entry.status.lower() != 'pending':
        return jsonify({'error': 'Unauthorized or request not pending'}), 403

    try:
        product_id = int(request.form.get('product_id'))
        quantity = int(request.form.get('quantity'))
        reason = request.form.get('reason', '').strip()

        product = Product.query.get(product_id)
        if not product:
            return jsonify({'error': 'Product not found'}), 404

        if quantity <= 0 or quantity > product.quantity:
            return jsonify({'error': 'Invalid quantity selected'}), 400

        if not reason:
            return jsonify({'error': 'Reason is required'}), 400

        request_entry.product_id = product_id
        request_entry.quantity = quantity
        request_entry.reason = reason
        db.session.commit()
        return jsonify({'message': f'Defect request for {quantity} x {product.name} updated'})

    except ValueError:
        return jsonify({'error': 'Invalid input data'}), 400

# ----------------------------------------------------------------------
# RECORD EXPENSE & INCOME – Use Zim time
# ----------------------------------------------------------------------
@cashier_bp.route('/record_expense', methods=['POST'])
@login_required
@cashier_required
def record_expense():
    try:
        description = request.form.get('description', '').strip()
        amount = float(request.form.get('amount', 0))
        if not description or amount <= 0:
            flash("Please provide valid expense details.", "warning")
            return redirect(url_for('cashier.cashier_dashboard'))
        expense = Expense(
            description=description,
            amount=amount,
            recorded_by=current_user.id,
            timestamp=get_zim_now()   # ← Zimbabwe time
        )
        db.session.add(expense)
        db.session.commit()
        update_cash_balance(expense_amount=amount)
        flash("Expense recorded successfully.", "success")
        return redirect(url_for('cashier.cashier_dashboard'))
    except ValueError:
        flash("Invalid expense amount.", "danger")
        return redirect(url_for('cashier.cashier_dashboard'))

@cashier_bp.route('/record_income', methods=['POST'])
@login_required
@cashier_required
def record_income():
    try:
        description = request.form.get('description', '').strip()
        amount = float(request.form.get('amount', 0))
        if not description or amount <= 0:
            flash("Please provide valid income details.", "warning")
            return redirect(url_for('cashier.cashier_dashboard'))
        income = ExtraIncome(
            description=description,
            amount=amount,
            recorded_by=current_user.id,
            timestamp=get_zim_now()   # ← Zimbabwe time
        )
        db.session.add(income)
        db.session.commit()
        update_cash_balance(income_amount=amount)
        flash("Extra income recorded successfully.", "success")
        return redirect(url_for('cashier.cashier_dashboard'))
    except ValueError:
        flash("Invalid income amount.", "danger")
        return redirect(url_for('cashier.cashier_dashboard'))
# ----------------------------------------------------------------------
# DAILY REPORT – Zimbabwe timezone consistent
# ----------------------------------------------------------------------
@cashier_bp.route('/daily_report', methods=['GET', 'POST'])
@login_required
@cashier_required
def daily_report():
    try:
        page = request.args.get('page', 1, type=int)
        per_page = 100
        selected_date_str = request.form.get('selected_date') or request.args.get('selected_date')
        if selected_date_str:
            try:
                report_date_local = datetime.strptime(selected_date_str, '%Y-%m-%d').date()
            except ValueError:
                report_date_local = get_zim_date_now()
                flash("Invalid date format, using today.", "warning")
        else:
            report_date_local = get_zim_date_now()

        start_utc, end_utc = get_utc_range_for_zim_date(report_date_local)

        sales = Sale.query.filter(Sale.timestamp.between(start_utc, end_utc))\
            .order_by(Sale.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)
        expenses = Expense.query.filter(Expense.timestamp.between(start_utc, end_utc))\
            .order_by(Expense.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)
        incomes = ExtraIncome.query.filter(ExtraIncome.timestamp.between(start_utc, end_utc))\
            .order_by(ExtraIncome.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)
        stock_requests = StockRequest.query.filter(StockRequest.timestamp.between(start_utc, end_utc))\
            .order_by(StockRequest.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)
        defect_requests = DefectRequests.query.filter(DefectRequests.timestamp.between(start_utc, end_utc))\
            .order_by(DefectRequests.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)

        total_sales = db.session.query(func.sum(Sale.total_price))\
            .filter(Sale.timestamp.between(start_utc, end_utc)).scalar() or 0.0
        total_expenses = db.session.query(func.sum(Expense.amount))\
            .filter(Expense.timestamp.between(start_utc, end_utc)).scalar() or 0.0
        total_income = db.session.query(func.sum(ExtraIncome.amount))\
            .filter(ExtraIncome.timestamp.between(start_utc, end_utc)).scalar() or 0.0
        net_cash_flow = total_sales + total_income - total_expenses

        cash_balance = DailyCashBalance.query.filter_by(date=report_date_local).first()
        if not cash_balance:
            prev_day = report_date_local - timedelta(days=1)
            prev = DailyCashBalance.query.filter_by(date=prev_day).first()
            opening = prev.closing_balance if prev else 0.0
            cash_balance = DailyCashBalance(
                date=report_date_local,
                opening_balance=opening,
                closing_balance=opening + net_cash_flow,
                updated_at=datetime.utcnow()
            )
            db.session.add(cash_balance)
            db.session.commit()
        else:
            cash_balance.closing_balance = cash_balance.opening_balance + net_cash_flow
            cash_balance.updated_at = datetime.utcnow()
            db.session.commit()

        # Hourly chart – Zimbabwe local hours
        labels, sales_data, expenses_data, income_data = [], [], [], []
        sales_records   = Sale.query.filter(Sale.timestamp.between(start_utc, end_utc)).all()
        expense_records = Expense.query.filter(Expense.timestamp.between(start_utc, end_utc)).all()
        income_records  = ExtraIncome.query.filter(ExtraIncome.timestamp.between(start_utc, end_utc)).all()

        def local_hour(ts):
            return (ts + ZIM_OFFSET).hour

        for hour in range(24):
            labels.append(f"{hour:02d}:00")
            s = sum(float(x.total_price or 0) for x in sales_records if local_hour(x.timestamp) == hour)
            e = sum(float(x.amount or 0) for x in expense_records if local_hour(x.timestamp) == hour)
            i = sum(float(x.amount or 0) for x in income_records if local_hour(x.timestamp) == hour)
            sales_data.append(round(s, 2))
            expenses_data.append(round(e, 2))
            income_data.append(round(i, 2))

        if request.method == 'POST' and request.form.get('action') == 'export_pdf':
            try:
                html = render_template(
                    'cashier_report_pdf.html',
                    sales=sales.items,
                    expenses=expenses.items,
                    incomes=incomes.items,
                    stock_requests=stock_requests.items,
                    defect_requests=defect_requests.items,
                    total_sales=round(total_sales, 2),
                    total_expenses=round(total_expenses, 2),
                    total_income=round(total_income, 2),
                    net_cash_flow=round(net_cash_flow, 2),
                    opening_balance=round(cash_balance.opening_balance, 2),
                    closing_balance=round(cash_balance.closing_balance, 2),
                    date=report_date_local.strftime('%Y-%m-%d'),
                    logo_path=url_for('static', filename='images/cthlogo.jpg', _external=True)
                )
                buffer = BytesIO()
                pisa_status = pisa.CreatePDF(html, dest=buffer)
                if pisa_status.err:
                    flash("Failed to generate PDF.", "danger")
                    return redirect(url_for('cashier.daily_report'))
                buffer.seek(0)
                return send_file(
                    buffer,
                    as_attachment=True,
                    download_name=f"daily_report_{report_date_local.strftime('%Y-%m-%d')}.pdf",
                    mimetype='application/pdf'
                )
            except Exception as e:
                logger.error(f"PDF error: {str(e)}")
                flash("PDF generation failed.", "danger")

        return render_template(
            'daily_report.html',
            sales=sales,
            expenses=expenses,
            incomes=incomes,
            stock_requests=stock_requests,
            defect_requests=defect_requests,
            total_sales=round(total_sales, 2),
            total_expenses=round(total_expenses, 2),
            total_income=round(total_income, 2),
            net_cash_flow=round(net_cash_flow, 2),
            opening_balance=round(cash_balance.opening_balance, 2),
            closing_balance=round(cash_balance.closing_balance, 2),
            labels=labels,
            sales_data=sales_data,
            expenses_data=expenses_data,
            income_data=income_data,
            report_page=True,
            view='report',
            current_range='daily',
            date=report_date_local.strftime('%Y-%m-%d'),
            selected_date=selected_date_str or report_date_local.strftime('%Y-%m-%d')
        )
    except Exception as e:
        logger.error(f"Error in daily_report: {str(e)}")
        flash("An error occurred while loading the report.", "danger")
        return redirect(url_for('cashier.cashier_dashboard'))

def generate_quotation_number():
    last_quotation = Quotation.query.order_by(Quotation.id.desc()).first()
    prefix = "cthq"
    if last_quotation and last_quotation.quotation_number:
        qnum_lower = last_quotation.quotation_number.lower()
        if qnum_lower.startswith(prefix):
            number_part = qnum_lower[len(prefix):]
            try:
                last_number = int(number_part)
            except ValueError:
                last_number = 0
        else:
            last_number = 0
    else:
        last_number = 0
    return f"{prefix}{last_number + 1:03d}"

@cashier_bp.route('/quotations', methods=['GET', 'POST'])
@login_required
@cashier_required
def quotations():
    if request.method == 'POST':
        try:
            customer_name = request.form.get('customer_name', '').strip()
            discount = float(request.form.get('discount_value', 0))
            items_json = request.form.get('items')
            items = json.loads(items_json) if items_json else []

            if not items or not customer_name:
                flash("Please provide customer name and at least one item.", "danger")
                return redirect(url_for('cashier.quotations'))

            quotation_number = generate_quotation_number()

            quotation = Quotation(
                quotation_number=quotation_number,
                customer_name=customer_name,
                created_by_id=current_user.id,
                discount=discount,
                created_at=datetime.utcnow()
            )
            db.session.add(quotation)
            db.session.flush()

            for item in items:
                product_id = item.get('product_id')
                product = Product.query.get(product_id)
                if not product:
                    db.session.rollback()
                    flash(f"Product ID {product_id} not found.", "danger")
                    return redirect(url_for('cashier.quotations'))

                qitem = QuotationItem(
                    quotation_id=quotation.id,
                    product_id=product_id,
                    product_name=item.get('product_name'),
                    quantity=int(item.get('quantity', 1)),
                    unit_price=float(item.get('unit_price')),
                    total_price=float(item.get('quantity', 1)) * float(item.get('unit_price'))
                )
                db.session.add(qitem)

            db.session.commit()
            flash(f"Quotation {quotation_number} created successfully!", "success")
            return redirect(url_for('cashier.quotations'))

        except (ValueError, json.JSONDecodeError) as e:
            db.session.rollback()
            flash(f"Error creating quotation: Invalid input data.", "danger")
            return redirect(url_for('cashier.quotations'))
        except Exception as e:
            db.session.rollback()
            flash(f"Error creating quotation: {str(e)}", "danger")
            return redirect(url_for('cashier.quotations'))

    products = Product.query.all()
    quotations = Quotation.query.filter_by(created_by_id=current_user.id).order_by(Quotation.created_at.desc()).all()
    return render_template('cashier_quotations.html', products=products, quotations=quotations)

@cashier_bp.route('/download_quotation/<int:quotation_id>')
@login_required
@cashier_required
def download_quotation(quotation_id):
    try:
        quotation = Quotation.query.get_or_404(quotation_id)
        if quotation.created_by_id != current_user.id:
            abort(403)

        html = render_template(
            'print_quotation.html',
            quotation=quotation,
            business_name="Crown Tech Hub",
            business_address="Karimapondo Mall, Corner Robert Mugabe & Leopold Takawira, 1st Floor, Workstation 25",
            printed_by=current_user.full_name,
            logo_path=url_for('static', filename='images/cthlogo.jpg', _external=True)
        )
        buffer = BytesIO()
        pisa_status = pisa.CreatePDF(html, dest=buffer)
        if pisa_status.err:
            logger.error(f"PDF generation failed: {pisa_status.err}")
            flash("Failed to generate quotation PDF.", "danger")
            return redirect(url_for('cashier.quotations'))
        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"quotation_{quotation.quotation_number}.pdf",
            mimetype='application/pdf'
        )

    except Exception as e:
        logger.error(f"Error generating PDF: {str(e)}")
        flash(f"Error generating PDF: {str(e)}", "danger")
        return redirect(url_for('cashier.quotations'))

@cashier_bp.route('/quotations/<int:quotation_id>/edit', methods=['GET', 'POST'])
@login_required
@cashier_required
def edit_quotation(quotation_id):
    quotation = Quotation.query.get_or_404(quotation_id)
    if quotation.created_by_id != current_user.id:
        abort(403)
    if quotation.converted:
        flash("Cannot edit a converted quotation.", "warning")
        return redirect(url_for('cashier.quotations'))

    if request.method == 'POST':
        try:
            customer_name = request.form.get('customer_name', '').strip()
            discount = float(request.form.get('discount_value', 0))
            items_json = request.form.get('items')
            items = json.loads(items_json) if items_json else []

            if not customer_name or not items:
                flash("Customer name and at least one item are required.", "danger")
                return redirect(url_for('cashier.edit_quotation', quotation_id=quotation.id))

            quotation.customer_name = customer_name
            quotation.discount = discount

            quotation.items.clear()
            for item in items:
                product_id = item.get('product_id')
                product = Product.query.get(product_id)
                if not product:
                    db.session.rollback()
                    flash(f"Product ID {product_id} not found.", "danger")
                    return redirect(url_for('cashier.edit_quotation', quotation_id=quotation.id))

                qitem = QuotationItem(
                    quotation_id=quotation.id,
                    product_id=product_id,
                    product_name=item.get('product_name'),
                    quantity=int(item.get('quantity', 1)),
                    unit_price=float(item.get('unit_price')),
                    total_price=float(item.get('quantity', 1)) * float(item.get('unit_price'))
                )
                db.session.add(qitem)

            db.session.commit()
            flash(f"Quotation {quotation.quotation_number} updated successfully.", "success")
            return redirect(url_for('cashier.quotations'))

        except (ValueError, json.JSONDecodeError) as e:
            db.session.rollback()
            flash(f"Error updating quotation: Invalid input data.", "danger")
            return redirect(url_for('cashier.edit_quotation', quotation_id=quotation.id))
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating quotation: {str(e)}", "danger")
            return redirect(url_for('cashier.edit_quotation', quotation_id=quotation.id))

    products = Product.query.all()
    quotations = Quotation.query.filter_by(created_by_id=current_user.id).order_by(Quotation.created_at.desc()).all()
    return render_template('cashier_quotations.html', products=products, quotations=quotations, edit_quotation=quotation)