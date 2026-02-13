from flask import Blueprint, send_from_directory, flash, redirect, url_for, current_app

utility_bp = Blueprint("utility", __name__)

@utility_bp.route("/download-template")
def download_template():
    try:
        return send_from_directory(
            current_app.config["TEMPLATE_FOLDER"],
            "standard_template.xlsx",
            as_attachment=True
        )
    except FileNotFoundError:
        flash("Template not found on server.", "danger")
        return redirect(url_for("upload.upload_page"))


@utility_bp.route("/form-help")
def form_submission_help():
    return "<h4>Coming soon: Vendor submission help guide</h4>"
