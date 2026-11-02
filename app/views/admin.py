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

# ----------------------------------------------------------------------
# TIMEZONE HELPER – Zimbabwe (UTC+2, no DST)
# ----------------------------------------------------------------------
ZIM_OFFSET = timedelta(hours=2)  # Zimbabwe is UTC+2

def get_utc_range_for_zim_date(zim_date: date):
    """
    Returns the UTC datetime range covering 00:00:00 to 23:59:59.999999
    Zimbabwe local time for the given business date.
    """
    start_local = datetime.combine(zim_date, datetime.min.time())
    end_local   = datetime.combine(zim_date, datetime.max.time())
    start_utc   = start_local - ZIM_OFFSET
    end_utc     = end_local   - ZIM_OFFSET
    return start_utc, end_utc

# ----------------------------------------------------------------------
# Backfill & Balance Correction – Uses Zimbabwe day ranges
# ----------------------------------------------------------------------
def backfill_balances_until_today():
    """Recompute and correct DailyCashBalance records up to today using Zimbabwe business days."""
    today = datetime.utcnow().date()
    all_balances = DailyCashBalance.query.order_by(DailyCashBalance.date.asc()).all()

    if not all_balances:
        first = DailyCashBalance(date=today, opening_balance=0.0, closing_balance=0.0, updated_at=datetime.utcnow())
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
            balance.updated_at = datetime.utcnow()
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
            updated_at=datetime.utcnow()
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
    try:
        balance = DailyCashBalance.query.filter_by(date=local_date).first()
        if not balance:
            prev_day = local_date - timedelta(days=1)
            prev = DailyCashBalance.query.filter_by(date=prev_day).first()
            opening = round(prev.closing_balance if prev else 0.0, 2)
            balance = DailyCashBalance(date=local_date, opening_balance=opening, closing_balance=opening)
            db.session.add(balance)

        net_flow = sales_amount + income_amount - expense_amount
        balance.closing_balance = round(balance.opening_balance + net_flow, 2)
        balance.updated_at = datetime.utcnow()
        db.session.commit()
        logger.debug(f"Updated balance {local_date}: +{net_flow:.2f} → Close={balance.closing_balance:.2f}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating cash balance {local_date}: {str(e)}")

def calculate_cumulative_balances(selected_date):
    try:
        selected_date = selected_date if isinstance(selected_date, date) else date.today()
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
    selected_date_str = request.form.get("selected_date", request.args.get("selected_date", datetime.utcnow().strftime('%Y-%m-%d')))
    range_type = request.form.get("range_type", request.args.get("range_type", "daily"))  # NEW: daily or monthly

    try:
        base_date = datetime.strptime(selected_date_str, "%Y-%m-%d").date()
    except ValueError:
        base_date = datetime.utcnow().date()
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
    cutoff_date = datetime.utcnow() - timedelta(days=3)  # longer for monthly
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
                "admin_report_pdf.html",
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
                logo_path=url_for("static", filename="images/cthlogo.jpg", _external=True)
            )
            buffer = BytesIO()
            pisa.CreatePDF(html, dest=buffer)
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
    summary = calculate_cumulative_balances(date.today())
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
        selected_date=date.today(),
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

    new_exp = Expense(description=description, amount=amount, recorded_by=current_user.id, timestamp=datetime.utcnow())
    db.session.add(new_exp)
    db.session.commit()

    update_cash_balance(date.today(), expense_amount=amount)
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
    req.approved_at = datetime.utcnow()

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
    req.rejected_at = datetime.utcnow()
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
    defect.approved_at = datetime.utcnow()
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
    defect.rejected_at = datetime.utcnow()
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
    defect.returned_at = datetime.utcnow()
    db.session.commit()
    flash(f"{defect.quantity} units of {product.name} returned to stock.", "success")
    return redirect(url_for('admin.admin_dashboard', view='defects'))

