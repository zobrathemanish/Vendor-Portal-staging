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

@ingestion_bp.route("/start-asset-etl", methods=["POST"])
@login_required
def start_asset_etl():

    import subprocess
    import sys
    import os
    from datetime import datetime

    data = request.get_json()

    vendor = data.get("vendor")
    submission_type = data.get("submission_type")
    blob_paths = data.get("blob_paths", [])

    submission_id = datetime.utcnow().strftime("%Y%m%dT%H%M%S")

    print(f"Starting asset ETL: vendor={vendor} submission={submission_id}")

    etl_script = os.path.join(
        current_app.root_path,
        "asset-etl",
        "run_vendor_asset_etl.py"
    )

    subprocess.Popen([
        sys.executable,
        etl_script,
        "--vendor", vendor,
        "--submission-type", submission_type,
        "--submission-id", submission_id
    ])

    return jsonify({
        "submission_id": submission_id
    })

@ingestion_bp.route("/api/asset-status/<vendor>/<submission_id>")
@login_required
def get_asset_status(vendor, submission_id):

    import json
    import os

    status_path = os.path.join(
        current_app.root_path,
        "logs",
        f"vendor={vendor}",
        "assets",
        f"submission={submission_id}",
        "asset_etl_status.json"
    )

    if not os.path.exists(status_path):

        return jsonify({
            "stage": "Starting",
            "progress": 5,
            "message": "Initializing pipeline"
        })

    with open(status_path) as f:
        data = json.load(f)

    events = data.get("events", [])

    if not events:

        return jsonify({
            "stage": "Starting",
            "progress": 5,
            "message": "Pipeline starting"
        })

    last = events[-1]

    stage = last.get("stage", "Processing")
    status = last.get("status", "RUNNING")
    message = last.get("message", "")

    # ----------------------------------------------------
    # Map stages to UI progress
    # ----------------------------------------------------

    stage_progress_map = {
        "ASSET VALIDATION": ("Checking files", 25),
        "ASSET CANONICALIZATION": ("Validating assets", 50),
        "ASSET TRANSFORMATION": ("Transforming assets", 90)
    }

    if status == "COMPLETED":

        return jsonify({
            "stage": "complete",
            "progress": 100,
            "message": message or "Asset ETL completed"
        })

    ui_stage, progress = stage_progress_map.get(
        stage,
        ("Processing", 60)
    )

    return jsonify({
        "stage": ui_stage,
        "progress": progress,
        "message": message or "Processing"
    })