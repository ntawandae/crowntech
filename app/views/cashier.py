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
from flask import current_app
from ..services import pos, quotes
from ..services.pos import PricingError, SaleError
from ..services.quotes import generate_quotation_number
from ..utils.receipts import receipt_pdf, receipt_items, render_pdf, static_link_callback
from ..utils.timeutil import ZIM_OFFSET, day_range, zim_now as _zim_now, zim_today as _zim_today
import hashlib

# Configure logging
logging.basicConfig(level=logging.INFO)
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
# Time helpers now live in app/utils/timeutil.py (Zimbabwe local time is the stored convention).

def get_zim_now():
    """Current Zimbabwe local time (UTC+2)"""
    return _zim_now()

def get_zim_date_now():
    """Current Zimbabwe business date"""
    return _zim_today()

def get_utc_range_for_zim_date(zim_date):
    """Start/end of a Zimbabwe business day. (Name kept for older callers: stored times are local,
    so this is a plain local-day range and no longer shifts by 2 hours.)"""
    return day_range(zim_date)

# Helper function to update daily cash balance
def update_cash_balance(sales_amount=0.0, income_amount=0.0, expense_amount=0.0):
    """Rebuild today's cash balance from the records (the amounts are ignored: the records are the truth)."""
    try:
        pos.recompute_day(get_zim_date_now())
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
    return render_template('cashier_dashboard.html', products=products, cart=cart,
                           discount_limit=current_app.config['CASHIER_MAX_DISCOUNT_PERCENT'],
                           delivery_enabled=True)
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
def _cart_lines():
    """Cart lines priced from the live catalogue (never from a stale session copy)."""
    lines = []
    for item in session.get('cart', []):
        product = db.session.get(Product, item.get('id')) if isinstance(item, dict) else None
        if not product:
            raise SaleError(f"A product in the cart no longer exists: {item.get('name', 'unknown')}.")
        try:
            qty = int(item.get('quantity', 0))
        except (TypeError, ValueError):
            qty = 0
        if qty >= 1:
            lines.append({"product": product, "qty": qty, "price": product.price})
    return lines


def _price_from_form(lines):
    subtotal = sum((pos.D(l["price"]) * l["qty"] for l in lines), pos.D(0))
    limit = current_app.config['CASHIER_MAX_DISCOUNT_PERCENT']
    return pos.price_sale(subtotal, request.form.get('discount_mode', 'amount'),
                          request.form.get('discount_value') or 0,
                          request.form.get('delivery_fee') or 0, limit)


def _process_sale(want_receipt):
    """Shared by 'Sell' and 'Sell & receipt'. The cart is only emptied once the sale is saved."""
    back = redirect(url_for('cashier.cashier_dashboard'))
    try:
        lines = _cart_lines()
        if not lines:
            flash("Cart is empty.", "warning")
            return back
        pricing = _price_from_form(lines)
        result = pos.complete_sale(
            lines, current_user, pricing,
            customer={"name": request.form.get('customer_name'), "email": request.form.get('customer_email'),
                      "phone": request.form.get('customer_phone')},
            delivery_address=request.form.get('delivery_address'))
    except (PricingError, SaleError) as exc:
        flash(str(exc), "danger")
        return back
    except Exception:
        db.session.rollback()
        logger.exception("Unexpected error while selling")
        flash("Something went wrong and the sale was not saved. Please try again.", "danger")
        return back

    session.pop('cart', None)
    session.pop('discount', None)
    session.modified = True
    receipt = result['receipt']
    summary = f"Sale completed. Receipt #{receipt.receipt_number} - total ${receipt.total:.2f}."
    if not want_receipt:
        flash(summary, "success")
        return back
    pdf = receipt_pdf(receipt, result['items'], current_user.full_name)
    if pdf is None:
        flash(f"{summary} The receipt PDF could not be created: reprint it from Reports.", "warning")
        return back
    flash(summary, "success")
    return send_file(pdf, as_attachment=True, download_name=f"receipt_{receipt.receipt_number}.pdf",
                     mimetype='application/pdf')


@cashier_bp.route('/sell_and_receipt', methods=['POST'])
@login_required
@cashier_required
def sell_and_receipt():
    return _process_sale(want_receipt=True)


# ----------------------------------------------------------------------
# SELL ONLY (no receipt PDF) – original working version + Zim time
# ----------------------------------------------------------------------
@cashier_bp.route('/sell', methods=['POST'])
@login_required
@cashier_required
def sell_items():
    return _process_sale(want_receipt=False)
    
@cashier_bp.route('/quotations/<int:quotation_id>/convert', methods=['POST'])
@login_required
@cashier_required
def convert_quotation(quotation_id):
    quotation = Quotation.query.get_or_404(quotation_id)
    if quotation.created_by_id != current_user.id:
        abort(403)
    try:
        result = quotes.convert_to_sale(
            quotation, current_user,
            customer_email=request.form.get('customer_email', ''),
            customer_phone=request.form.get('customer_phone', ''),
            delivery_fee=request.form.get('delivery_fee') or 0)
    except (PricingError, SaleError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for('cashier.quotations'))
    except Exception:
        db.session.rollback()
        logger.exception("Error converting quotation")
        flash("Something went wrong converting that quotation. Nothing was changed.", "danger")
        return redirect(url_for('cashier.quotations'))

    receipt = result['receipt']
    flash(f"Quotation {quotation.quotation_number} converted. Receipt #{receipt.receipt_number} recorded.", "success")
    pdf = receipt_pdf(receipt, result['items'], current_user.full_name)
    if pdf is None:
        flash("The receipt PDF could not be created: reprint it from Reports.", "warning")
        return redirect(url_for('cashier.quotations'))
    return send_file(pdf, as_attachment=True, download_name=f"receipt_{receipt.receipt_number}.pdf",
                     mimetype='application/pdf')


