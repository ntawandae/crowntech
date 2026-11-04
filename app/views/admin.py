# -*- coding: utf-8 -*-
from datetime import datetime, date, timedelta
import logging
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, current_app, abort, jsonify, send_file
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from sqlalchemy import func
from xhtml2pdf import pisa
from io import BytesIO
import os
import json
import secrets
from werkzeug.security import generate_password_hash
from .. import login_manager
from ..services import pos, quotes
from ..services.pos import PricingError, SaleError
from ..utils.receipts import receipt_items, receipt_pdf, render_pdf, static_link_callback
from ..utils.timeutil import day_range, zim_now, zim_today

# ----------------------------------------------------------------------
# TIMEZONE HELPER – Zimbabwe (UTC+2, no DST)
# ----------------------------------------------------------------------
ZIM_OFFSET = timedelta(hours=2)  # Zimbabwe is UTC+2

def get_utc_range_for_zim_date(zim_date: date):
    """Start/end of a Zimbabwe business day. (Name kept for older callers. Stored times are Zimbabwe
    local time, so this is a plain local-day range - it no longer shifts by 2 hours, which is what
    pushed late-evening sales onto the next day's report.)"""
    return day_range(zim_date)

# ----------------------------------------------------------------------
# Backfill & Balance Correction – Uses Zimbabwe day ranges
# ----------------------------------------------------------------------
def backfill_balances_until_today():
    """Recompute and correct DailyCashBalance records up to today using Zimbabwe business days."""
    today = zim_now().date()
    all_balances = DailyCashBalance.query.order_by(DailyCashBalance.date.asc()).all()

    if not all_balances:
        first = DailyCashBalance(date=today, opening_balance=0.0, closing_balance=0.0, updated_at=zim_now())
        db.session.add(first)
        db.session.commit()
        logger.info(f"Initialized first balance for {today}")
        return

    prev_closing = 0.0
    for balance in all_balances:
        day = balance.date if isinstance(balance.date, date) else balance.date.date()
        start_utc, end_utc = get_utc_range_for_zim_date(day)

        total_sales   = db.session.query(func.sum(Sale.total_price))  .filter(Sale.timestamp.between(start_utc, end_utc)).scalar() or 0.0
        total_income  = db.session.query(func.sum(ExtraIncome.amount)).filter(ExtraIncome.timestamp.between(start_utc, end_utc)).scalar() or 0.0
        total_expenses = db.session.query(func.sum(Expense.amount))   .filter(Expense.timestamp.between(start_utc, end_utc)).scalar() or 0.0

        net_cash_flow = total_sales + total_income - total_expenses
        expected_opening = prev_closing
        expected_closing = expected_opening + net_cash_flow

        if abs(balance.opening_balance - expected_opening) > 0.01 or abs(balance.closing_balance - expected_closing) > 0.01:
            balance.opening_balance = round(expected_opening, 2)
            balance.closing_balance = round(expected_closing, 2)
            balance.updated_at = zim_now()
            db.session.commit()
            logger.info(f"Corrected {day}: Open={expected_opening:.2f}, Close={expected_closing:.2f}")

        prev_closing = balance.closing_balance

    # Backfill missing future days
    last_date = all_balances[-1].date if all_balances else today
    if isinstance(last_date, datetime):
        last_date = last_date.date()
    current_date = last_date + timedelta(days=1)
    while current_date <= today:
        new_balance = DailyCashBalance(
            date=current_date,
            opening_balance=round(prev_closing, 2),
            closing_balance=round(prev_closing, 2),
            updated_at=zim_now()
        )
        db.session.add(new_balance)
        db.session.commit()
        prev_closing = new_balance.closing_balance
        logger.info(f"Backfilled {current_date}: Opening/Closing={prev_closing:.2f}")
        current_date += timedelta(days=1)

# ----------------------------------------------------------------------
# Blueprint + logger
# ----------------------------------------------------------------------
admin_bp = Blueprint('admin', __name__, url_prefix='/admin')
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
logger.addHandler(handler)

# ----------------------------------------------------------------------
# Models & Utils
# ----------------------------------------------------------------------
from ..models import (
    db, Product, Sale, Expense, DefectRequests, StockRequest, User, UserProfile,
    Quotation, QuotationItem, Receipt, ExtraIncome, DailyCashBalance, SaleReversal
)
from app.utils.pdf_generator import generate_quotation_number, generate_pdf

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def validate_date(date_obj):
    try:
        if isinstance(date_obj, str):
            return datetime.strptime(date_obj, "%Y-%m-%d").date()
        if isinstance(date_obj, (datetime, date)):
            return date_obj if isinstance(date_obj, date) else date_obj.date()
        return None
    except Exception as e:
        logger.error(f"Date validation error: {str(e)}")
        return None

def update_cash_balance(local_date, sales_amount=0.0, income_amount=0.0, expense_amount=0.0):
    """Rebuild that day's balance from the records (the amounts are ignored: the records are the truth)."""
    try:
        pos.recompute_day(local_date)
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating cash balance {local_date}: {str(e)}")

