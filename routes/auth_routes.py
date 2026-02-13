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
    print("Authenticated:", current_user.is_authenticated)
    if request.method == "POST":
        username = request.form.get("userid")
        password = request.form.get("password")

        user = User.query.filter_by(username=username).first()

        if user and user.is_active and user.check_password(password):
            login_user(user)
            flash("Logged in successfully", "success")
            return redirect(url_for("upload.upload_page"))

        flash("Invalid credentials", "danger")

    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out", "info")
    return redirect(url_for("auth.login"))

@auth_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("upload.upload_page"))
    return redirect(url_for("auth.login"))