# === ALL OTHER ROUTES UNCHANGED (kept for completeness) ===

@cashier_bp.route('/receipt/<int:receipt_id>/pdf')
@login_required
@cashier_required
def reprint_receipt(receipt_id):
    receipt = Receipt.query.get_or_404(receipt_id)
    pdf = receipt_pdf(receipt, receipt_items(receipt), current_user.full_name)
    if pdf is None:
        flash("Could not create the receipt PDF.", "danger")
        return redirect(url_for('cashier.daily_report'))
    return send_file(pdf, as_attachment=True, download_name=f"receipt_{receipt.receipt_number}.pdf",
                     mimetype='application/pdf')


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

        cash_balance = pos.recompute_day(report_date_local)

        # Hourly chart – Zimbabwe local hours
        labels, sales_data, expenses_data, income_data = [], [], [], []
        sales_records   = Sale.query.filter(Sale.timestamp.between(start_utc, end_utc)).all()
        expense_records = Expense.query.filter(Expense.timestamp.between(start_utc, end_utc)).all()
        income_records  = ExtraIncome.query.filter(ExtraIncome.timestamp.between(start_utc, end_utc)).all()

        def local_hour(ts):
            return ts.hour  # stored times are already Zimbabwe local

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
                pisa_status = pisa.CreatePDF(html, dest=buffer, link_callback=static_link_callback)
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


@cashier_bp.route('/quotations', methods=['GET', 'POST'])
@login_required
@cashier_required
def quotations():
    if request.method == 'POST':
        try:
            customer, items, discount = quotes.parse_quote_form(
                request.form, current_app.config['CASHIER_MAX_DISCOUNT_PERCENT'])
            q = quotes.save_quotation(current_user, customer, items, discount)
            flash(f"Quotation {q.quotation_number} created.", "success")
        except PricingError as exc:
            flash(str(exc), "danger")
        except Exception:
            db.session.rollback()
            logger.exception("Error creating quotation")
            flash("The quotation could not be saved. Please try again.", "danger")
        return redirect(url_for('cashier.quotations'))

    products = Product.query.order_by(Product.name).all()
    mine = Quotation.query.filter_by(created_by_id=current_user.id).order_by(Quotation.created_at.desc()).all()
    return render_template('quotations.html', products=products, quotations=mine, edit=None)

@cashier_bp.route('/download_quotation/<int:quotation_id>')
@login_required
@cashier_required
def download_quotation(quotation_id):
    quotation = Quotation.query.get_or_404(quotation_id)
    if quotation.created_by_id != current_user.id:
        abort(403)
    pdf = render_pdf('print_quotation.html', quotation=quotation,
                     business_name=current_app.config['BUSINESS_NAME'],
                     business_address=current_app.config['BUSINESS_ADDRESS'],
                     printed_by=current_user.full_name,
                     logo_path=url_for('static', filename='images/cthlogo.jpg'))
    if pdf is None:
        flash("Failed to generate the quotation PDF.", "danger")
        return redirect(url_for('cashier.quotations'))
    return send_file(pdf, as_attachment=True, download_name=f"quotation_{quotation.quotation_number}.pdf",
                     mimetype='application/pdf')

@cashier_bp.route('/quotations/<int:quotation_id>/edit', methods=['GET', 'POST'])
@login_required
@cashier_required
def edit_quotation(quotation_id):
    quotation = Quotation.query.get_or_404(quotation_id)
    if quotation.created_by_id != current_user.id:
        abort(403)
    if quotation.converted:
        flash("A converted quotation can't be edited.", "warning")
        return redirect(url_for('cashier.quotations'))

    if request.method == 'POST':
        try:
            customer, items, discount = quotes.parse_quote_form(
                request.form, current_app.config['CASHIER_MAX_DISCOUNT_PERCENT'])
            quotes.save_quotation(current_user, customer, items, discount, quotation=quotation)
            flash(f"Quotation {quotation.quotation_number} updated.", "success")
            return redirect(url_for('cashier.quotations'))
        except PricingError as exc:
            flash(str(exc), "danger")
        except Exception:
            db.session.rollback()
            logger.exception("Error updating quotation")
            flash("The quotation could not be saved. Please try again.", "danger")
        return redirect(url_for('cashier.edit_quotation', quotation_id=quotation.id))

    products = Product.query.order_by(Product.name).all()
    mine = Quotation.query.filter_by(created_by_id=current_user.id).order_by(Quotation.created_at.desc()).all()
    return render_template('quotations.html', products=products, quotations=mine,
                           edit=quotes.quotation_edit_payload(quotation), edit_id=quotation.id,
                           edit_number=quotation.quotation_number)