@admin_bp.route('/export-report-pdf', methods=['POST'])
@login_required
def export_report_pdf():
    if current_user.role != 'admin':
        abort(403)

    selected_date = request.form.get('selected_date', datetime.utcnow().strftime('%Y-%m-%d'))
    selected_date = validate_date(selected_date)
    if not selected_date:
        selected_date = date.today()
        flash("Invalid date for PDF export, using today.", "warning")

    start = datetime.combine(selected_date, datetime.min.time()) - timedelta(hours=8)
    end = datetime.combine(selected_date, datetime.max.time()) - timedelta(hours=8)

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
    if current_user.role != 'admin':
        abort(403)

    if request.method == 'POST':
        try:
            customer_name = request.form.get('customer_name', '').strip()
            discount = float(request.form.get('discount_value', 0))
            items_json = request.form.get('items')
            items = json.loads(items_json) if items_json else []

            if not items or not customer_name:
                flash("Please provide customer name and at least one item.", "danger")
                return redirect(url_for('admin.quotations'))

            for item in items:
                product_id = item.get('product_id')
                if not product_id:
                    flash(f"Invalid item: {item.get('product_name', 'Unknown')} must be associated with a product.", "danger")
                    return redirect(url_for('admin.quotations'))
                product = Product.query.get(product_id)
                if not product:
                    flash(f"Product ID {product_id} does not exist.", "danger")
                    return redirect(url_for('admin.quotations'))

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
                qitem = QuotationItem(
                    quotation_id=quotation.id,
                    product_id=product_id,
                    product_name=product.name,
                    quantity=int(item.get('quantity', 1)),
                    unit_price=float(item.get('unit_price')),
                    total_price=float(item.get('quantity', 1)) * float(item.get('unit_price'))
                )
                db.session.add(qitem)

            db.session.commit()
            flash(f"Quotation {quotation_number} created successfully!", "success")
            return redirect(url_for('admin.quotations'))

        except (ValueError, json.JSONDecodeError) as e:
            db.session.rollback()
            flash(f"Error creating quotation: Invalid input data.", "danger")
            return redirect(url_for('admin.quotations'))
        except Exception as e:
            db.session.rollback()
            flash(f"Error creating quotation: {str(e)}", "danger")
            return redirect(url_for('admin.quotations'))

    products = Product.query.all()
    quotations = Quotation.query.order_by(Quotation.created_at.desc()).all()
    summary = calculate_cumulative_balances(date.today())
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
        quotations=quotations,
        view='quotations',
        summary=summary,
        report_page=False,
        selected_date=date.today()
    )

