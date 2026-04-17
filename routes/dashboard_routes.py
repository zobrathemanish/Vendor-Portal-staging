from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required, current_user
import os
from azure.storage.blob import BlobServiceClient
from flask import request
import json
from io import BytesIO
import pyarrow.parquet as pq
import pandas as pd
from utils.pipeline_state_helper import compute_vendor_pipeline_state, get_gold_container

admin_bp = Blueprint("admin", __name__)

AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = os.getenv("SILVER_CONTAINER", "silver")


blob_service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
container = blob_service.get_container_client(SILVER_CONTAINER)

BRONZE_CONTAINER = os.getenv("BRONZE_CONTAINER", "bronze")
bronze_container = blob_service.get_container_client(BRONZE_CONTAINER)

# =========================================================
# UTILITIES
# =========================================================

def blob_exists(path):
    try:
        container.get_blob_client(path).get_blob_properties()
        return True
    except:
        return False


def list_all_submission_prefixes():
    prefixes = [
        "rejected/logs/vendor=",
        "in_review/vendor=",
        "ready/vendor=",
        "post_pricing_review/vendor=",
        "ready_pricing_review/vendor=",
        "approved/logs/vendor="
    ]

    submissions = set()

    for prefix in prefixes:
        blobs = container.list_blobs(name_starts_with=prefix)
        for blob in blobs:
            parts = blob.name.split("/")
            vendor = None
            submission_id = None

            for part in parts:
                if part.startswith("vendor="):
                    vendor = part.replace("vendor=", "")
                if part.startswith("submission="):
                    submission_id = part.replace("submission=", "")

            if vendor and submission_id:
                submissions.add((vendor, submission_id))

    return list(submissions)


# =========================================================
# STAGE + FAILURE
# =========================================================

def detect_stage_fast(vendor, submission_id, blob_names):

    if any("approved/logs" in b for b in blob_names):
        return "approved"

    if any("category_queue" in b for b in blob_names):
        return "category_queue"

    if any("/ready/" in b for b in blob_names):
        return "ready"

    # 🔥 FIXED: supports workflow folders
    if any("in_review/" in b and "_workflow/vendor=" in b for b in blob_names):
        return "in_review"

    if any("rejected/logs" in b for b in blob_names):
        return "rejected"

    return "bronze_only"

def detect_failure_fast(blob_names):

    for b in blob_names:
        if "VALIDATION" in b:
            return "validation"
        if "INGESTION" in b:
            return "ingestion"

    return None


# =========================================================
# STAGE-AWARE PROGRESS
# =========================================================

def build_pipeline_state_fast(vendor, submission_id, stage, blob_names):

    pipeline = {
        "ingestion": "pending",
        "validation": "pending",
        "mapping": "pending",
        "media": "pending",
        "profiling": "pending",
        "autofix": "pending",
        "integrity": "pending",
        "etl": "pending",
        "delta": "pending",
        "category": "pending",
        "analytics": "pending",
    }

    if stage == "rejected":
        pipeline["ingestion"] = "failed"
        pipeline["validation"] = "failed"
        return pipeline

    # =========================
    # CORE STEPS (FILE-BASED)
    # =========================

    if any("mapped/mapped.xlsx" in b for b in blob_names):
        pipeline["mapping"] = "success"

    if any("media_canonical.xlsx" in b for b in blob_names):
        pipeline["media"] = "success"

    if any("health_issues.xlsx" in b for b in blob_names):
        pipeline["profiling"] = "success"

    if any("autofix_report.xlsx" in b for b in blob_names):
        pipeline["autofix"] = "success"

    if any("integrity_report.xlsx" in b for b in blob_names):
        pipeline["integrity"] = "success"

    if any("review/etl_mapped.xlsx" in b for b in blob_names):
        pipeline["etl"] = "success"

    if any("review/delta_mapped.xlsx" in b for b in blob_names):
        pipeline["delta"] = "success"

    # =========================
    # BASE PIPELINE STATE
    # =========================

    if stage in ["in_review", "ready", "category_queue", "approved"]:
        pipeline["ingestion"] = "success"
        pipeline["validation"] = "success"

    # =========================
    # CATEGORY / APPROVAL
    # =========================

    if stage == "category_queue":
        pipeline["category"] = "pending"

    if any("approved/logs" in b for b in blob_names):
        pipeline["category"] = "approved"

    # =========================
    # 🔥 ANALYTICS (NEW FIX)
    # =========================

    # Check across ALL workflows dynamically
    if any(
        f"vendor={vendor}" in b and
        f"submission={submission_id}" in b and
        "analytics/vendor_scorecard" in b
        for b in blob_names
    ):
        pipeline["analytics"] = "generated"

    return pipeline

 # -----------------------------------------
# FINAL STATE RESET AFTER GOLD
# -----------------------------------------
def save_pipeline_history(container, vendor, workflows_result,
                        merge_status, integrity_status,
                          category_status, gold_status, gold_time):

                history_path = f"approved/unified_workflow/vendor={vendor}/_history.json"

                try:
                    history = read_json(container, history_path)
                except:
                    history = []

                # Avoid duplicate entries
                if history and history[-1].get("timestamp") == str(gold_time):
                    return

                snapshot = {
                    "timestamp": str(gold_time),

                    # 🔥 FULL WORKFLOW STATE (this was missing)
                    "workflows": workflows_result,

                    # 🔥 GLOBAL STATE
                    "global": {
                        "merge": merge_status,
                        "integrity": integrity_status,
                        "category": category_status,
                        "gold": gold_status
                    }
                }

                history.append(snapshot)

                container.upload_blob(
                    history_path,
                    json.dumps(history, indent=2),
                    overwrite=True
                )

