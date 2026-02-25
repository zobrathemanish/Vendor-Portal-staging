from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required, current_user
import os
from azure.storage.blob import BlobServiceClient
from flask import request

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

def detect_stage(vendor, submission_id):

    # APPROVED
    if blob_exists(
        f"approved/logs/vendor={vendor}/submission={submission_id}/promotion_log.json"
    ):
        return "approved"

    # CATEGORY QUEUE (vendor-level)
    if blob_exists(f"category_queue/vendor={vendor}/etl_mapped.xlsx"):
        return "category_queue"

    # READY PRICING REVIEW (final pricing output)
    if any(container.list_blobs(
        name_starts_with=f"ready_pricing_review/vendor={vendor}/submission={submission_id}/"
    )):
        return "ready_pricing_review"

    # POST PRICING (processing)
    if any(container.list_blobs(
        name_starts_with=f"post_pricing_review/vendor={vendor}/submission={submission_id}/"
    )):
        return "post_pricing_review"

    # READY
    if any(container.list_blobs(
        name_starts_with=f"ready/vendor={vendor}/submission={submission_id}/"
    )):
        return "ready"

    # IN REVIEW
    if any(container.list_blobs(
        name_starts_with=f"in_review/vendor={vendor}/submission={submission_id}/"
    )):
        return "in_review"

    # REJECTED
    if any(container.list_blobs(
        name_starts_with=f"rejected/logs/vendor={vendor}/submission={submission_id}/"
    )):
        return "rejected"

    return "bronze_only"

def detect_failure(vendor, submission_id):
    prefix = f"rejected/logs/vendor={vendor}/submission={submission_id}/"
    blobs = container.list_blobs(name_starts_with=prefix)

    for blob in blobs:
        if "VALIDATION" in blob.name:
            return "validation"
        if "INGESTION" in blob.name:
            return "ingestion"

    return None


# =========================================================
# STAGE-AWARE PROGRESS
# =========================================================

def build_pipeline_state(vendor, submission_id, stage):

    base_in_review = f"in_review/vendor={vendor}/submission={submission_id}/"
    base_ready = f"ready/vendor={vendor}/submission={submission_id}/"
    base_post = f"post_pricing_review/vendor={vendor}/submission={submission_id}/"
    base_ready_pricing = f"ready_pricing_review/vendor={vendor}/submission={submission_id}/review/"
    base_category_queue = f"category_queue/vendor={vendor}/etl_mapped.xlsx"
    base_approved = f"approved/logs/vendor={vendor}/submission={submission_id}/promotion_log.json"

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
        "post_review_etl": "pending",   # NEW
        "pricing": "pending",
        "category": "pending",
        "analytics": "pending",
    }

    # --------------------------------------------------
    # FAILURE (hard override)
    # --------------------------------------------------
    if stage == "rejected":
        pipeline["ingestion"] = "failed"
        pipeline["validation"] = "failed"
        return pipeline

    # --------------------------------------------------
    # ORIGINAL ETL PASS (IN_REVIEW → READY)
    # --------------------------------------------------
    if blob_exists(f"{base_in_review}mapped/mapped.xlsx"):
        pipeline["mapping"] = "success"

    if blob_exists(f"{base_in_review}canonical/media_canonical.xlsx"):
        pipeline["media"] = "success"

    if blob_exists(f"{base_in_review}profiling/health_issues.xlsx"):
        pipeline["profiling"] = "success"

    if blob_exists(f"{base_in_review}autofix/autofix_report.xlsx"):
        pipeline["autofix"] = "success"

    if blob_exists(f"{base_in_review}integrity/integrity_report.xlsx"):
        pipeline["integrity"] = "success"

    # Validation + Ingestion considered successful once in_review reached
    if stage in ["in_review", "ready", "post_pricing_review", 
                 "ready_pricing_review", "category_queue", "approved"]:
        pipeline["validation"] = "success"
        pipeline["ingestion"] = "success"

    # READY STAGE ETL
    if blob_exists(f"{base_ready}review/etl_mapped.xlsx"):
        pipeline["etl"] = "success"

    if blob_exists(f"{base_ready}review/delta_mapped.xlsx"):
        pipeline["delta"] = "success"

    # --------------------------------------------------
    # POST PRICING REVIEW PROCESSING (SECOND PASS PREP)
    # --------------------------------------------------
    if blob_exists(f"{base_post}profiling/health_issues.xlsx"):
        pipeline["profiling"] = "success"

    if blob_exists(f"{base_post}autofix/autofix_report.xlsx"):
        pipeline["autofix"] = "success"

    if blob_exists(f"{base_post}integrity_report.xlsx"):
        pipeline["integrity"] = "success"

    # --------------------------------------------------
    # POST REVIEW FINAL ETL OUTPUT
    # --------------------------------------------------
    if blob_exists(f"{base_ready_pricing}etl_mapped.xlsx"):
        pipeline["post_review_etl"] = "success"

    if blob_exists(f"{base_ready_pricing}delta_mapped.xlsx"):
        pipeline["delta"] = "success"

    # Turn before artifacts green
    if stage in ["ready_pricing_review", "category_queue", "approved"]:
        pipeline["mapping"] = "success"
        pipeline["media"] = "success"
        pipeline["profiling"] = "success"
        pipeline["autofix"] = "success"
        pipeline["integrity"] = "success"
        pipeline["etl"] = "success"

    # --------------------------------------------------
    # PRICING STATE (LIFECYCLE)
    # --------------------------------------------------
    if stage == "post_pricing_review":
        pipeline["pricing"] = "processing"

    elif stage in ["ready_pricing_review", "category_queue", "approved"]:
        pipeline["pricing"] = "approved"

    elif stage == "ready":
        pipeline["pricing"] = "pending"

    # --------------------------------------------------
    # CATEGORY STATE
    # --------------------------------------------------
    if stage == "category_queue":
        pipeline["category"] = "pending"

    if blob_exists(base_approved):
        pipeline["category"] = "approved"

    # --------------------------------------------------
    # ANALYTICS
    # --------------------------------------------------
    scorecard_path = (
        f"analytics/vendor_scorecard/vendor={vendor}/"
        f"submission={submission_id}/"
        f"vendor_scorecard_{submission_id}.xlsx"
    )

    if blob_exists(scorecard_path):
        pipeline["analytics"] = "generated"

    return pipeline


# =========================================================
# ADMIN UI PAGE
# =========================================================

@admin_bp.route("/admin/submissions")
@login_required
def admin_submissions_page():
    if current_user.role != "admin":
        return "Unauthorized", 403

    return render_template("admin_submission.html")


# =========================================================
# ADMIN API
# =========================================================

@admin_bp.route("/api/admin/submissions")
@login_required
def get_admin_submissions():
    if current_user.role != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    try:
        results = []

        all_submissions = list_all_submission_prefixes()

        for vendor, submission_id in all_submissions:
            stage = detect_stage(vendor, submission_id)
            failure = detect_failure(vendor, submission_id)
            pipeline = build_pipeline_state(vendor, submission_id, stage)

            results.append({
                "vendor": vendor,
                "submission_id": submission_id,
                "stage": stage,
                "failure": failure,
                "pipeline": pipeline,
            })

        results.sort(key=lambda x: x["submission_id"], reverse=True)

        # latest_only=1 (default) keeps only newest submission per vendor
        latest_only_param = request.args.get("latest_only", "1").lower() in ("1", "true", "yes")

        if latest_only_param:
            latest_only = {}
            for r in results:
                v = r["vendor"]
                if v not in latest_only:
                    latest_only[v] = r
            results = list(latest_only.values())

        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500