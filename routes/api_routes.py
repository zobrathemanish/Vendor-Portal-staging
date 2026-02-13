from flask import Blueprint, Response
import time
from flask import request, current_app
from services.azure_service import generate_upload_sas
from datetime import datetime
import hashlib
from azure.storage.blob import BlobServiceClient
from flask import request, current_app
from flask import Blueprint, request, current_app, Response, send_file
from flask_login import login_required, current_user
from datetime import datetime
from io import BytesIO
import time
import os
import json
import hashlib
import pandas as pd

from azure.storage.blob import BlobServiceClient
from services.azure_service import generate_upload_sas, generate_read_sas_url, read_json_blob_from_azure
from extensions import LOG_BUFFER



from extensions import LOG_BUFFER

api_bp = Blueprint("api", __name__)

@api_bp.route("/logs/stream")
def stream_logs():
    def event_stream():
        last_len = 0
        while True:
            if len(LOG_BUFFER) > last_len:
                for msg in list(LOG_BUFFER)[last_len:]:
                    yield f"data: {msg}\n\n"
                last_len = len(LOG_BUFFER)
            time.sleep(0.5)

    return Response(event_stream(), mimetype="text/event-stream")


@api_bp.route("/api/get-asset-upload-sas", methods=["POST"])
def get_asset_upload_sas():
    data = request.json

    vendor = data.get("vendor")
    submission_id = data.get("submission_id")
    if not submission_id:
        return {"error": "Missing submission_id"}, 400

    sku = data.get("sku")
    filename = data.get("filename")

    if not vendor or not filename:
        return {"error": "Missing vendor or filename"}, 400

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    blob_path = (
        f"raw/vendor={vendor}/"
        f"submission={submission_id}/"
        f"assets/{timestamp}_{filename}"
    )

    sas_url = generate_upload_sas(
        container=current_app.config["AZURE_CONTAINER_NAME"],
        blob_path=blob_path
    )

    return {
        "sas_url": sas_url,
        "blob_path": blob_path
    }

@api_bp.route("/api/check-asset-hash", methods=["POST"])
def check_asset_hash():
    data = request.json

    submission_id = data.get("submission_id")
    if not submission_id:
        return {"error": "Missing submission_id"}, 400

    vendor = data.get("vendor")
    client_hash = data.get("file_hash")
    filename = data.get("filename")

    if not vendor or not client_hash or not filename:
        return {"skip": False}

    blob_service = BlobServiceClient.from_connection_string(
        current_app.config["AZURE_CONNECTION_STRING"]
    )

    container_client = blob_service.get_container_client(
        current_app.config["AZURE_CONTAINER_NAME"]
    )

    prefix = f"raw/vendor={vendor}/submission={submission_id}/assets/"

    blobs = list(container_client.list_blobs(name_starts_with=prefix))
    if not blobs:
        return {"skip": False}

    for blob in blobs:
        if not blob.name.endswith(filename):
            continue

        blob_data = container_client.download_blob(blob.name).readall()
        server_hash = hashlib.sha256(blob_data).hexdigest()

        if server_hash == client_hash:
            return {
                "skip": True,
                "existing_blob_path": blob.name
            }

    return {"skip": False}

@api_bp.route("/api/cleanup-old-assets", methods=["POST"])
def cleanup_old_assets():
    data = request.json

    submission_id = data.get("submission_id")
    if not submission_id:
        return {"error": "Missing submission_id"}, 400

    vendor = data.get("vendor")
    keep_blob_paths = set(data.get("keep_blob_paths", []))

    if not vendor:
        return {"status": "no_vendor_provided"}

    blob_service = BlobServiceClient.from_connection_string(
        current_app.config["AZURE_CONNECTION_STRING"]
    )

    container_client = blob_service.get_container_client(
        current_app.config["AZURE_CONTAINER_NAME"]
    )

    prefix = f"raw/vendor={vendor}/submission={submission_id}/assets/"

    for blob in container_client.list_blobs(name_starts_with=prefix):
        if blob.name not in keep_blob_paths:
            container_client.delete_blob(blob.name)

    return {"status": "cleanup_complete"}

@api_bp.route("/api/submission-status")
@login_required
def submission_status():
    submission_id = request.args.get("submission_id")

    if not submission_id:
        return {"status": "MISSING_PARAMS"}, 400

    if current_user.role == "vendor":
        vendor = current_user.vendor
    else:
        vendor = request.args.get("vendor")

    if not vendor:
        return {"status": "MISSING_VENDOR"}, 400

    blob_path = (
        f"logs/vendor={vendor}/"
        f"submission={submission_id}/status.json"
    )

    try:
        data = read_json_blob_from_azure(
            blob_path=blob_path,
            container_name="silver"
        )
        return data

    except FileNotFoundError:
        return {"status": "PENDING"}

