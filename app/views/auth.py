from flask import abort, current_app, send_file, Blueprint, render_template, redirect, request, flash, url_for
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash
from ..models import User, UserProfile
from .. import db
from ..security import verify_csrf, clear_failures, client_key, record_failure, safe_next, throttled
from werkzeug.utils import secure_filename
import secrets
import os
from datetime import datetime  # Added for date parsing

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(_home_for(current_user))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        key = client_key(email)
        if throttled(key):
            flash("Too many failed attempts. Please wait a few minutes and try again.", "danger")
            return render_template('login.html'), 429

        user = User.query.filter_by(username=email).first()
        if user and user.role in ("disabled", "system"):
            flash("Account is disabled.", "danger")
            return redirect(url_for("auth.login"))
        if user and check_password_hash(user.password, password):
            clear_failures(key)
            login_user(user)
            flash(f'Welcome back, {user.full_name}!', 'success')
            return redirect(safe_next(request.args.get('next'), _home_for(user)))
        record_failure(key)
        flash('Invalid credentials.', 'danger')

    return render_template('login.html')


def _home_for(user):
    if user.role == 'admin':
        return url_for('admin.admin_dashboard')
    if user.role == 'cashier':
        return url_for('cashier.cashier_dashboard')
    return url_for('shop.account')


@auth_bp.route('/logout')
@login_required
def logout():
    was_customer = current_user.role == 'customer'
    logout_user()
    flash("You have been logged out.")
    return redirect(url_for('shop.home') if was_customer else url_for('auth.login'))


@auth_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if current_user.role == 'customer':
        return redirect(url_for('shop.account'))
    # Ensure UserProfile exists
    profile = current_user.profile
    if not profile:
        profile = UserProfile(user_id=current_user.id, editable=True)
        db.session.add(profile)
        db.session.commit()

    # Handle password change (from modal)
    if request.method == "POST" and "old_password" in request.form:
        old_password = request.form["old_password"]
        new_password = request.form["new_password"]
        confirm_password = request.form["confirm_password"]

        if not check_password_hash(current_user.password, old_password):
            flash("Old password is incorrect.", "danger")
            return redirect(url_for("auth.profile"))

        if len(new_password) < 8:
            flash("Choose a new password with at least 8 characters.", "danger")
            return redirect(url_for("auth.profile"))

        if new_password != confirm_password:
            flash("New passwords do not match.", "danger")
            return redirect(url_for("auth.profile"))

        current_user.password = generate_password_hash(new_password)
        db.session.commit()
        flash("Password changed successfully!", "success")
        return redirect(url_for("auth.profile"))

    # Handle profile update (main form with editable fields)
    elif request.method == "POST":
        if not profile.editable:
            flash("Profile is locked. Contact admin to unlock.", "warning")
            return redirect(url_for("auth.profile"))

        # Handle Date of Birth safely
        dob_str = request.form.get("dob")
        if dob_str:
            try:
                profile.dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid date format for Date of Birth.", "danger")
                return redirect(url_for("auth.profile"))
        else:
            profile.dob = None  # Allow empty DOB

        # Other fields - store None instead of string "None"
        profile.id_number = request.form.get("id_number") or None
        profile.address = request.form.get("address") or None
        profile.sex = request.form.get("sex")
        profile.phone_number = request.form.get("phone_number") or None
        profile.qualifications = request.form.get("qualifications") or None
        profile.next_of_kin = request.form.get("next_of_kin") or None
        profile.next_of_kin_contact = request.form.get("next_of_kin_contact") or None
        profile.next_of_kin_address = request.form.get("next_of_kin_address") or None

        # Contract file upload - stored privately (not under /static) and served only to the
        # owner or an admin through auth.contract.
        if "contract_file" in request.files:
            file = request.files["contract_file"]
            if file and file.filename:  # Only save if a file was actually selected
                ext = os.path.splitext(file.filename)[1].lower()
                if ext not in CONTRACT_EXTENSIONS:
                    flash("Contracts must be a PDF, image or Word document.", "danger")
                    return redirect(url_for("auth.profile"))
                os.makedirs(contracts_dir(), exist_ok=True)
                filename = f"{current_user.id}_{secrets.token_hex(6)}{ext}"
                file.save(os.path.join(contracts_dir(), filename))
                remove_contract_file(profile.contract_file)   # replace, don't pile up old copies
                profile.contract_file = filename

        # Lock the profile after successful update
        profile.editable = False
        db.session.commit()
        flash("Profile updated successfully!", "success")
        return redirect(url_for("auth.profile"))

    # GET request - render the profile page
    return render_template("profile.html", user=current_user, profile=profile)


# ----------------------------------------------------------------------
# Private contract files
# ----------------------------------------------------------------------
CONTRACT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}


def contracts_dir():
    return os.path.join(current_app.instance_path, "contracts")


def remove_contract_file(stored_name):
    if not stored_name:
        return
    path = os.path.join(contracts_dir(), os.path.basename(stored_name))
    if os.path.isfile(path):
        os.remove(path)


@auth_bp.route("/contract/<int:user_id>")
@login_required
def contract(user_id):
    """Only the contract's owner or an admin may open it."""
    if current_user.id != user_id and current_user.role != "admin":
        abort(403)
    profile = UserProfile.query.filter_by(user_id=user_id).first_or_404()
    if not profile.contract_file:
        abort(404)
    path = os.path.join(contracts_dir(), os.path.basename(profile.contract_file))
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=False)


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password_first():
    """Shown to staff whose password was set/reset by an admin, until they choose their own."""
    if not current_user.must_change_password:
        return redirect(_home_for(current_user))
    if request.method == "POST":
        verify_csrf()
        new, confirm = request.form.get("new_password", ""), request.form.get("confirm_password", "")
        if len(new) < 8:
            flash("Choose a password with at least 8 characters.", "danger")
        elif new != confirm:
            flash("The two passwords don't match.", "danger")
        elif check_password_hash(current_user.password, new):
            flash("Choose a different password from the temporary one.", "danger")
        else:
            current_user.password = generate_password_hash(new)
            current_user.must_change_password = False
            db.session.commit()
            flash("Password updated. Welcome aboard!", "success")
            return redirect(_home_for(current_user))
    return render_template("auth/change_password.html")
