from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required, current_user
import os
from azure.storage.blob import BlobServiceClient
from flask import request
import json
from io import BytesIO
import pyarrow.parquet as pq
import pandas as pd

admin_bp = Blueprint("admin", __name__)

AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = os.getenv("SILVER_CONTAINER", "silver")

blob_service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
container = blob_service.get_container_client(SILVER_CONTAINER)

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
        stage = request.args.get("stage", "post_pricing_review")

        if stage not in ["in_review", "post_pricing_review"]:
            return jsonify({"error": "Invalid stage"}), 400

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

@admin_bp.route("/api/admin/submissions")
@login_required
def get_admin_submissions():
    if current_user.role != "admin_team":
        return jsonify({"error": "Unauthorized"}), 403

    try:
        from collections import defaultdict

        # =========================================
        # 1. SCAN ALL BLOBS WITH TIMESTAMP
        # =========================================
        vendor_data = defaultdict(lambda: {
            "workflows": {
                "pricing": [],
                "products": [],
                "assets": []
            },
            "global": {}
        })

        for blob in container.list_blobs():
            name = blob.name
            last_modified = blob.last_modified

            if "vendor=" not in name:
                continue

            parts = name.split("/")

            vendor = None
            workflow = None
            submission_id = None
            submission_type = None

            for part in parts:
                if part.startswith("vendor="):
                    vendor = part.replace("vendor=", "")
                elif part.endswith("_workflow"):
                    workflow = part.replace("_workflow", "")
                elif part.startswith("submission="):
                    submission_id = part.replace("submission=", "")
                elif part.startswith("submission_type="):
                    submission_type = part.replace("submission_type=", "")

            if not vendor:
                continue

            # =========================================
            # WORKFLOW BLOBS
            # =========================================
            if workflow and submission_id:
                vendor_data[vendor]["workflows"][workflow].append({
                    "blob": name,
                    "submission_id": submission_id,
                    "submission_type": submission_type,
                    "last_modified": last_modified
                })

            # =========================================
            # GLOBAL BLOBS
            # =========================================
            if "approved/unified_workflow" in name:
                vendor_data[vendor]["global"]["merge"] = last_modified

            if "approved/unified_integrity" in name:
                vendor_data[vendor]["global"]["integrity"] = {
                    "time": last_modified,
                    "blob": name
                }

            if "selected/unified_workflow" in name:
                vendor_data[vendor]["global"]["gold"] = last_modified

            if "_category_completion.json" in name:
                vendor_data[vendor]["global"]["category_completion"] = {
                    "time": last_modified,
                    "blob": name
                }

            if name.endswith(f"category_queue/vendor={vendor}/active/delta_mapped.parquet") \
                or name.endswith(f"category_queue/vendor={vendor}/active/delta_mapped.xlsx") \
                or name.endswith(f"category_queue/vendor={vendor}/active/decisions.parquet"):

                    vendor_data[vendor]["global"].setdefault("category_active", []).append(name)

        # =========================================
        # 2. BUILD FINAL RESULTS
        # =========================================
        results = []

        for vendor, data in vendor_data.items():

            workflows_result = {}

            latest_workflow_time = None

            # -----------------------------------------
            # PER WORKFLOW STATUS
            # -----------------------------------------
            for wf, blobs in data["workflows"].items():

                if not blobs:
                    workflows_result[wf] = {"pre_review": "not_started", "review": "not_started"}
                    continue

                # latest submission
                latest = max(blobs, key=lambda x: x["last_modified"])

                latest_time = latest["last_modified"]
                if not latest_workflow_time or latest_time > latest_workflow_time:
                    latest_workflow_time = latest_time

                blob_names = [b["blob"] for b in blobs]

                # detection
                has_ingestion = any("raw/" in b for b in blob_names)
                has_mapping = any("mapped/mapped.xlsx" in b or "_asset_manifest.json" in b for b in blob_names)
                has_etl = any("etl_mapped.xlsx" in b or "transformed_assets.zip" in b for b in blob_names)

                if latest["submission_type"] and "review" in latest["submission_type"]:
                    status = "success" if (has_ingestion and has_mapping and has_etl) else "in_progress"
                    workflows_result[wf] = {"pre_review": "done", "review": status}
                else:
                    status = "success" if (has_ingestion and has_mapping and has_etl) else "in_progress"
                    workflows_result[wf] = {"pre_review": status, "review": "not_started"}

            # -----------------------------------------
            # GLOBAL STATES
            # -----------------------------------------
            global_data = data["global"]

            merge_time = global_data.get("merge")
            integrity_data = global_data.get("integrity")
            gold_time = global_data.get("gold")
            category_completion = global_data.get("category_completion")
            category_active = global_data.get("category_active", [])

            # MERGE
            if not merge_time:
                merge_status = "not_started"
            elif latest_workflow_time and merge_time >= latest_workflow_time:
                merge_status = "success"
            else:
                merge_status = "in_progress"

            # INTEGRITY
            if not integrity_data:
                integrity_status = "not_started"
            elif merge_time and integrity_data["time"] >= merge_time:
                integrity_status = "success"
            else:
                integrity_status = "in_progress"

            # CATEGORY
            if category_active:
                category_status = "in_progress"
            elif not category_completion:
                category_status = "not_started"
            elif merge_time and category_completion["time"] >= merge_time:
                category_status = "success"
            else:
                category_status = "not_started"

            # GOLD
            if not gold_time:
                gold_status = "not_started"
            elif integrity_data and gold_time >= integrity_data["time"]:
                gold_status = "success"
            else:
                gold_status = "in_progress"

            results.append({
                "vendor": vendor,
                "workflows": workflows_result,
                "global": {
                    "merge": merge_status,
                    "integrity": integrity_status,
                    "category": category_status,
                    "gold": gold_status
                }
            })

        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500