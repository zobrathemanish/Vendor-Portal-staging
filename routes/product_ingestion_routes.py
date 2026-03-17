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

product_ingestion_bp = Blueprint(
    "product_ingestion",
    __name__,
    url_prefix="/ingestion"
)

# ---------------------------------------
# PRODUCT SAS UPLOAD
# ---------------------------------------

@product_ingestion_bp.route("/get-product-upload-sas", methods=["POST"])
@login_required
def get_product_upload_sas():

    data = request.get_json()

    vendor = data.get("vendor")
    filename = data.get("filename")
    submission_id = data.get("submission_id")

    if not vendor or not filename:
        return {"error": "Missing vendor or filename"}, 400

    blob_path = (
        f"raw/vendor={vendor}/products/"
        f"submission={submission_id}/original_xml/{filename}"
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
# PRODUCT INGESTION PAGE
# ---------------------------------------

@product_ingestion_bp.route("/product")
@login_required
def ingest_product():
    return render_template("ingestion/ingest_product.html")


# ---------------------------------------
# START PRODUCT INGESTION
# ---------------------------------------

@product_ingestion_bp.route("/start-product-etl", methods=["POST"])
@login_required
def start_product_etl():

    data = request.get_json()

    vendor = data.get("vendor")
    submission_id = data.get("submission_id")
    blob_path = data.get("blob_path")
    submission_type = data.get("submission_type")
    workflow = "products"

    print(f"Starting product ingestion: {vendor} {submission_id}")

    print("ROOT PATH:", current_app.root_path)

    # ---------------------------------------
    # STEP 1: RUN MAPPING + VALIDATION
    # ---------------------------------------

    print("ROOT PATH:", current_app.root_path)

    mapping_script = os.path.join(
        current_app.root_path,
        "mapping_validation",
        "scripts",
        "run_mapping_validation.py"
    )

    print("SCRIPT PATH:", mapping_script)
    print("SCRIPT EXISTS:", os.path.exists(mapping_script))

    result = subprocess.run([
        sys.executable,
        mapping_script,
        "--vendor", vendor,
        "--workflow", workflow,
        "--submission-id", submission_id,
        "--submission-type", submission_type,
        "--blob-path", blob_path
    ], capture_output=True, text=True)

    if result.returncode != 0:

        print("=== MAPPING STDOUT ===")
        print(result.stdout)

        print("=== MAPPING STDERR ===")
        print(result.stderr)

        return jsonify({
            "status": "failed",
            "stage": "validation",
            "message": result.stderr
        })

    # ---------------------------------------
    # STEP 2: CREATE MANIFEST
    # ---------------------------------------

    create_submission_manifest(
        vendor=vendor,
        workflow=workflow,
        submission_id=submission_id,
        submission_type=submission_type,
        files=[blob_path]
    )

    # ---------------------------------------
    # STEP 3: START ETL
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
# PRODUCT STATUS
# ---------------------------------------

@product_ingestion_bp.route("/api/product-status/<vendor>/<submission_id>")
@login_required
def get_product_status(vendor, submission_id):
    """
    Returns:
    - precheck status first if ETL has not started yet
    - ETL status if ETL status file exists
    """

    precheck_status_path = os.path.join(
        current_app.root_path,
        "product-etl",
        "logs",
        f"vendor={vendor}",
        "products",
        f"submission={submission_id}",
        "product_precheck_status.json"
    )

    etl_status_path = os.path.join(
        current_app.root_path,
        "product-etl",
        "logs",
        f"vendor={vendor}",
        "products",
        f"submission={submission_id}",
        "product_etl_status.json"
    )

    try:
        # ----------------------------------
        # ETL STATUS TAKES PRIORITY
        # ----------------------------------
        if os.path.exists(etl_status_path):
            with open(etl_status_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if data.get("status") == "completed":
                return jsonify({
                    "stage": "complete",
                    "progress": 100,
                    "message": data.get("message", "Processing complete"),
                    "current_file": data.get("current_file", "")
                })

            if data.get("status") == "failed":
                return jsonify({
                    "stage": "failed",
                    "progress": data.get("progress", 0),
                    "message": data.get("message", "Processing failed"),
                    "current_file": data.get("current_file", "")
                })

            return jsonify({
                "stage": data.get("stage", "processing"),
                "progress": data.get("progress", 5),
                "message": data.get("message", "Processing"),
                "current_file": data.get("current_file", "")
            })

        # ----------------------------------
        # PRECHECK STATUS
        # ----------------------------------
        if os.path.exists(precheck_status_path):
            with open(precheck_status_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if data.get("status") == "failed":
                return jsonify({
                    "stage": "failed",
                    "progress": data.get("progress", 15),
                    "message": data.get(
                        "message",
                        "Basic validation failed"
                    ),
                    "current_file": data.get("current_file", "")
                })

            if data.get("status") == "completed":
                return jsonify({
                    "stage": "validate",
                    "progress": data.get("progress", 25),
                    "message": data.get(
                        "message",
                        "Basic validation passed. Starting ETL..."
                    ),
                    "current_file": data.get("current_file", "")
                })

            return jsonify({
                "stage": data.get("stage", "upload"),
                "progress": data.get("progress", 10),
                "message": data.get("message", "Running mapping/basic validation"),
                "current_file": data.get("current_file", "")
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
        print("PRODUCT STATUS READ ERROR:", e)

        return jsonify({
            "stage": "processing",
            "progress": 5,
            "message": "Reading pipeline status"
        })


# ---------------------------------------
# PRODUCT OUTPUTS
# ---------------------------------------

@product_ingestion_bp.route("/api/product-outputs/<vendor>/<submission_type>/<submission_id>")
@login_required
def get_product_outputs(vendor, submission_type, submission_id):
    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    blob_service = BlobServiceClient.from_connection_string(conn)

    container = blob_service.get_container_client("silver")

    prefixes = [
        f"in_review/products_workflow/{vendor}/{submission_type}/{submission_id}/reports/",
        f"in_review/products_workflow/{vendor}/{submission_type}/{submission_id}/logs/"
    ]

    files = []

    try:
        allowed = {
            "mapped.xlsx",
            "health_issues.xlsx",
            "validation_report.xlsx",
            "product_submission_summary.xlsx",
            "transformed_products.xlsx"
        }

        for prefix in prefixes:
            blobs = container.list_blobs(name_starts_with=prefix)

            for blob in blobs:
                name = blob.name.split("/")[-1]

                if name not in allowed:
                    continue

                files.append({
                    "name": name,
                    "path": blob.name
                })

    except Exception as e:
        print("PRODUCT OUTPUT LIST ERROR:", e)

    summary = {
        "records_processed": 0,
        "total_issues": 0,
        "autofixed_issues": 0,
        "remaining_issues": 0
    }

    # ----------------------------------
    # RECORDS PROCESSED
    # ----------------------------------
    try:
        mapped_blob = (
            f"in_review/products_workflow/{vendor}/{submission_type}/{submission_id}/logs/"
            f"mapped.xlsx"
        )

        raw = container.get_blob_client(mapped_blob).download_blob().readall()
        df_mapped = pd.read_excel(BytesIO(raw))

        summary["records_processed"] = len(df_mapped)

    except Exception as e:
        print("Mapped summary error:", e)

    # ----------------------------------
    # ISSUE SUMMARY
    # ----------------------------------
    try:
        summary_blob = (
            f"in_review/products_workflow/{vendor}/{submission_type}/{submission_id}/reports/"
            f"product_submission_summary.xlsx"
        )

        raw = container.get_blob_client(summary_blob).download_blob().readall()
        df_summary = pd.read_excel(BytesIO(raw))

        if "issue" in df_summary.columns:
            summary["total_issues"] = int(df_summary["issue"].notna().sum())

        if "action_taken" in df_summary.columns:
            summary["autofixed_issues"] = int(
                (df_summary["action_taken"] == "fixed_automatically").sum()
            )

        if "vendor_action_required" in df_summary.columns:
            summary["remaining_issues"] = int(
                (df_summary["vendor_action_required"] != "none").sum()
            )

    except Exception:
        # fallback to validation report
        try:
            validation_blob = (
                f"in_review/products_workflow/{vendor}/{submission_type}/{submission_id}/logs/"
                f"validation_report.xlsx"
            )

            raw = container.get_blob_client(validation_blob).download_blob().readall()
            df_validation = pd.read_excel(BytesIO(raw))

            summary["total_issues"] = len(df_validation)

            if "severity" in df_validation.columns:
                summary["remaining_issues"] = int(
                    (df_validation["severity"] == "blocking").sum()
                )

        except Exception as e:
            print("Product validation fallback error:", e)

    return jsonify({
        "files": files,
        "summary": summary
    })


# ---------------------------------------
# PRODUCT REPORT DOWNLOAD
# ---------------------------------------

@product_ingestion_bp.route("/api/product-report")
@login_required
def get_product_report():
    blob_path = unquote(request.args.get("path"))

    if not blob_path:
        return {"error": "Missing file path"}, 400

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


# ---------------------------------------
# PRODUCT REPORT PREVIEW
# ---------------------------------------

@product_ingestion_bp.route("/api/product-report-preview")
@login_required
def preview_product_report():
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