#ingestion_routes.py

from flask import Blueprint, render_template, request, redirect, url_for, flash, request, current_app, jsonify
from flask_login import login_required
import subprocess
import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from services.azure_service import create_submission_manifest
from urllib.parse import unquote


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

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
@ingestion_bp.route("/assets")
@login_required
def ingest_assets():
    return render_template("ingestion/ingest_assets.html")

@ingestion_bp.route("/start-asset-etl", methods=["POST"])
@login_required
def start_asset_etl():

    data = request.get_json()

    vendor = data.get("vendor")
    submission_type = data.get("submission_type")
    blob_paths = data.get("blob_paths", [])
    submission_id = data.get("submission_id")

    create_submission_manifest(
        vendor,
        submission_id,
        submission_type,
        blob_paths
    )

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
        "status": "started",
        "submission_id": submission_id
    })


@ingestion_bp.route("/api/asset-status/<vendor>/<submission_id>")
@login_required
def get_asset_status(vendor, submission_id):

    import json
    import os

    status_path = os.path.join(
        current_app.root_path,
        "asset-etl",
        "logs",
        f"vendor={vendor}",
        "assets",
        f"submission={submission_id}",
        "asset_etl_status.json"
    )

    if not os.path.exists(status_path):

        return jsonify({
            "stage": "starting",
            "progress": 5,
            "message": "Initializing pipeline"
        })

    try:

        with open(status_path, "r") as f:
            data = json.load(f)

        stage = data.get("stage", "processing")
        progress = data.get("progress", 5)
        message = data.get("message", "Processing")

        if data.get("status") == "completed":

            return jsonify({
                "stage": "complete",
                "progress": 100,
                "message": message or "Processing complete"
            })

        return jsonify({
            "stage": stage,
            "progress": progress,
            "message": message
        })

    except Exception as e:

        print("STATUS READ ERROR:", e)

        return jsonify({
            "stage": "processing",
            "progress": 5,
            "message": "Reading pipeline status"
        })
    
@ingestion_bp.route("/api/asset-outputs/<vendor>/<submission_id>")
@login_required
def get_asset_outputs(vendor, submission_id):

    import os
    from azure.storage.blob import BlobServiceClient

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    blob_service = BlobServiceClient.from_connection_string(conn)

    container = blob_service.get_container_client("silver")

    prefixes = [
        f"in_review/vendor={vendor}/assets_workflow/submission={submission_id}/reports/"
    ]

    files = []

    try:

        for prefix in prefixes:

            blobs = container.list_blobs(name_starts_with=prefix)

            for blob in blobs:

                name = blob.name.split("/")[-1]

                allowed = {
                    "transformed_assets.zip",
                    "asset_submission_summary.xlsx"
                }

                if name not in allowed:
                    continue
            
                files.append({
                        "name": name,
                        "path": blob.name
                    })

    except Exception as e:
        print("OUTPUT LIST ERROR:", e)

    return jsonify({"files": files})


from flask import Response
from azure.storage.blob import BlobServiceClient

@ingestion_bp.route("/api/asset-report")
@login_required
def get_asset_report():

    blob_path = unquote(request.args.get("path"))

    if not blob_path:
        return {"error": "Missing file path"}, 400

    blob_path = unquote(blob_path)

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    blob_service = BlobServiceClient.from_connection_string(conn)

    container = blob_service.get_container_client("silver")

    blob = container.get_blob_client(blob_path)

    stream = blob.download_blob().readall()

    filename = blob_path.split("/")[-1]

    return Response(
        stream,
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        },
        mimetype="application/octet-stream"
    )
