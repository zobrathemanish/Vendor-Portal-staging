from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from models.user import SimpleUser, USERS

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        user_record = USERS.get(email)
        if user_record and user_record["password"] == password:
            user = SimpleUser(
                id=user_record["id"],
                email=email,
                role=user_record["role"],
                vendor=user_record["vendor"]
            )
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
    return redirect(url_for("login"))

@auth_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("upload.upload_page"))
    return redirect(url_for("login"))