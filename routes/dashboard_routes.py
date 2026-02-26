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

    if any("ready_pricing_review" in b for b in blob_names):
        return "ready_pricing_review"

    if any("post_pricing_review" in b for b in blob_names):
        return "post_pricing_review"

    if any("ready/vendor=" in b for b in blob_names):
        return "ready"

    if any("in_review/vendor=" in b for b in blob_names):
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
        "post_review_etl": "pending",
        "pricing": "pending",
        "category": "pending",
        "analytics": "pending",
    }

    if stage == "rejected":
        pipeline["ingestion"] = "failed"
        pipeline["validation"] = "failed"
        return pipeline

    # Original ETL artifacts
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

    if any("ready_pricing_review" in b and "etl_mapped.xlsx" in b for b in blob_names):
        pipeline["post_review_etl"] = "success"

    if stage in ["in_review", "ready", "post_pricing_review",
                 "ready_pricing_review", "category_queue", "approved"]:
        pipeline["validation"] = "success"
        pipeline["ingestion"] = "success"

    if stage == "post_pricing_review":
        pipeline["pricing"] = "processing"

    elif stage in ["ready_pricing_review", "category_queue", "approved"]:
        pipeline["pricing"] = "approved"

    if stage == "category_queue":
        pipeline["category"] = "pending"

    if any("approved/logs" in b for b in blob_names):
        pipeline["category"] = "approved"

    analytics_prefix = (
    f"analytics/vendor_scorecard/vendor={vendor}/submission={submission_id}/"
)

    analytics_files = list(container.list_blobs(name_starts_with=analytics_prefix))

    if any("vendor_scorecard" in blob.name for blob in analytics_files):
        pipeline["analytics"] = "generated"

    return pipeline

# =========================================================
# ADMIN UI PAGE
# =========================================================
@admin_bp.route("/admin")
@login_required
def admin_home():
    if current_user.role != "admin":
        return "Unauthorized", 403
    return render_template("admin_home.html")

@admin_bp.route("/admin/submissions")
@login_required
def admin_submissions_page():
    if current_user.role != "admin":
        return "Unauthorized", 403

    return render_template("admin_submission.html")

@admin_bp.route("/admin/dashboard")
@login_required
def admin_dashboard_page():
    if current_user.role != "admin":
        return "Unauthorized", 403

    return render_template("admin_dashboard.html")


# =========================================================
# ADMIN API
# =========================================================

@admin_bp.route("/api/admin/summary")
@login_required
def get_admin_summary():
    if current_user.role != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    try:
        stage = request.args.get("stage", "post_pricing_review")

        if stage not in ["in_review", "post_pricing_review"]:
            return jsonify({"error": "Invalid stage"}), 400

        results = {}

        prefix = f"{stage}/"

        for blob in container.list_blobs(name_starts_with=prefix):
            parts = blob.name.split("/")

            # Expect:
            # stage/vendor=X/submission=Y/analytics/vendor_scorecard/...
            if len(parts) < 6:
                continue

            vendor_part = parts[1]
            submission_part = parts[2]

            if not vendor_part.startswith("vendor="):
                continue
            if not submission_part.startswith("submission="):
                continue

            vendor = vendor_part.replace("vendor=", "")
            submission_id = submission_part.replace("submission=", "")

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
                    results[vendor] = record

        return jsonify(list(results.values()))

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@admin_bp.route("/api/admin/submissions")
@login_required
def get_admin_submissions():
    if current_user.role != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    try:
        results = {}

        # 🔥 SINGLE CONTAINER SCAN
        all_blobs = container.list_blobs()

        submission_map = {}

        for blob in all_blobs:
            name = blob.name

            if "vendor=" not in name or "submission=" not in name:
                continue

            parts = name.split("/")

            vendor = None
            submission_id = None

            for part in parts:
                if part.startswith("vendor="):
                    vendor = part.replace("vendor=", "")
                if part.startswith("submission="):
                    submission_id = part.replace("submission=", "")

            if not vendor or not submission_id:
                continue

            key = (vendor, submission_id)

            if key not in submission_map:
                submission_map[key] = set()

            submission_map[key].add(name)

        # 🔥 Now build results from memory (FAST)
        for (vendor, submission_id), blob_names in submission_map.items():

            stage = detect_stage_fast(vendor, submission_id, blob_names)
            failure = detect_failure_fast(blob_names)
            pipeline = build_pipeline_state_fast(vendor, submission_id, stage, blob_names)

            results[(vendor, submission_id)] = {
                "vendor": vendor,
                "submission_id": submission_id,
                "stage": stage,
                "failure": failure,
                "pipeline": pipeline,
            }

        final_results = list(results.values())
        final_results.sort(key=lambda x: x["submission_id"], reverse=True)

        # latest_only filter
        latest_only_param = request.args.get("latest_only", "1").lower() in ("1", "true", "yes")

        if latest_only_param:
            latest_only = {}
            for r in final_results:
                v = r["vendor"]
                if v not in latest_only:
                    latest_only[v] = r
            final_results = list(latest_only.values())

        return jsonify(final_results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500