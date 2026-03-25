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

    precheck_status_path = os.path.join(
        current_app.root_path,
        "product-etl",
        "logs",
        f"vendor={vendor}",
        "pricing",
        f"submission={submission_id}",
        "pricing_precheck_status.json"
    )

    etl_status_path = os.path.join(
        current_app.root_path,
        "product-etl",
        "logs",
        f"vendor={vendor}",
        "pricing",
        f"submission={submission_id}",
        "pricing_etl_status.json"
    )

    try:

        if os.path.exists(etl_status_path):
            with open(etl_status_path, "r") as f:
                data = json.load(f)

            return jsonify({
                "stage": data.get("stage", "processing"),
                "progress": data.get("progress", 5),
                "message": data.get("message", "Processing"),
                "current_file": data.get("current_file", "")
            })

        if os.path.exists(precheck_status_path):
            with open(precheck_status_path, "r") as f:
                data = json.load(f)

            return jsonify({
                "stage": data.get("stage", "upload"),
                "progress": data.get("progress", 10),
                "message": data.get("message", "Running validation"),
                "current_file": data.get("current_file", "")
            })

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

    prefix = f"in_review/pricing_workflow/{vendor}/{submission_type}/{submission_id}/"

    files = []

    try:
        for blob in container.list_blobs(name_starts_with=prefix):
            files.append({
                "name": blob.name.split("/")[-1],
                "path": blob.name
            })
    except Exception as e:
        print("PRICING OUTPUT ERROR:", e)

    return jsonify({"files": files})

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

    df = pd.read_excel(BytesIO(raw))

    return jsonify({
        "columns": list(df.columns),
        "rows": df.fillna("").to_dict(orient="records")
    })