@api_bp.route("/api/output-summary")
def api_output_summary():

    vendor = request.args.get("vendor")
    submission_id = request.args.get("submission_id")

    if not vendor or not submission_id:
        return {
            "promotion_status": "UNKNOWN",
            "outputs": [],
            "rejection_logs": []
        }

    blob_service = BlobServiceClient.from_connection_string(
        current_app.config["AZURE_CONNECTION_STRING"]
    )

    container = blob_service.get_container_client("silver")

    result = {
        "promotion_status": "SUCCESS",
        "outputs": [],
        "rejection_logs": []
    }

    # --------------------------------------------------
    # 1️⃣ READY OUTPUT FILES
    # --------------------------------------------------

    ready_root = "ready"

    pricing_review_prefix = (
        f"ready_pricing_review/vendor={vendor}/"
        f"submission={submission_id}/"
        f"review/"
    )

    pricing_blobs = list(
        container.list_blobs(name_starts_with=pricing_review_prefix)
    )

    if pricing_blobs:
        ready_root = "ready_pricing_review"

    ready_prefix = (
        f"{ready_root}/vendor={vendor}/"
        f"submission={submission_id}/"
        f"review/"
    )

    for blob in container.list_blobs(name_starts_with=ready_prefix):
        if blob.name.endswith("/"):
            continue

        filename = os.path.basename(blob.name)

        if filename.lower().endswith(".done"):
            continue

        result["outputs"].append({
            "filename": filename,
            "url": generate_read_sas_url(
                blob_service, "silver", blob.name
            )
        })

    # --------------------------------------------------
    # 2️⃣ REJECTED / BLOCKING LOGS
    # --------------------------------------------------

    rejected_prefix = (
        f"rejected/logs/vendor={vendor}/"
        f"submission={submission_id}/"
    )

    for blob in container.list_blobs(name_starts_with=rejected_prefix):

        if blob.name.endswith("/"):
            continue

        if not blob.name.lower().endswith(".json"):
            continue

        log_blob = container.get_blob_client(blob.name)
        raw = log_blob.download_blob().readall().decode("utf-8")
        log_json = json.loads(raw)

        result["promotion_status"] = "HALTED"

        result["rejection_logs"].append({
            "filename": os.path.basename(blob.name),
            "url": generate_read_sas_url(
                blob_service, "silver", blob.name
            ),
            "stage": log_json.get("stage"),
            "display_file": os.path.basename(
                log_json.get("file", "")
            ).replace(".parquet", ""),
            "error_type": log_json.get("error_type"),
            "error_message": log_json.get("error_message"),
            "logged_at": log_json.get("timestamp"),
        })

    # --------------------------------------------------
    # 3️⃣ MAPPED OUTPUT (READ FROM BRONZE IF HALTED)
    # --------------------------------------------------

    if result["promotion_status"] == "HALTED":

        mapped_prefix = (
            f"raw/vendor={vendor}/"
            f"submission={submission_id}/"
            f"mapped/"
        )

        bronze_container = blob_service.get_container_client("bronze")

        for blob in bronze_container.list_blobs(name_starts_with=mapped_prefix):

            if blob.name.lower().endswith(".xlsx"):

                result["outputs"].append({
                    "filename": "mapped.xlsx",
                    "url": generate_read_sas_url(
                        blob_service, "bronze", blob.name
                    )
                })

    return result


@api_bp.route("/api/download-log")
@login_required
def download_log():
    vendor = request.args.get("vendor")
    submission_id = request.args.get("submission_id")

    blob_service = BlobServiceClient.from_connection_string(
        current_app.config["AZURE_CONNECTION_STRING"]
    )

    container = blob_service.get_container_client("silver")

    prefix = f"rejected/logs/vendor={vendor}/submission={submission_id}/"

    blobs = list(container.list_blobs(name_starts_with=prefix))

    if not blobs:
        return {"error": "No log found"}, 404

    blob_name = sorted(blobs, key=lambda b: b.last_modified)[-1].name

    blob = container.get_blob_client(blob_name)
    data = blob.download_blob().readall()

    return send_file(
        BytesIO(data),
        download_name=os.path.basename(blob_name),
        as_attachment=True
    )

@api_bp.route("/api/log-preview")
@login_required
def log_preview():
    vendor = request.args.get("vendor")
    submission_id = request.args.get("submission_id")

    blob_service = BlobServiceClient.from_connection_string(
        current_app.config["AZURE_CONNECTION_STRING"]
    )

    container = blob_service.get_container_client("silver")

    prefix = f"rejected/logs/vendor={vendor}/submission={submission_id}/"

    blobs = list(container.list_blobs(name_starts_with=prefix))

    if not blobs:
        return {"rows": []}

    blob_name = sorted(blobs, key=lambda b: b.last_modified)[-1].name
    blob = container.get_blob_client(blob_name)
    data = blob.download_blob().readall()

    df = pd.read_excel(BytesIO(data))

    preview = df.head(20).to_dict(orient="records")

    return {"rows": preview}