def calculate_cumulative_balances(selected_date):
    try:
        selected_date = selected_date if isinstance(selected_date, date) else zim_today()
        start_utc, end_utc = get_utc_range_for_zim_date(selected_date)
        prev_day = selected_date - timedelta(days=1)
        prev_balance = DailyCashBalance.query.filter_by(date=prev_day).first()
        opening_balance = round(prev_balance.closing_balance if prev_balance else 0.0, 2)

        total_sales   = db.session.query(func.sum(Sale.total_price))  .filter(Sale.timestamp.between(start_utc, end_utc)).scalar() or 0.0
        total_income  = db.session.query(func.sum(ExtraIncome.amount)).filter(ExtraIncome.timestamp.between(start_utc, end_utc)).scalar() or 0.0
        total_expenses = db.session.query(func.sum(Expense.amount))   .filter(Expense.timestamp.between(start_utc, end_utc)).scalar() or 0.0

        sales = Sale.query.filter(Sale.timestamp.between(start_utc, end_utc)).all()
        total_profit = sum(float(s.profit or 0.0) for s in sales)

        net_cash_flow = total_sales + total_income - total_expenses
        net_profit    = total_profit + total_income - total_expenses

        current = DailyCashBalance.query.filter_by(date=selected_date).first()
        closing_balance = round(current.closing_balance if current else opening_balance + net_cash_flow, 2)

        return (
            opening_balance, round(total_sales, 2), round(total_income, 2), round(total_expenses, 2),
            round(total_profit, 2), round(net_cash_flow, 2), round(net_profit, 2), closing_balance
        )
    except Exception as e:
        logger.error(f"Error calculating balances for {selected_date}: {str(e)}")
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

