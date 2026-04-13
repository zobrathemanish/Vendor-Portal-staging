#auth_routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from extensions import login_manager
from models.user import User

auth_bp = Blueprint("auth", __name__)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return _role_redirect(current_user)

    if request.method == "POST":
        username = request.form.get("userid")
        password = request.form.get("password")

        user = User.query.filter_by(username=username).first()

        if user and user.is_active and user.check_password(password):
            # 🔥 Kill any previous session completely
            logout_user()

            # 🔥 Login fresh
            login_user(user)

            # 🔥 FORCE redirect (ignore next completely)
            return redirect(url_for("auth.index"))

        flash("Invalid credentials", "danger")

    return render_template("login.html")

@login_manager.unauthorized_handler
def unauthorized():
    # 🔥 Always go to clean login (NO next param)
    return redirect(url_for("auth.login"))


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/")
def index():
    if current_user.is_authenticated:
        return _role_redirect(current_user)

    return redirect(url_for("auth.login"))

# ======================================================
# 🔐 ROLE REDIRECT LOGIC (Enterprise Clean)
# ======================================================

def _role_redirect(user):
    """
    Centralized role-based routing logic.
    Keeps login clean and future-proof.
    """

    # Bulk upload setup
    if user.username =="bulk_upload":
        return redirect(url_for("upload.upload_page"))

    # Category Review Team
    if user.role == "category_team":
        return redirect(url_for("category_review.category_review_page"))

    # Pricing Team → Pricing Ingestion
    elif user.role == "pricing_team":
        return redirect(url_for("ingestion.ingest_pricing"))

    # Asset Team → Asset Ingestion
    elif user.role == "asset_team":
        return redirect(url_for("ingestion.ingest_assets"))

    # Product Team → Product Ingestion
    elif user.role == "product_team":
        return redirect(url_for("ingestion.ingest_product"))

    # Admin
    elif user.role == "admin_team":
            return redirect(url_for("admin.admin_home"))

    # Fallback
    return redirect(url_for("auth.login"))