@admin_bp.route('/quotations/<int:quotation_id>/convert', methods=['POST'])
@login_required
def convert_quotation(quotation_id):
    if current_user.role != 'admin':
        abort(403)

    try:
        quotation = Quotation.query.get_or_404(quotation_id)
        if quotation.converted:
            flash("This quotation has already been converted to a sale.", "warning")
            return redirect(url_for('admin.quotations'))

        for q_item in quotation.items:
            if not q_item.product_id:
                flash(f"Invalid item: {q_item.product_name} must be associated with a product.", "danger")
                return redirect(url_for('admin.quotations'))
            product = Product.query.get(q_item.product_id)
            if not product:
                flash(f"Product '{q_item.product_name}' no longer exists.", "danger")
                return redirect(url_for('admin.quotations'))
            if product.quantity < q_item.quantity:
                flash(f"Not enough stock for '{product.name}'. Requested: {q_item.quantity}, Available: {product.quantity}.", "danger")
                return redirect(url_for('admin.quotations'))
            if product.cost_price is None:
                logger.warning(f"Product {product.name} (ID: {q_item.product_id}) has None cost_price. Setting to 0.0 for profit calculation.")
                product.cost_price = 0.0

        sold_items = []
        subtotal = 0
        for q_item in quotation.items:
            product = Product.query.get(q_item.product_id)
            total_price = q_item.unit_price * q_item.quantity
            cost_price = float(product.cost_price) if product.cost_price is not None else 0.0
            profit = (q_item.unit_price - cost_price) * q_item.quantity
            logger.debug(f"Converting quotation item: product={product.name}, unit_price={q_item.unit_price}, cost_price={cost_price}, quantity={q_item.quantity}, profit={profit}")
            subtotal += total_price
            sold_items.append({
                'name': product.name,
                'price': q_item.unit_price,
                'quantity': q_item.quantity,
                'total_price': total_price,
                'profit': profit
            })

        tax_rate = 0.0
        tax = subtotal * tax_rate
        total = subtotal - (quotation.discount or 0.0) + tax

        today = datetime.utcnow().strftime('%Y%m%d')
        prefix = f"SALE-{today}-"
        last_receipt = Receipt.query.filter(Receipt.receipt_number.like(f"{prefix}%")).order_by(Receipt.receipt_number.desc()).first()
        receipt_number = f"{prefix}{int(last_receipt.receipt_number.split('-')[-1]) + 1:04d}" if last_receipt else f"{prefix}0001"

        max_retries = 5
        retry_count = 0
        while Receipt.query.filter_by(receipt_number=receipt_number).first() and retry_count < max_retries:
            retry_count += 1
            last_number = int(receipt_number.split('-')[-1]) + 1
            receipt_number = f"{prefix}{last_number:04d}"

        if retry_count >= max_retries:
            flash("Unable to generate a unique receipt number.", "danger")
            return redirect(url_for('admin.quotations'))

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

        for q_item in quotation.items:
            product = Product.query.get(q_item.product_id)
            total_price = q_item.unit_price * q_item.quantity
            cost_price = float(product.cost_price) if product.cost_price is not None else 0.0
            profit = (q_item.unit_price - cost_price) * q_item.quantity
            sale = Sale(
                product_id=q_item.product_id,
                quantity=q_item.quantity,
                total_price=total_price,
                profit=profit,
                cashier_id=current_user.id,
                timestamp=datetime.utcnow(),
                receipt_id=receipt.id
            )
            db.session.add(sale)
            product.quantity -= q_item.quantity
            logger.debug(f"Created sale: product={product.name}, total_price={total_price}, profit={profit}")

        quotation.converted = True
        db.session.commit()

        update_cash_balance(date.today(), sales_amount=total)

        pdf = generate_pdf('receipt_template.html', {
            'items': sold_items,
            'subtotal': subtotal,
            'tax': tax,
            'total': total,
            'discount': quotation.discount or 0.0,
            'receipt_number': receipt_number,
            'customer_name': quotation.customer_name,
            'customer_email': receipt.customer_email,
            'customer_phone': receipt.customer_phone,
            'printed_by': current_user.full_name,
            'business_name': 'Crown Tech Hub',
            'business_address': 'Karimapondo Mall, Corner Robert Mugabe & Leopold Takawira, 1st Floor, Workstation 25, Harare, Zimbabwe',
            'logo_path': url_for('static', filename='images/cthlogo.jpg', _external=True),
            'current_year': datetime.utcnow().year
        })

        flash(f"Quotation {quotation.quotation_number} converted to sale. Receipt #{receipt_number} generated.", "success")
        return send_file(
            pdf,
            as_attachment=True,
            download_name=f"receipt_{receipt_number}.pdf",
            mimetype='application/pdf'
        )

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error converting quotation ID {quotation_id}: {str(e)}")
        flash(f"Error converting quotation: {str(e)}", "danger")
        return redirect(url_for('admin.quotations'))

@admin_bp.route('/download_quotation/<int:quotation_id>')
@login_required
def download_quotation(quotation_id):
    if current_user.role != 'admin':
        abort(403)

    try:
        quotation = Quotation.query.get_or_404(quotation_id)
        pdf = generate_pdf('print_quotation.html', {
            'quotation': quotation,
            'business_name': "Crown Tech Hub",
            'business_address': "Karimapondo Mall, Corner Robert Mugabe & Leopold Takawira, 1st Floor, Workstation 25",
            'printed_by': current_user.full_name,
            'logo_path': url_for('static', filename='images/cthlogo.jpg', _external=True)
        })

        return send_file(
            pdf,
            as_attachment=True,
            download_name=f"quotation_{quotation.quotation_number}.pdf",
            mimetype='application/pdf'
        )

    except Exception as e:
        flash(f"Error generating PDF: {str(e)}", "danger")
        return redirect(url_for('admin.quotations'))

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
            'current_year': datetime.utcnow().year
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
            'current_year': datetime.utcnow().year,
            'current_date': datetime.utcnow().strftime('%Y-%m-%d')
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

    today = datetime.utcnow().date()
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
    balance.updated_at = datetime.utcnow()
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

    user = User(
        username=email,
        full_name=full_name,
        role=role,
        password=generate_password_hash(password)
    )

    db.session.add(user)
    db.session.commit()

    flash("User created successfully.", "success")
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

    db.session.commit()

    flash(f"Temporary password: {new_password}", "info")
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

