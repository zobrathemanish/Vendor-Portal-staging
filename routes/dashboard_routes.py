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

            # 🔥 extract workflow
            workflow = None
            for part in parts:
                if part.endswith("_workflow"):
                    workflow = part.replace("_workflow", "")

            key = (vendor, submission_id)

            if key not in submission_map:
                submission_map[key] = {
                    "blob_names": set(),
                    "workflows": {}
                }

            submission_map[key]["blob_names"].add(name)

            if workflow:
                if workflow not in submission_map[key]["workflows"]:
                    submission_map[key]["workflows"][workflow] = set()
                submission_map[key]["workflows"][workflow].add(name)

        # 🔥 Now build results from memory (FAST)
        for (vendor, submission_id), data in submission_map.items():

            blob_names = data["blob_names"]
            workflows = data["workflows"]

            stage = detect_stage_fast(vendor, submission_id, blob_names)
            failure = detect_failure_fast(blob_names)

            merged_pipeline = {}
            workflow_pipelines = {}

            # 🔥 build per-workflow pipelines
            for wf, wf_blobs in workflows.items():
                wf_stage = detect_stage_fast(vendor, submission_id, wf_blobs)
                wf_pipeline = build_pipeline_state_fast(vendor, submission_id, wf_stage, wf_blobs)

                workflow_pipelines[wf] = wf_pipeline

                # 🔥 merge pipelines
                for step, status in wf_pipeline.items():
                    if status == "success":
                        merged_pipeline[step] = "success"
                    elif step not in merged_pipeline:
                        merged_pipeline[step] = status

            results[(vendor, submission_id)] = {
                "vendor": vendor,
                "submission_id": submission_id,
                "stage": stage,
                "failure": failure,
                "pipeline": merged_pipeline,
                "workflows": workflow_pipelines
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

        print(f"DEBUG submissions returned: {len(results)}")

        return jsonify(final_results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500