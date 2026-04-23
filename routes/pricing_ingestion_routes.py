# product_ingestion_routes.py

from flask import (
    Blueprint,
    render_template,
    request,
    current_app,
    jsonify,
    Response
)
from flask_login import login_required
import subprocess
import sys
import os
import json
from io import BytesIO
from urllib.parse import unquote

import pandas as pd
from azure.storage.blob import BlobServiceClient

from services.azure_service import create_submission_manifest
from services.azure_service import generate_upload_sas
from utils.status_helper import read_status

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

pricing_ingestion_bp = Blueprint(
    "pricing_ingestion",
    __name__,
    url_prefix="/ingestion"
)

# ---------------------------------------
# PRICING SAS UPLOAD
# ---------------------------------------

@pricing_ingestion_bp.route("/get-pricing-upload-sas", methods=["POST"])
@login_required
def get_pricing_upload_sas():

    data = request.get_json()

    vendor = data.get("vendor")
    filename = data.get("filename")
    submission_id = data.get("submission_id")
    submission_type = data.get("submission_type")
    workflow = "pricing"

    if not vendor or not filename or not submission_id or not submission_type:
        return {"error": "Missing required fields"}, 400

    blob_path = (
        f"raw/vendor={vendor}/"
        f"workflow={workflow}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}/"
        f"original_excel/{filename}"
    )

    sas_url = generate_upload_sas(
        container=current_app.config["AZURE_CONTAINER_NAME"],
        blob_path=blob_path
    )

    return {
        "sas_url": sas_url,
        "blob_path": blob_path
    }
# ---------------------------------------
# PRICING INGESTION PAGE
# ---------------------------------------

@pricing_ingestion_bp.route("/pricing")
@login_required
def ingest_pricing():
    return render_template("ingestion/ingest_pricing.html")

# ---------------------------------------
# START PRICING INGESTION
# ---------------------------------------

@pricing_ingestion_bp.route("/start-pricing-etl", methods=["POST"])
@login_required
def start_pricing_etl():

    data = request.get_json()

    vendor = data.get("vendor")
    submission_id = data.get("submission_id")
    blob_path = data.get("blob_path")
    submission_type = data.get("submission_type")
    workflow = "pricing"

    print(f"Starting pricing ingestion: {vendor} {submission_id}")

    mapping_script = os.path.join(
        current_app.root_path,
        "mapping_validation",
        "scripts",
        "run_mapping_validation.py"
    )

    result = subprocess.run([
        sys.executable,
        mapping_script,
        "--vendor", vendor,
        "--workflow", workflow,
        "--submission-id", submission_id,
        "--submission-type", submission_type,
    ], capture_output=True, text=True)

    if result.returncode != 0:

        print("=== PRICING MAPPING STDOUT ===")
        print(result.stdout)

        print("=== PRICING MAPPING STDERR ===")
        print(result.stderr)

        return jsonify({
            "status": "failed",
            "stage": "validation",
            "message": result.stderr
        })

    # ---------------------------------------
    # CREATE MANIFEST
    # ---------------------------------------

    create_submission_manifest(
        vendor=vendor,
        workflow=workflow,
        submission_id=submission_id,
        submission_type=submission_type,
        files=[blob_path]
    )

    # ---------------------------------------
    # START ETL
    # ---------------------------------------

    etl_script = os.path.join(
        current_app.root_path,
        "data_ETL",
        "run_vendor_etl.py"
    )

    subprocess.Popen([
        sys.executable,
        etl_script,
        "--vendor", vendor,
        "--workflow", workflow,
        "--submission-type", submission_type,
        "--submission-id", submission_id
    ])

    return jsonify({
        "status": "started",
        "submission_id": submission_id
    })


# ---------------------------------------
# PRICING STATUS
# ---------------------------------------

@pricing_ingestion_bp.route("/api/pricing-status/<vendor>/<submission_id>")
@login_required
def get_pricing_status(vendor, submission_id):

    try:
        # ----------------------------------
        # ETL STATUS (BLOB)
        # ----------------------------------
        etl_data = read_status(
            vendor, "pricing", submission_id, "pricing_etl_status.json"
        )

        if etl_data:
            return jsonify({
                "stage": etl_data.get("stage", "processing"),
                "progress": etl_data.get("progress", 5),
                "message": etl_data.get("message", "Processing"),
                "current_file": etl_data.get("current_file", "")
            })

        # ----------------------------------
        # PRECHECK STATUS (BLOB)
        # ----------------------------------
        precheck_data = read_status(
            vendor, "pricing", submission_id, "pricing_precheck_status.json"
        )

        if precheck_data:
            return jsonify({
                "stage": precheck_data.get("stage", "upload"),
                "progress": precheck_data.get("progress", 10),
                "message": precheck_data.get("message", "Running validation"),
                "current_file": precheck_data.get("current_file", "")
            })

        # ----------------------------------
        # DEFAULT
        # ----------------------------------
        return jsonify({
            "stage": "starting",
            "progress": 5,
            "message": "Initializing pipeline"
        })

    except Exception as e:
        print("PRICING STATUS ERROR:", e)
        return jsonify({
            "stage": "processing",
            "progress": 5,
            "message": "Reading pipeline status"
        })
    
