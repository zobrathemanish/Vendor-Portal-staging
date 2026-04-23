#asset_ingestion_routes.py

from flask import Blueprint, render_template, request, redirect, url_for, flash, request, current_app, jsonify
from flask_login import login_required
import subprocess
import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from services.azure_service import create_submission_manifest
from urllib.parse import unquote
import json
import pandas as pd
from io import BytesIO

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

submission_progress = {}

asset_ingestion_bp = Blueprint(
    "ingestion",
    __name__,
    url_prefix="/ingestion"
)

# ---------------------------------------
# PRODUCT INGESTION
# ---------------------------------------

@asset_ingestion_bp.route("/product")
@login_required
def ingest_product():
    return render_template("ingestion/ingest_product.html")


# ---------------------------------------
# PRICING INGESTION
# ---------------------------------------

@asset_ingestion_bp.route("/pricing")
@login_required
def ingest_pricing():
    return render_template("ingestion/ingest_pricing.html")


# ---------------------------------------
# ASSET INGESTION
# ---------------------------------------
@asset_ingestion_bp.route("/assets")
@login_required
def ingest_assets():
    return render_template("ingestion/ingest_assets.html")

@asset_ingestion_bp.route("/start-asset-etl", methods=["POST"])
@login_required
def start_asset_etl():

    data = request.get_json()

    vendor = data.get("vendor")
    submission_type = data.get("submission_type")
    blob_paths = data.get("blob_paths", [])
    submission_id = data.get("submission_id")
    workflow = "assets"

    create_submission_manifest(
        vendor,
        workflow,
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

@asset_ingestion_bp.route("/api/asset-status/<vendor>/<submission_id>")
@login_required
def get_asset_status(vendor, submission_id):

    try:
        conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        blob_service = BlobServiceClient.from_connection_string(conn)
        container = blob_service.get_container_client("silver")

        blob_path = (
            f"logs/vendor={vendor}/workflow=assets/"
            f"submission={submission_id}/asset_etl_status.json"
        )

        blob = container.get_blob_client(blob_path)

        # ----------------------------------
        # equivalent to os.path.exists()
        # ----------------------------------
        if not blob.exists():
            return jsonify({
                "stage": "starting",
                "progress": 5,
                "message": "Initializing pipeline"
            })

        # ----------------------------------
        # equivalent to open() + json.load()
        # ----------------------------------
        data = json.loads(blob.download_blob().readall())

        stage = data.get("stage", "processing")
        progress = data.get("progress", 5)
        message = data.get("message", "Processing")

        if data.get("status") == "completed":
            return jsonify({
                "stage": "complete",
                "progress": 100,
                "message": message or "Processing complete"
            })

        if data.get("status") == "failed":
            return jsonify({
                "stage": "failed",
                "progress": progress,
                "message": message or "Processing failed"
            })

        return jsonify({
            "stage": stage,
            "progress": progress,
            "message": message
        })

    except Exception as e:
        print("ASSET STATUS ERROR:", e)
        return jsonify({
            "stage": "processing",
            "progress": 5,
            "message": "Reading pipeline status"
        })
    

@asset_ingestion_bp.route("/api/asset-outputs/<vendor>/<submission_type>/<submission_id>")
@login_required
def get_asset_outputs(vendor, submission_type, submission_id):
    import os
    from azure.storage.blob import BlobServiceClient

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    blob_service = BlobServiceClient.from_connection_string(conn)

    container = blob_service.get_container_client("silver")

    prefixes = [
        f"in_review/assets_workflow/vendor={vendor}/submission_type={submission_type}/submission={submission_id}/reports/",
        f"in_review/assets_workflow/vendor={vendor}/submission_type={submission_type}/submission={submission_id}/logs/"
    ]

    files = []

    try:

        for prefix in prefixes:

            blobs = container.list_blobs(name_starts_with=prefix)

            for blob in blobs:

                name = blob.name.split("/")[-1]

                allowed = {
                    "transformed_assets.zip",
                    "asset_submission_summary.xlsx",
                    "validation_report.xlsx"

                }

                if name not in allowed:
                    continue
            
                files.append({
                        "name": name,
                        "path": blob.name
                    })

    except Exception as e:
        print("OUTPUT LIST ERROR:", e)

    summary = {
        "assets_processed": 0,
        "total_issues": 0,
        "autofixed_issues": 0,
        "remaining_issues": 0
    }

    # ---------------------------
    # ASSETS PROCESSED
    # ---------------------------
    try:

        health_blob = (
            f"in_review/assets_workflow/vendor={vendor}/submission_type={submission_type}/submission={submission_id}/logs/"
            f"health_report.xlsx"
        )

        raw = container.get_blob_client(health_blob).download_blob().readall()
        df_health = pd.read_excel(BytesIO(raw))

        summary["assets_processed"] = len(df_health)

    except Exception as e:
            print("Health summary error:", e)

    # ---------------------------
    # ISSUE SUMMARY
    # ---------------------------
    try:

        report_blob = (
            f"in_review/assets_workflow/vendor={vendor}/submission_type={submission_type}/submission={submission_id}/reports/"
            f"asset_submission_summary.xlsx"
        )

        raw = container.get_blob_client(report_blob).download_blob().readall()
        df_summary = pd.read_excel(BytesIO(raw))

        summary["total_issues"] = int(df_summary["issue"].notna().sum())

        summary["autofixed_issues"] = int(
            (df_summary["action_taken"] == "fixed_automatically").sum()
        )

        summary["remaining_issues"] = int(
            (df_summary["vendor_action_required"] != "none").sum()
        )

    except Exception:

        # fallback to validation report
        try:

            validation_blob = (
                f"in_review/assets_workflow/vendor={vendor}/submission_type={submission_type}/submission={submission_id}/logs/"
                f"validation_report.xlsx"
            )

            raw = container.get_blob_client(validation_blob).download_blob().readall()
            df_validation = pd.read_excel(BytesIO(raw))

            summary["total_issues"] = len(df_validation)

            summary["remaining_issues"] = int(
                (df_validation["severity"] == "blocking").sum()
            )

        except Exception as e:
            print("Validation fallback error:", e)

    return jsonify({
        "files": files,
        "summary": summary
    })


from flask import Response
from azure.storage.blob import BlobServiceClient

@asset_ingestion_bp.route("/api/asset-report")
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

@asset_ingestion_bp.route("/api/asset-report-preview")
@login_required
def preview_asset_report():

    blob_path = unquote(request.args.get("path"))

    if not blob_path:
        return {"error":"Missing path"},400

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    blob_service = BlobServiceClient.from_connection_string(conn)

    container = blob_service.get_container_client("silver")

    raw = container.get_blob_client(blob_path).download_blob().readall()

    df = pd.read_excel(BytesIO(raw))

    return jsonify({
        "columns": list(df.columns),
        "rows": df.fillna("").to_dict(orient="records")
    })

@asset_ingestion_bp.route("/api/transformed-assets/<vendor>/<submission_type>/<submission_id>")
@login_required
def list_transformed_assets(vendor, submission_type, submission_id):

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    blob_service = BlobServiceClient.from_connection_string(conn)

    container = blob_service.get_container_client("silver")

    prefix = f"ready/assets_workflow/vendor={vendor}/submission_type={submission_type}/submission={submission_id}/assets/"

    images = []

    for blob in container.list_blobs(name_starts_with=prefix):

        name = blob.name.lower()

        if name.endswith(".jpg") or name.endswith(".jpeg") or name.endswith(".png"):

            images.append({
                "name": blob.name.split("/")[-1],
                "path": blob.name
            })

    return jsonify(images)

@asset_ingestion_bp.route("/api/asset-image")
@login_required
def get_asset_image():

    blob_path = unquote(request.args.get("path"))

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    blob_service = BlobServiceClient.from_connection_string(conn)

    container = blob_service.get_container_client("silver")

    data = container.get_blob_client(blob_path).download_blob().readall()

    return Response(data, mimetype="image/jpeg")