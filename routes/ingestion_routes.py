from flask import Blueprint, render_template, request, redirect, url_for, flash, request, current_app, jsonify
from flask_login import login_required
import subprocess
import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo

submission_progress = {}

ingestion_bp = Blueprint(
    "ingestion",
    __name__,
    url_prefix="/ingestion"
)

# ---------------------------------------
# PRODUCT INGESTION
# ---------------------------------------

@ingestion_bp.route("/product")
@login_required
def ingest_product():
    return render_template("ingestion/ingest_product.html")


# ---------------------------------------
# PRICING INGESTION
# ---------------------------------------

@ingestion_bp.route("/pricing")
@login_required
def ingest_pricing():
    return render_template("ingestion/ingest_pricing.html")


# ---------------------------------------
# ASSET INGESTION
# ---------------------------------------

@ingestion_bp.route("/assets", methods=["GET", "POST"])
@login_required
def ingest_assets():
    import uuid

    if request.method == "POST":

        submission_type = request.form.get("submission_type")
        vendor = request.form.get("vendor_name")
        submission_id = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        # submission_id = datetime.now(ZoneInfo("America/Winnipeg")).strftime("%Y%m%dT%H%M%S")
        submission_progress[submission_id] = {
            "vendor": vendor,
            "status": "started",
            "message": "Preparing asset processing",
            "percent": 0,
            "current_file": None,
            "started_at": datetime.utcnow().isoformat()
        }

        print(f"Starting asset ETL for vendor: {vendor}")

        etl_script = os.path.join(
            current_app.root_path,
            "asset-etl",
            "run_vendor_asset_etl.py"
        )

        subprocess.Popen([
            sys.executable,
            etl_script,
            "--vendor",vendor,
            "--submission-type", submission_type,
            "--submission-id", submission_id
        ])

        flash("Assets uploaded successfully. Asset ETL started.", "success")

        return redirect(url_for("ingestion.ingest_assets", submission_id=submission_id))

    return render_template("ingestion/ingest_assets.html")

@ingestion_bp.route("/api/submission-status/<submission_id>")
def submission_status(submission_id):

    data = submission_progress.get(submission_id)

    if not data:
        return jsonify({"status": "unknown"}), 404

    return jsonify(data)