from flask import Blueprint, jsonify, render_template
from flask_login import login_required, current_user
import os
from azure.storage.blob import BlobServiceClient

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
    stage_paths = {
        "rejected": f"rejected/logs/vendor={vendor}/submission={submission_id}/",
        "post_pricing_review": f"post_pricing_review/vendor={vendor}/submission={submission_id}/",
        "ready": f"ready/vendor={vendor}/submission={submission_id}/",
        "in_review": f"in_review/vendor={vendor}/submission={submission_id}/",
    }

    for stage, prefix in stage_paths.items():
        if any(container.list_blobs(name_starts_with=prefix)):
            return stage

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

    pipeline = {
        "ingestion": "unknown",
        "validation": "unknown",
        "mapping": "pending",
        "media": "pending",
        "profiling": "pending",
        "autofix": "pending",
        "integrity": "pending",
        "etl": "pending",
        "pricing": "pending",
        "analytics": "pending",
    }

    # REJECTED
    if stage == "rejected":
        pipeline["ingestion"] = "failed"
        pipeline["validation"] = "failed"
        return pipeline

    # IN REVIEW
    if stage in ["in_review", "ready", "post_pricing_review"]:
        pipeline["ingestion"] = "success"
        pipeline["validation"] = "success"

        pipeline["mapping"] = (
            "success" if blob_exists(f"{base_in_review}mapped/mapped.xlsx") else "missing"
        )

        pipeline["media"] = (
            "success" if blob_exists(f"{base_in_review}canonical/media_canonical.xlsx") else "missing"
        )

        pipeline["profiling"] = (
            "success" if blob_exists(f"{base_in_review}profiling/health_issues.xlsx") else "missing"
        )

        pipeline["autofix"] = (
            "success" if blob_exists(f"{base_in_review}autofix/autofix_report.xlsx") else "missing"
        )

        pipeline["integrity"] = (
            "success" if blob_exists(f"{base_in_review}integrity/integrity_report.xlsx") else "missing"
        )

    # READY
    if stage in ["ready", "post_pricing_review"]:
        pipeline["etl"] = (
            "success" if blob_exists(f"{base_ready}review/etl_mapped.xlsx") else "missing"
        )

    # POST PRICING
    if stage == "post_pricing_review":
        pipeline["pricing"] = "approved"
        pipeline["etl"] = (
            "success" if blob_exists(f"{base_post}etl_mapped.xlsx") else "missing"
        )
    elif stage == "ready":
        pipeline["pricing"] = "pending"

    # ANALYTICS
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

        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500