# ---------------------------------------
# PRICING OUTPUTS
# ---------------------------------------

@pricing_ingestion_bp.route("/api/pricing-outputs/<vendor>/<submission_type>/<submission_id>")
@login_required
def get_pricing_outputs(vendor, submission_type, submission_id):

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    blob_service = BlobServiceClient.from_connection_string(conn)
    container = blob_service.get_container_client("silver")

    files = []

    def add_file(label, blob_path):
        blob_client = container.get_blob_client(blob_path)
        if blob_client.exists():
            files.append({
                "name": label,
                "path": blob_path
            })

    # ----------------------------------
    # MATCH PRODUCT STRUCTURE (pricing version)
    # ----------------------------------

    vendor_action_blob = (
        f"in_review/pricing_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}/"
        f"profiling/vendor_action_report.xlsx"
    )

    integrity_blob = (
        f"in_review/pricing_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}/"
        f"integrity/integrity_report.xlsx"
    )

    etl_mapped_blob = (
        f"ready/pricing_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}/"
        f"review/etl_mapped.xlsx"
    )

    delta_blob = (
        f"ready/pricing_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}/"
        f"review/delta_mapped.xlsx"
    )

    full_health_blob = (
        f"in_review/pricing_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}/"
        f"profiling/health_issues.xlsx"
    )

    # ----------------------------------
    # ADD FILES (same as product)
    # ----------------------------------

    add_file("Vendor Action Report", vendor_action_blob)
    add_file("Integrity Report", integrity_blob)
    add_file("Review Output (ETL Mapped)", etl_mapped_blob)
    add_file("Delta vs Current System", delta_blob)

    # ----------------------------------
    # SUMMARY (same logic as product)
    # ----------------------------------

    summary = {
        "records_processed": 0,
        "total_issues": 0,
        "autofixed_issues": 0,
        "remaining_issues": 0,
    }

    try:
        # ---- health report → issues
        full_blob_client = container.get_blob_client(full_health_blob)
        if full_blob_client.exists():
            raw_full = full_blob_client.download_blob().readall()
            df_full = pd.read_excel(BytesIO(raw_full), dtype=str)

            summary["total_issues"] = len(df_full)

            if "_fixable_by_code" in df_full.columns:
                vals = (
                    df_full["_fixable_by_code"]
                    .astype(str)
                    .str.strip()
                    .str.lower()
                )
                summary["autofixed_issues"] = int(
                    vals.isin(["true", "1", "yes"]).sum()
                )

            summary["remaining_issues"] = (
                summary["total_issues"] - summary["autofixed_issues"]
            )

        # ---- vendor report → records processed
        vendor_blob_client = container.get_blob_client(vendor_action_blob)
        if vendor_blob_client.exists():
            raw_vendor = vendor_blob_client.download_blob().readall()
            df_vendor = pd.read_excel(BytesIO(raw_vendor), dtype=str)

            if "part number (_entity_key)" in df_vendor.columns:
                part_col = (
                    df_vendor["part number (_entity_key)"]
                    .astype(str)
                    .str.strip()
                    .replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
                    .dropna()
                )

                summary["records_processed"] = part_col.nunique()
            else:
                summary["records_processed"] = len(df_vendor)

    except Exception as e:
        print("PRICING OUTPUT SUMMARY ERROR:", e)

    return jsonify({
        "files": files,
        "summary": summary
    })

# ---------------------------------------
# PRICING REPORT DOWNLOAD
# ---------------------------------------

@pricing_ingestion_bp.route("/api/pricing-report")
@login_required
def get_pricing_report():

    blob_path = unquote(request.args.get("path"))

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    blob_service = BlobServiceClient.from_connection_string(conn)

    blob = blob_service.get_container_client("silver").get_blob_client(blob_path)

    stream = blob.download_blob().readall()
    filename = blob_path.split("/")[-1]

    return Response(
        stream,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
        mimetype="application/octet-stream"
    )

# ---------------------------------------
# PRODUCT REPORT PREVIEW
# ---------------------------------------

@pricing_ingestion_bp.route("/api/pricing-report-preview")
@login_required
def preview_pricing_report():

    blob_path = unquote(request.args.get("path"))

    if not blob_path:
        return {"error": "Missing path"}, 400

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    blob_service = BlobServiceClient.from_connection_string(conn)

    container = blob_service.get_container_client("silver")
    raw = container.get_blob_client(blob_path).download_blob().readall()

    df = pd.read_excel(BytesIO(raw), dtype=str)

    return jsonify({
        "columns": list(df.columns),
        "rows": df.fillna("").to_dict(orient="records")
    })