# ----------------------------------------------------------------------
# ADMIN DASHBOARD
# ----------------------------------------------------------------------
@admin_bp.route("/dashboard", methods=["GET", "POST"])
@login_required
def admin_dashboard():
    if current_user.role != "admin":
        return redirect(url_for("auth.login"))

    backfill_balances_until_today()

    view = request.form.get("view", request.args.get("view", "overview"))
    selected_date_str = request.form.get("selected_date", request.args.get("selected_date", zim_now().strftime('%Y-%m-%d')))
    range_type = request.form.get("range_type", request.args.get("range_type", "daily"))  # NEW: daily or monthly

    try:
        base_date = datetime.strptime(selected_date_str, "%Y-%m-%d").date()
    except ValueError:
        base_date = zim_now().date()
        flash("Invalid date format, defaulting to today.", "warning")

    # Determine date range based on view type
    if range_type == "monthly":
        # First day of the month
        start_date = date(base_date.year, base_date.month, 1)
        # Last day of the month
        if base_date.month == 12:
            end_date = date(base_date.year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(base_date.year, base_date.month + 1, 1) - timedelta(days=1)
        title_date = start_date.strftime("%B %Y")  # e.g., "February 2026"
    else:
        start_date = base_date
        end_date = base_date
        title_date = start_date.strftime("%Y-%m-%d")

    # Get UTC range covering the full period
    start_utc, end_utc = get_utc_range_for_zim_date(start_date)
    # Extend end_utc to cover the last day fully
    _, end_utc_last = get_utc_range_for_zim_date(end_date)
    end_utc = max(end_utc, end_utc_last)

    # Core financials – aggregate over full range
    # We need to adjust calculate_cumulative_balances to support range instead of single date
    # For now, we'll sum directly (later we can refactor calculate function)
    total_sales = db.session.query(func.sum(Sale.total_price))\
        .filter(Sale.timestamp.between(start_utc, end_utc)).scalar() or 0.0
    total_income = db.session.query(func.sum(ExtraIncome.amount))\
        .filter(ExtraIncome.timestamp.between(start_utc, end_utc)).scalar() or 0.0
    total_expenses = db.session.query(func.sum(Expense.amount))\
        .filter(Expense.timestamp.between(start_utc, end_utc)).scalar() or 0.0
    total_profit = db.session.query(func.sum(Sale.profit))\
        .filter(Sale.timestamp.between(start_utc, end_utc)).scalar() or 0.0

    net_cash_flow = total_sales + total_income - total_expenses
    net_profit = total_profit + total_income - total_expenses

    # Opening/closing balance: use first day's opening + monthly net flow
    first_day_balance = DailyCashBalance.query.filter_by(date=start_date).first()
    opening_balance = round(first_day_balance.opening_balance if first_day_balance else 0.0, 2)
    closing_balance = round(opening_balance + net_cash_flow, 2)

    # Products (top sold – full period)
# Products for 'products' view
    products = []
    if view == "products":
        products = (
            Product.query
            .outerjoin(Sale)  # left join so zero-sales products are included
            .group_by(Product.id)
            .order_by(func.coalesce(func.sum(Sale.quantity), 0).desc(), Product.name.asc())
            .all()
        )
    # Paginated data (all transactions in period)
    page = request.args.get("page", 1, type=int)
    per_page = 100
    sales = Sale.query.filter(Sale.timestamp.between(start_utc, end_utc)).order_by(Sale.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)
    expenses = Expense.query.filter(Expense.timestamp.between(start_utc, end_utc)).order_by(Expense.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)
    incomes = ExtraIncome.query.filter(ExtraIncome.timestamp.between(start_utc, end_utc)).order_by(ExtraIncome.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)

    # Stock/defect requests – same cutoff logic, but full period
    cutoff_date = zim_now() - timedelta(days=3)  # longer for monthly
    stock_requests = StockRequest.query.filter(
        (StockRequest.status == 'pending') | (StockRequest.timestamp >= cutoff_date)
    ).order_by(StockRequest.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)
    defect_requests = DefectRequests.query.filter(
        (DefectRequests.status == 'Pending') | (DefectRequests.timestamp >= cutoff_date)
    ).order_by(DefectRequests.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)

    # Monthly chart: daily totals instead of hourly
    labels, sales_data, expenses_data, income_data, profit_data = [], [], [], [], []
    current = start_date
    while current <= end_date:
        day_start, day_end = get_utc_range_for_zim_date(current)
        day_sales = db.session.query(func.sum(Sale.total_price))\
            .filter(Sale.timestamp.between(day_start, day_end)).scalar() or 0.0
        day_income = db.session.query(func.sum(ExtraIncome.amount))\
            .filter(ExtraIncome.timestamp.between(day_start, day_end)).scalar() or 0.0
        day_expenses = db.session.query(func.sum(Expense.amount))\
            .filter(Expense.timestamp.between(day_start, day_end)).scalar() or 0.0
        day_profit = db.session.query(func.sum(Sale.profit))\
            .filter(Sale.timestamp.between(day_start, day_end)).scalar() or 0.0

        labels.append(current.strftime("%d"))
        sales_data.append(round(day_sales, 2))
        expenses_data.append(round(day_expenses, 2))
        income_data.append(round(day_income, 2))
        profit_data.append(round(day_profit, 2))
        current += timedelta(days=1)

    # PDF Export – update to monthly if needed
    if request.method == "POST" and request.form.get("action") == "export_pdf":
        try:
            html = render_template(
                "report_pdf.html",
                sales=sales.items,
                expenses=expenses.items,
                incomes=incomes.items,
                stock_requests=stock_requests.items,
                defect_requests=defect_requests.items,
                products=products,
                total_sales=total_sales,
                total_expenses=total_expenses,
                total_income=total_income,
                total_profit=total_profit,
                net_cash_flow=net_cash_flow,
                opening_balance=opening_balance,
                closing_balance=closing_balance,
                date=title_date,  # e.g., "February 2026" or single date
                range_type=range_type,
                current_range=range_type,
                logo_path=url_for("static", filename="images/cthlogo.jpg", _external=True)
            )
            buffer = BytesIO()
            pisa.CreatePDF(html, dest=buffer, link_callback=static_link_callback)
            buffer.seek(0)
            filename = f"admin_report_{title_date.replace(' ', '_')}.pdf"
            return send_file(buffer, as_attachment=True, download_name=filename, mimetype="application/pdf")
        except Exception as e:
            logger.error(f"PDF generation failed: {str(e)}")
            flash("Failed to generate PDF.", "danger")

    # Summary
    summary = {
        "opening_balance": round(opening_balance, 2),
        "total_sales": round(total_sales, 2),
        "total_income": round(total_income, 2),
        "total_expenses": round(total_expenses, 2),
        "total_profit": round(total_profit, 2),
        "net_cash_flow": round(net_cash_flow, 2),
        "net_profit": round(net_profit, 2),
        "closing_balance": round(closing_balance, 2)
    }

    return render_template(
        "admin_dashboard.html",
        view=view,
        selected_date=base_date.strftime("%Y-%m-%d"),
        range_type=range_type,
        title_date=title_date,
        summary=summary,
        opening_balance=summary["opening_balance"],
        closing_balance=summary["closing_balance"],
        total_sales=summary["total_sales"],
        total_income=summary["total_income"],
        total_expenses=summary["total_expenses"],
        total_profit=summary["total_profit"],
        net_cash_flow=summary["net_cash_flow"],
        net_profit=summary["net_profit"],
        sales=sales,
        products=products,
        expenses=expenses,
        incomes=incomes,
        stock_requests=stock_requests,
        defect_requests=defect_requests,
        labels=labels,
        sales_data=sales_data,
        expenses_data=expenses_data,
        income_data=income_data,
        profit_data=profit_data,
        report_page=True
    )
@admin_bp.route("/backfill_balances", methods=["GET"])
@login_required
def backfill_balances():
    if current_user.role != "admin":
        return redirect(url_for("auth.login"))
    backfill_balances_until_today()
    flash("Balances recalculated and backfilled successfully.", "success")
    return redirect(url_for("admin.admin_dashboard"))


@admin_bp.route("/products", methods=["GET"])
@login_required
def admin_products():
    if current_user.role != "admin":
        return redirect(url_for("auth.login"))

    # Fetch all products ordered by name
    products = Product.query.order_by(Product.name).all()

    return render_template(
        "admin_dashboard.html",  # Or a separate template like "admin_products.html"
        view='products',
        products=products
    )

@admin_bp.route('/add-stock', methods=['GET', 'POST'])
@login_required
def add_stock():
    if current_user.role != 'admin':
        abort(403)

    if request.method == 'POST':
        mode = request.form.get('mode')
        if mode == 'existing':
            product_id = int(request.form.get('product_id', 0))
            quantity = int(request.form.get('quantity', 0))
            product = Product.query.get(product_id)
            if product and quantity > 0:
                product.quantity += quantity
                db.session.commit()
                flash(f"{quantity} added to {product.name}.", "success")
            else:
                flash("Invalid product or quantity.", "danger")
            return redirect(url_for('admin.add_stock'))

        elif mode == 'new':
            name = request.form.get('name', '').strip()
            brand = request.form.get('brand', '').strip()
            category = request.form.get('category', '').strip()
            price = float(request.form.get('price', 0))
            cost_price = float(request.form.get('cost_price', 0))
            quantity = int(request.form.get('quantity', 0))
            image = request.files.get('image')

            if Product.query.filter(func.lower(Product.name) == func.lower(name)).first():
                flash(f"Product '{name}' already exists.", "danger")
                return redirect(url_for('admin.add_stock'))

            if not image or image.filename == '' or not name or not brand or price < 0 or cost_price < 0 or quantity < 0:
                flash('Please provide valid product details and image.', 'danger')
                return redirect(url_for('admin.add_stock'))

            filename = secure_filename(image.filename)
            image_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            os.makedirs(current_app.config['UPLOAD_FOLDER'], exist_ok=True)
            image.save(image_path)

            new_product = Product(
                name=name,
                brand=brand,
                category=category,
                price=price,
                cost_price=cost_price,
                quantity=quantity,
                image_filename=filename
            )
            db.session.add(new_product)
            db.session.commit()
            flash(f"New product '{name}' added with {quantity} in stock.", "success")
            return redirect(url_for('admin.add_stock'))

    products = Product.query.order_by(Product.name).all()
    summary = calculate_cumulative_balances(zim_today())
    summary = {
        "opening_balance": round(summary[0], 2),
        "total_sales": round(summary[1], 2),
        "total_income": round(summary[2], 2),
        "total_expenses": round(summary[3], 2),
        "total_profit": round(summary[4], 2),
        "net_cash_flow": round(summary[5], 2),
        "net_profit": round(summary[6], 2),
        "closing_balance": round(summary[7], 2)
    }
    return render_template(
        'admin_dashboard.html',
        products=products,
        summary=summary,
        view='stock',
        sales=[],
        expenses=[],
        stock_requests=[],
        defect_requests=[],
        sales_labels=[],
        sales_data=[],
        expense_data=[],
        income_data=[],
        selected_date=zim_today(),
        report_page=False
    )

@admin_bp.route('/delete_product/<int:product_id>', methods=['POST'])
@login_required
def delete_product(product_id):
    if current_user.role != 'admin':
        abort(403)

    product = Product.query.get_or_404(product_id)
    sales_count = Sale.query.filter_by(product_id=product_id).count()
    stock_requests_count = StockRequest.query.filter_by(product_id=product_id).count()
    defect_requests_count = DefectRequests.query.filter_by(product_id=product_id).count()
    quotation_items_count = QuotationItem.query.filter_by(product_id=product_id).count()

    if sales_count > 0 or stock_requests_count > 0 or defect_requests_count > 0 or quotation_items_count > 0:
        flash(f"Cannot delete '{product.name}' as it has associated records.", "danger")
        return redirect(url_for('admin.admin_dashboard', view='products'))

    try:
        if product.image_filename:
            image_path = os.path.join(current_app.config['UPLOAD_FOLDER'], product.image_filename)
            if os.path.exists(image_path):
                os.remove(image_path)
        db.session.delete(product)
        db.session.commit()
        flash(f"Product '{product.name}' deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting product: {str(e)}", "danger")
        logger.error(f"Error deleting product ID {product_id}: {str(e)}")

    return redirect(url_for('admin.admin_dashboard', view='products'))

@admin_bp.route('/edit_product/<int:product_id>', methods=['POST'])
@login_required
def edit_product(product_id):
    if current_user.role != 'admin':
        abort(403)

    product = Product.query.get_or_404(product_id)
    data = request.get_json()
    name = data.get('name', '').strip()
    try:
        price = float(data.get('price', 0))
        cost_price = float(data.get('cost_price', 0))
        quantity = int(data.get('quantity', 0))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': "Invalid numeric value."}), 400

    existing_product = Product.query.filter(
        func.lower(Product.name) == func.lower(name),
        Product.id != product_id
    ).first()
    if existing_product:
        return jsonify({'success': False, 'message': f"Product name '{name}' is already in use."}), 400

    if not name or price < 0 or cost_price < 0 or quantity < 0:
        return jsonify({'success': False, 'message': "Please provide valid product details."}), 400

    try:
        product.name = name
        product.price = price
        product.cost_price = cost_price
        product.quantity = quantity
        db.session.commit()
        return jsonify({
            'success': True,
            'message': f"Product '{product.name}' updated successfully.",
            'product': {
                'id': product.id,
                'name': product.name,
                'price': f"{product.price:.2f}",
                'cost_price': f"{product.cost_price:.2f}",
                'quantity': product.quantity
            }
        })
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating product ID {product_id}: {str(e)}")
        return jsonify({'success': False, 'message': f"Error updating product: {str(e)}"}), 500

@admin_bp.route('/record_expense', methods=['POST'])
@login_required
def record_expense():
    if current_user.role != 'admin':
        abort(403)

    description = request.form.get('description', '').strip()
    try:
        amount = float(request.form.get('amount', 0))
    except ValueError:
        flash("Invalid expense amount.", "danger")
        return redirect(url_for('admin.admin_dashboard', view='expenses'))

    if not description or amount <= 0:
        flash("Please provide valid expense details.", "danger")
        return redirect(url_for('admin.admin_dashboard', view='expenses'))

    new_exp = Expense(description=description, amount=amount, recorded_by=current_user.id, timestamp=zim_now())
    db.session.add(new_exp)
    db.session.commit()

    update_cash_balance(zim_today(), expense_amount=amount)
    flash("Expense recorded.", "success")
    return redirect(url_for('admin.admin_dashboard', view='expenses'))

@admin_bp.route('/approve_stock_request/<int:request_id>', methods=['POST'])
@login_required
def approve_stock_request(request_id):
    if current_user.role != 'admin':
        abort(403)

    req = StockRequest.query.get_or_404(request_id)
    if req.status != 'pending':
        flash("Request already processed.", "warning")
        return redirect(url_for('admin.admin_dashboard', view='requests'))

    product = req.product
    if not product or req.quantity <= 0:
        flash("Invalid request or product.", "danger")
        return redirect(url_for('admin.admin_dashboard', view='requests'))

    product.quantity += req.quantity
    req.status = 'approved'
    req.approved_by = current_user.id
    req.approved_at = zim_now()

    try:
        db.session.commit()
        flash(f"Stock request for {product.name} approved.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error approving request: {str(e)}", "danger")
        logger.error(f"Error approving StockRequest ID {request_id}: {str(e)}")

    return redirect(url_for('admin.admin_dashboard', view='requests'))

@admin_bp.route('/reject_stock_request/<int:request_id>', methods=['POST'])
@login_required
def reject_stock_request(request_id):
    if current_user.role != 'admin':
        abort(403)

    req = StockRequest.query.get_or_404(request_id)
    if req.status != 'pending':
        flash("Request already processed.", "warning")
        return redirect(url_for('admin.admin_dashboard', view='requests'))

    req.status = 'rejected'
    req.approved_by = current_user.id
    req.rejected_at = zim_now()
    db.session.commit()
    flash(f"Stock request for {req.product.name} rejected.", "info")
    return redirect(url_for('admin.admin_dashboard', view='requests'))

@admin_bp.route('/approve_defect/<int:request_id>', methods=['POST'])
@login_required
def approve_defect(request_id):
    if current_user.role != 'admin':
        abort(403)

    defect = DefectRequests.query.get_or_404(request_id)
    if defect.status != 'Pending':
        flash("Request already processed.", "warning")
        return redirect(url_for('admin.admin_dashboard', view='defects'))

    product = defect.product
    if product.quantity < defect.quantity:
        flash(f"Not enough stock to approve {defect.quantity} units of {product.name}.", "danger")
        return redirect(url_for('admin.admin_dashboard', view='defects'))

    product.quantity -= defect.quantity
    defect.status = 'Approved'
    defect.approved_at = zim_now()
    db.session.commit()
    flash(f"Defect request for {product.name} approved.", "success")
    return redirect(url_for('admin.admin_dashboard', view='defects'))

@admin_bp.route('/reject_defect/<int:request_id>', methods=['POST'])
@login_required
def reject_defect(request_id):
    if current_user.role != 'admin':
        abort(403)

    defect = DefectRequests.query.get_or_404(request_id)
    if defect.status != 'Pending':
        flash("Request already processed.", "warning")
        return redirect(url_for('admin.admin_dashboard', view='defects'))

    defect.status = 'Rejected'
    defect.rejected_at = zim_now()
    db.session.commit()
    flash(f"Defect request for {defect.product.name} rejected.", "info")
    return redirect(url_for('admin.admin_dashboard', view='defects'))

@admin_bp.route('/return_defect/<int:request_id>', methods=['POST'])
@login_required
def return_defect(request_id):
    if current_user.role != 'admin':
        abort(403)

    defect = DefectRequests.query.get_or_404(request_id)
    if defect.status != 'Approved':
        flash("Only approved defects can be returned.", "warning")
        return redirect(url_for('admin.admin_dashboard', view='defects'))

    product = defect.product
    product.quantity += defect.quantity
    defect.status = 'Returned'
    defect.returned_at = zim_now()
    db.session.commit()
    flash(f"{defect.quantity} units of {product.name} returned to stock.", "success")
    return redirect(url_for('admin.admin_dashboard', view='defects'))

@admin_bp.route('/export-report-pdf', methods=['POST'])
@login_required
def export_report_pdf():
    if current_user.role != 'admin':
        abort(403)

    selected_date = request.form.get('selected_date', zim_now().strftime('%Y-%m-%d'))
    selected_date = validate_date(selected_date)
    if not selected_date:
        selected_date = zim_today()
        flash("Invalid date for PDF export, using today.", "warning")

    start, end = day_range(selected_date)

    sales = Sale.query.filter(Sale.timestamp.between(start, end)).all()
    expenses = Expense.query.filter(Expense.timestamp.between(start, end)).all()
    extra_incomes = ExtraIncome.query.filter(ExtraIncome.timestamp.between(start, end)).all()
    stock_requests = StockRequest.query.filter(StockRequest.timestamp.between(start, end)).all()
    defect_requests = DefectRequests.query.filter(DefectRequests.timestamp.between(start, end)).all()

    summary = calculate_cumulative_balances(selected_date)
    summary = {
        "opening_balance": round(summary[0], 2),
        "total_sales": round(summary[1], 2),
        "total_income": round(summary[2], 2),
        "total_expenses": round(summary[3], 2),
        "total_profit": round(summary[4], 2),
        "net_cash_flow": round(summary[5], 2),
        "net_profit": round(summary[6], 2),
        "closing_balance": round(summary[7], 2)
    }

    rendered = render_template(
        'report_pdf.html',
        summary=summary,
        sales=sales,
        expenses=expenses,
        extra_incomes=extra_incomes,
        stock_requests=stock_requests,
        defect_requests=defect_requests,
        current_range='Daily',
        total_sales=summary['total_sales'],
        total_expenses=summary['total_expenses'],
        total_profit=summary['total_profit'],
        date=selected_date.strftime('%Y-%m-%d'),
        selected_date=selected_date.strftime('%Y-%m-%d'),
        logo_path=url_for('static', filename='images/cthlogo.jpg', _external=True)
    )

    pdf = BytesIO()
    pisa_status = pisa.CreatePDF(BytesIO(rendered.encode('utf-8')), dest=pdf)
    if pisa_status.err:
        flash("Error generating PDF.", "danger")
        return redirect(url_for('admin.admin_dashboard', view='overview'))

    pdf.seek(0)
    filename = f"daily_report_{selected_date.strftime('%Y-%m-%d')}.pdf"
    return send_file(
        pdf,
        as_attachment=True,
        download_name=filename,
        mimetype='application/pdf'
    )

@admin_bp.route('/quotations', methods=['GET', 'POST'])
@login_required
def quotations():
    if request.method == 'POST':
        try:
            customer, items, discount = quotes.parse_quote_form(request.form)
            q = quotes.save_quotation(current_user, customer, items, discount)
            flash(f"Quotation {q.quotation_number} created.", "success")
        except PricingError as exc:
            flash(str(exc), "danger")
        except Exception:
            db.session.rollback()
            logger.exception("Error creating quotation")
            flash("The quotation could not be saved. Please try again.", "danger")
        return redirect(url_for('admin.quotations'))

    products = Product.query.order_by(Product.name).all()
    everything = Quotation.query.order_by(Quotation.created_at.desc()).all()
    return render_template('quotations.html', products=products, quotations=everything, edit=None)


@admin_bp.route('/quotations/<int:quotation_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_quotation(quotation_id):
    quotation = Quotation.query.get_or_404(quotation_id)
    if quotation.converted:
        flash("A converted quotation can't be edited.", "warning")
        return redirect(url_for('admin.quotations'))

    if request.method == 'POST':
        try:
            customer, items, discount = quotes.parse_quote_form(request.form)
            quotes.save_quotation(current_user, customer, items, discount, quotation=quotation)
            flash(f"Quotation {quotation.quotation_number} updated.", "success")
            return redirect(url_for('admin.quotations'))
        except PricingError as exc:
            flash(str(exc), "danger")
        except Exception:
            db.session.rollback()
            logger.exception("Error updating quotation")
            flash("The quotation could not be saved. Please try again.", "danger")
        return redirect(url_for('admin.edit_quotation', quotation_id=quotation.id))

    products = Product.query.order_by(Product.name).all()
    everything = Quotation.query.order_by(Quotation.created_at.desc()).all()
    return render_template('quotations.html', products=products, quotations=everything,
                           edit=quotes.quotation_edit_payload(quotation), edit_id=quotation.id,
                           edit_number=quotation.quotation_number)

@admin_bp.route('/quotations/<int:quotation_id>/convert', methods=['POST'])
@login_required
def convert_quotation(quotation_id):
    quotation = Quotation.query.get_or_404(quotation_id)
    try:
        result = quotes.convert_to_sale(
            quotation, current_user,
            customer_email=request.form.get('customer_email', ''),
            customer_phone=request.form.get('customer_phone', ''),
            delivery_fee=request.form.get('delivery_fee') or 0)
    except (PricingError, SaleError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for('admin.quotations'))
    except Exception:
        db.session.rollback()
        logger.exception("Error converting quotation")
        flash("Something went wrong converting that quotation. Nothing was changed.", "danger")
        return redirect(url_for('admin.quotations'))

    receipt = result['receipt']
    flash(f"Quotation {quotation.quotation_number} converted. Receipt #{receipt.receipt_number} recorded.", "success")
    pdf = receipt_pdf(receipt, result['items'], current_user.full_name)
    if pdf is None:
        flash("The receipt PDF could not be created: reprint it from Sales.", "warning")
        return redirect(url_for('admin.quotations'))
    return send_file(pdf, as_attachment=True, download_name=f"receipt_{receipt.receipt_number}.pdf",
                     mimetype='application/pdf')

@admin_bp.route('/download_quotation/<int:quotation_id>')
@login_required
def download_quotation(quotation_id):
    quotation = Quotation.query.get_or_404(quotation_id)
    pdf = render_pdf('print_quotation.html', quotation=quotation,
                     business_name=current_app.config['BUSINESS_NAME'],
                     business_address=current_app.config['BUSINESS_ADDRESS'],
                     printed_by=current_user.full_name,
                     logo_path=url_for('static', filename='images/cthlogo.jpg'))
    if pdf is None:
        flash("Failed to generate the quotation PDF.", "danger")
        return redirect(url_for('admin.quotations'))
    return send_file(pdf, as_attachment=True, download_name=f"quotation_{quotation.quotation_number}.pdf",
                     mimetype='application/pdf')


@admin_bp.route('/receipt/<int:receipt_id>/pdf')
@login_required
def reprint_receipt(receipt_id):
    receipt = Receipt.query.get_or_404(receipt_id)
    pdf = receipt_pdf(receipt, receipt_items(receipt), current_user.full_name)
    if pdf is None:
        flash("Could not create the receipt PDF.", "danger")
        return redirect(url_for('admin.view_sales'))
    return send_file(pdf, as_attachment=True, download_name=f"receipt_{receipt.receipt_number}.pdf",
                     mimetype='application/pdf')

@admin_bp.route('/download_catalog')
@login_required
def download_catalog():
    if current_user.role != 'admin':
        abort(403)

    try:
        products = Product.query.all()
        for product in products:
            try:
                if product.image_filename and product.image_filename.strip():
                    image_path = os.path.join(current_app.root_path, 'static', 'Uploads', product.image_filename.strip())
                    if os.path.exists(image_path):
                        product.image_path = image_path
                    else:
                        product.image_path = None
                        flash(f"Image file not found for product {product.name}: {product.image_filename}", "warning")
                else:
                    product.image_path = None
            except AttributeError:
                product.image_path = None
                flash(f"Invalid image filename for product {product.name}", "warning")

        pdf = generate_pdf('catalog.html', {
            'products': products,
            'business_name': "Crown Tech Hub",
            'business_address': "Karimapondo Mall, Corner Robert Mugabe & Leopold Takawira, 1st Floor, Workstation 25",
            'business_contact': "Phone: +263774659090 | +263782896646 | +263778329323 | Email: crowntechzw@gmail.com",
            'logo_path': url_for('static', filename='images/cthlogo.jpg', _external=True),
            'current_year': zim_now().year
        })

        return send_file(
            pdf,
            as_attachment=True,
            download_name="catalog.pdf",
            mimetype='application/pdf'
        )
    except Exception as e:
        flash(f"Error generating PDF: {str(e)}", "danger")
        return redirect(url_for('admin.admin_dashboard', view='products'))

@admin_bp.route('/download_stock_list')
@login_required
def download_stock_list():
    if current_user.role != 'admin':
        abort(403)

    try:
        products = Product.query.order_by(Product.quantity.desc()).all()
        for product in products:
            try:
                if product.image_filename and product.image_filename.strip():
                    image_path = os.path.join(current_app.root_path, 'static', 'Uploads', product.image_filename.strip())
                    if os.path.exists(image_path):
                        product.image_path = image_path
                    else:
                        product.image_path = None
                        flash(f"Image file not found for product {product.name}: {product.image_filename}", "warning")
                else:
                    product.image_path = None
            except AttributeError:
                product.image_path = None
                flash(f"Invalid image filename for product {product.name}", "warning")

        pdf = generate_pdf('stock_list.html', {
            'products': products,
            'business_name': "Crown Tech Hub",
            'business_address': "Karimapondo Mall, Corner Robert Mugabe & Leopold Takawira, 1st Floor, Workstation 25",
            'business_contact': "Phone: +263774659090 | +263782896646 | +263778329323 | Email: crowntechzw@gmail.com",
            'logo_path': url_for('static', filename='images/cthlogo.jpg', _external=True),
            'current_year': zim_now().year,
            'current_date': zim_now().strftime('%Y-%m-%d')
        })

        return send_file(
            pdf,
            as_attachment=True,
            download_name="stock_list.pdf",
            mimetype='application/pdf'
        )
    except Exception as e:
        flash(f"Error generating stock list PDF: {str(e)}", "danger")
        return redirect(url_for('admin.admin_dashboard', view='products'))



# inside your admin routes
@admin_bp.route('/view_sales', methods=['GET'])
@login_required
def view_sales():
    if current_user.role != 'admin':
        abort(403)

    today = zim_now().date()
    date_filter = request.args.get('date', today.strftime('%Y-%m-%d'))
    search_query = request.args.get('search', '').strip()

    # --- Base query ---
    query = Sale.query.join(Product).outerjoin(Receipt)

    # --- Search first: if search query present, ignore date filter ---
    if search_query:
        query = query.filter(
            db.or_(
                Receipt.receipt_number.ilike(f"%{search_query}%"),
                Product.name.ilike(f"%{search_query}%")
            )
        )
        flash(f"Showing results for search: '{search_query}'", "info")
    else:
        # --- Otherwise, default to today's sales ---
        try:
            selected_date = datetime.strptime(date_filter, '%Y-%m-%d')
            start = selected_date.replace(hour=0, minute=0, second=0, microsecond=0)
            end = selected_date.replace(hour=23, minute=59, second=59, microsecond=999999)
            query = query.filter(Sale.timestamp.between(start, end))
        except ValueError:
            flash("Invalid date format provided.", "warning")

    # --- Sorting newest first ---
    query = query.order_by(Sale.timestamp.desc())

    # --- Pagination ---
    page = request.args.get('page', 1, type=int)
    per_page = 50
    sales = query.paginate(page=page, per_page=per_page, error_out=False)

    return render_template(
        'admin_view_sales.html',
        sales=sales.items,
        pagination=sales,
        date_filter=date_filter,
        search_query=search_query,
        today=today
    )


@admin_bp.route('/reverse_sale/<int:sale_id>', methods=['POST'])
@login_required
def reverse_sale(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    product = sale.product

    if not product:
        flash("Product not found for this sale.", "danger")
        return redirect(url_for('admin.admin_dashboard', view='overview'))

    sale_date = sale.timestamp.date()

    # Step 1 — Restore stock
    product.quantity += sale.quantity
    db.session.add(product)

    # Step 2 — Adjust cash balance on sale day
    balance = DailyCashBalance.query.filter_by(date=sale_date).first()
    if not balance:
        balance = DailyCashBalance(date=sale_date, opening_balance=0.0, closing_balance=0.0)
        db.session.add(balance)

    amount_reversed = sale.total_price
    balance.closing_balance = max(balance.closing_balance - amount_reversed, 0.0)
    balance.updated_at = zim_now()
    db.session.add(balance)

    # Step 3 — Log reversal
    reversal = SaleReversal(
        sale_id=sale.id,
        product_id=product.id,
        quantity=sale.quantity,
        amount_reversed=sale.total_price,
        reason=request.form.get('reason') or "Manual reversal",
        reversed_by=current_user.id
    )
    db.session.add(reversal)

    # Step 4 — Delete the sale record
    db.session.delete(sale)

    try:
        db.session.commit()

        # ✅ Step 5 — Recompute balances to fix all later days
        backfill_balances_until_today()

        flash(
            f"✅ Sale reversed successfully. ${amount_reversed:.2f} deducted from {sale_date} balance and {sale.quantity} units restored.",
            "success"
        )
    except Exception as e:
        db.session.rollback()
        flash(f"❌ Error reversing sale: {str(e)}", "danger")

    return redirect(url_for('admin.admin_dashboard', view='overview'))

import secrets
from werkzeug.security import generate_password_hash

@admin_bp.route("/users/create", methods=["POST"])
@login_required
def create_user():
    if current_user.role != "admin":
        return redirect(url_for("auth.login"))

    email = request.form["email"].lower()
    full_name = request.form["full_name"]
    role = request.form["role"]
    password = request.form["password"]

    if role not in ["admin", "cashier"]:
        flash("Invalid role.", "danger")
        return redirect(url_for("admin.users"))

    if User.query.filter_by(username=email).first():
        flash("User already exists.", "warning")
        return redirect(url_for("admin.users"))

    if len(password) < 8:
        flash("The temporary password must be at least 8 characters.", "danger")
        return redirect(url_for("admin.users"))

    user = User(
        username=email,
        full_name=full_name,
        role=role,
        password=generate_password_hash(password),
        email_verified=True,          # an admin vouches for staff
        must_change_password=True,    # they choose their own at first sign-in
    )

    db.session.add(user)
    db.session.commit()

    try:
        from ..services.mailer import abs_url, send_template
        send_template(email, "Your Crown Tech Hub staff account", "staff_welcome", name=full_name, role=role,
                      link=abs_url(url_for("auth.login")))
    except Exception:
        logger.exception("Could not queue the staff welcome email")

    flash("User created. They will be asked to choose their own password at first sign-in.", "success")
    return redirect(url_for("admin.users"))

@admin_bp.route("/users")
@login_required
def users():
    if current_user.role != "admin":
        return redirect(url_for("auth.login"))

    # Staff only - online customers and the internal "Online shop" account are not managed here.
    users = (User.query.filter(User.role.in_(["admin", "cashier", "disabled"]))
             .order_by(User.role, User.full_name).all())
    return render_template("admin_users.html", users=users)
@admin_bp.route("/users/toggle/<int:user_id>", methods=["POST"])
@login_required
def toggle_user(user_id):
    if current_user.role != "admin":
        return redirect(url_for("auth.login"))

    user = User.query.get_or_404(user_id)
    if user.role in ("customer", "system"):
        flash("Customer accounts are not managed here.", "warning")
        return redirect(url_for("admin.users"))

    # Protect last admin
    if user.role == "admin" and User.query.filter_by(role="admin").count() <= 1:
        flash("Cannot disable the last admin.", "danger")
        return redirect(url_for("admin.users"))

    # Toggle role
    user.role = "disabled" if user.role != "disabled" else "cashier"

    db.session.commit()
    flash("User status updated.", "success")
    return redirect(url_for("admin.users"))

@admin_bp.route("/users/reset-password/<int:user_id>", methods=["POST"])
@login_required
def reset_password(user_id):
    if current_user.role != "admin":
        return redirect(url_for("auth.login"))

    user = User.query.get_or_404(user_id)
    if user.role == "system":
        flash("That account cannot be changed.", "warning")
        return redirect(url_for("admin.users"))

    new_password = secrets.token_urlsafe(8)
    user.password = generate_password_hash(new_password)
    user.must_change_password = True

    db.session.commit()

    flash(f"Temporary password: {new_password} (they will be asked to change it at next sign-in)", "info")
    return redirect(url_for("admin.users"))
@admin_bp.route("/users/unlock-profile/<int:user_id>", methods=["POST"])
@login_required
def unlock_profile(user_id):
    if current_user.role != "admin":
        return redirect(url_for("auth.login"))

    profile = UserProfile.query.filter_by(user_id=user_id).first_or_404()
    profile.editable = True
    db.session.commit()
    flash(f"{profile.user.full_name}'s profile is now editable.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route('/email-setup', methods=['GET', 'POST'])
@login_required
def email_setup():
    """Shows how email is configured and sends a real test message, reporting the exact result."""
    import os
    from ..security import client_key, record_failure, throttled
    from ..services.mailer import send_email_sync

    cfg = current_app.config
    if request.method == 'POST':
        key = client_key('emailtest')
        if throttled(key, limit=10, window=3600):
            flash("Too many test emails this hour. Try again later.", "warning")
        else:
            record_failure(key)
            to = (request.form.get('to') or current_user.username).strip()
            ok, message = send_email_sync(
                to, f"{cfg['BUSINESS_NAME']} email test",
                f"This is a test email from {cfg['BUSINESS_NAME']}.\n\nIf you can read this, email sending works.\n",
                f"<p>This is a test email from <strong>{cfg['BUSINESS_NAME']}</strong>.</p><p>If you can read this, email sending works.</p>")
            flash(message, 'success' if ok else 'danger')
        return redirect(url_for('admin.email_setup'))

    on_render = os.environ.get('RENDER', '').lower() == 'true'
    behind_proxy = os.environ.get('BEHIND_PROXY', '').lower() in ('1', 'true', 'yes')
    if cfg['MAIL_USE_SSL']:
        encryption = 'SSL from the start (port 465 style)'
    elif cfg['MAIL_USE_TLS']:
        encryption = 'STARTTLS'
    else:
        encryption = 'None'
    warnings = []
    if not cfg['MAIL_SERVER']:
        warnings.append("MAIL_SERVER is empty: emails are NOT being sent (they are saved in instance/outbox/ for testing).")
    if on_render and cfg['MAIL_SERVER'] and cfg['MAIL_PORT'] in (25, 465, 587):
        warnings.append("Render's free plan blocks outbound ports 25, 465 and 587. If the test times out, use port 80 "
                        "with MAIL_USE_TLS=true and MAIL_USE_SSL=false, or upgrade the Render instance.")
    if cfg['MAIL_SERVER'] and not cfg['MAIL_PASSWORD']:
        warnings.append("MAIL_PASSWORD is empty. Set it to the SMTP password created for the sender address.")
    if not cfg['PUBLIC_BASE_URL']:
        warnings.append("PUBLIC_BASE_URL is not set. Links in emails will use whatever address the request arrived on. "
                        "Set it to your site address, e.g. https://crowntechhub.co.zw")
    elif not cfg['PUBLIC_BASE_URL'].startswith('https://'):
        warnings.append("PUBLIC_BASE_URL should start with https:// so email links are secure.")
    if on_render and not behind_proxy:
        warnings.append("BEHIND_PROXY is not set to true. On Render, every visitor then looks like the same address, so "
                        "login limits are shared by everyone and some links may use http. Set BEHIND_PROXY=true.")
    status = [
        ("Mode", "Sending through SMTP" if cfg['MAIL_SERVER'] else "Development: saved to instance/outbox/ only"),
        ("Server", f"{cfg['MAIL_SERVER'] or '-'}:{cfg['MAIL_PORT']}"),
        ("Encryption", encryption),
        ("Login", cfg['MAIL_USERNAME'] or '-'),
        ("Password", "set" if cfg['MAIL_PASSWORD'] else "not set"),
        ("Sent from", f"{cfg['MAIL_FROM_NAME']} <{cfg['MAIL_FROM']}>"),
        ("Site address", cfg['PUBLIC_BASE_URL'] or "not set"),
        ("Google sign-in", "configured" if cfg['GOOGLE_CLIENT_ID'] and cfg['GOOGLE_CLIENT_SECRET'] else "not configured"),
    ]
    return render_template('admin_email_setup.html', status=status, warnings=warnings,
                           default_to=current_user.username)


@admin_bp.before_request
def require_admin():
    """Blanket rule for this whole blueprint: admins only. (Route-level checks stay as a second lock.)"""
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    if current_user.role != 'admin':
        abort(403)