# =========================================================
# ADMIN UI PAGE
# =========================================================
@admin_bp.route("/admin")
@login_required
def admin_home():
    if current_user.role != "admin_team":
        return "Unauthorized", 403
    return render_template("admin_home.html")

@admin_bp.route("/admin/submissions")
@login_required
def admin_submissions_page():
    if current_user.role != "admin_team":
        return "Unauthorized", 403

    return render_template("admin_submission.html")

@admin_bp.route("/admin/dashboard")
@login_required
def admin_dashboard_page():
    if current_user.role != "admin_team":
        return "Unauthorized", 403

    return render_template("admin_dashboard.html")


# =========================================================
# ADMIN API
# =========================================================

@admin_bp.route("/api/admin/summary")
@login_required
def get_admin_summary():
    if current_user.role != "admin_team":
        return jsonify({"error": "Unauthorized"}), 403

    try:
        stage = request.args.get("stage", "in_review")

        if stage != "in_review":
            return jsonify({"error": "Invalid stage"}), 400

        prefix = "in_review/"

        results = {}

        prefix = f"{stage}/"

        for blob in container.list_blobs(name_starts_with=prefix):
            parts = blob.name.split("/")

            # NEW STRUCTURE:
            # in_review/{workflow}_workflow/vendor=X/submission_type=Y/submission=Z/analytics/...

            if len(parts) < 7:
                continue

            workflow_part = parts[1]               # product_workflow
            vendor_part = parts[2]                # vendor=...
            submission_type_part = parts[3]       # submission_type=...
            submission_part = parts[4]            # submission=...

            if not vendor_part.startswith("vendor="):
                continue
            if not submission_part.startswith("submission="):
                continue

            vendor = vendor_part.replace("vendor=", "")
            submission_id = submission_part.replace("submission=", "")
            workflow = workflow_part.replace("_workflow","")
            submission_type = submission_type_part.replace("submission_type=", "")

            # Only load SCORECARD (contains all summary metrics)
            if "vendor_scorecard" in blob.name and blob.name.endswith(".xlsx"):
                blob_client = container.get_blob_client(blob.name)
                raw = blob_client.download_blob().readall()

                df = pd.read_excel(BytesIO(raw))
                df = df.where(pd.notnull(df), None)

                # Keep latest submission per vendor
                if vendor not in results or submission_id > results[vendor]["submission_id"]:
                    record = df.to_dict(orient="records")[0]
                    record["vendor"] = vendor
                    record["submission_id"] = submission_id
                    record["stage"] = stage
                    record["workflow"] = workflow
                    record["submission_type"] = submission_type
                    results[vendor] = record

        return jsonify(list(results.values()))

    except Exception as e:
        return jsonify({"error": str(e)}), 500
def read_json(container, path):
    import json
    blob_client = container.get_blob_client(path)
    data = blob_client.download_blob().readall()
    return json.loads(data)

# def get_gold_container():
#     svc = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
#     return svc.get_container_client("gold")

@admin_bp.route("/api/admin/submissions")
@login_required
def get_admin_submissions():
    if current_user.role != "admin_team":
        return jsonify({"error": "Unauthorized"}), 403

    try:
        gold_container = get_gold_container(AZURE_CONN_STR)

        # collect vendors from silver
        vendors = set()

        for blob in container.list_blobs():
            if "vendor=" not in blob.name:
                continue

            for part in blob.name.split("/"):
                if part.startswith("vendor="):
                    vendors.add(part.replace("vendor=", ""))
                    break

        results = []

        for vendor in sorted(vendors):
            state = compute_vendor_pipeline_state(
                vendor=vendor,
                silver_container=container,
                bronze_container=bronze_container,
                gold_container=gold_container
            )

            current_result = {
                "vendor": vendor,
                "display": state["display"],
                "ids": state["ids"],
                "workflows": state["workflows"],
                "global": state["global"]
            }

            # keep current reset-to-grey behavior after gold
            if state["global"]["gold"] == "success":
                current_result["workflows"] = {
                    "pricing": {"pre_review": "not_started", "review": "not_started"},
                    "products": {"pre_review": "not_started", "review": "not_started"},
                    "assets": {"pre_review": "not_started", "review": "not_started"},
                }
                current_result["global"] = {
                    "merge": "not_started",
                    "integrity": "not_started",
                    "category": "not_started",
                    "gold": "not_started"
                }

            results.append(current_result)

        latest_only_param = request.args.get("latest_only", "true").lower() in ("1", "true")

        if not latest_only_param:
            for vendor in sorted(vendors):
                history_path = f"approved/unified_workflow/vendor={vendor}/_history.json"

                try:
                    history = read_json(container, history_path)

                    for h in history:
                        results.append({
                            "vendor": vendor,
                            "display": h.get("display", {}),
                            "ids": h.get("ids", {}),
                            "workflows": h.get("workflows", {}),
                            "global": h.get("global", {}),
                            "is_history": True,
                            "timestamp": h.get("timestamp")
                        })
                except:
                    pass

        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500