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
def read_json(container, path):
    import json
    blob_client = container.get_blob_client(path)
    data = blob_client.download_blob().readall()
    return json.loads(data)

def get_gold_container():
    svc = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
    return svc.get_container_client("gold")

@admin_bp.route("/api/admin/submissions")
@login_required
def get_admin_submissions():
    if current_user.role != "admin_team":
        return jsonify({"error": "Unauthorized"}), 403

    try:
        from collections import defaultdict

        gold_container = get_gold_container()

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


            if "_category_completion.json" in name:
                vendor_data[vendor]["global"]["category_completion"] = {
                    "time": last_modified,
                    "blob": name
                }

            if name.endswith(f"category_queue/vendor={vendor}/active/delta_mapped.parquet") \
                or name.endswith(f"category_queue/vendor={vendor}/active/delta_mapped.xlsx") \
                or name.endswith(f"category_queue/vendor={vendor}/active/decisions.parquet"):

                    vendor_data[vendor]["global"].setdefault("category_active", []).append(name)

            if f"approved/assets_workflow/vendor={vendor}/part_number=" in name:
                vendor_data[vendor]["global"].setdefault("approved_assets", []).append({
                    "blob": name,
                    "last_modified": last_modified
                })
        # =========================================
        # 2. BUILD FINAL RESULTS
        # =========================================
        # =========================================
        # 2. BUILD FINAL RESULTS (FRESHNESS-BASED)
        # =========================================
        results = []

        def is_fresh(file_time, ref_time):
            return file_time and ref_time and file_time >= ref_time

        for vendor, data in vendor_data.items():

            workflows_result = {}
            latest_workflow_time = None

            # -----------------------------------------
            # GLOBAL STATES
            # -----------------------------------------
            global_data = data["global"]

            merge_time = global_data.get("merge")
        
            integrity_data = global_data.get("integrity")
            # 🔥 GET GOLD TIME FROM GOLD CONTAINER
            gold_time = None
            gold_path = f"selected/unified_workflow/vendor={vendor}/unified_etl_mapped.xlsx"

            try:
                blob = gold_container.get_blob_client(gold_path)
                gold_time = blob.get_blob_properties().last_modified
            except:
                gold_time = None
            category_completion = global_data.get("category_completion")
            category_active = global_data.get("category_active", [])

            # -----------------------------------------
            # MERGE
            # -----------------------------------------
            # -----------------------------------------
            # MERGE (FIXED)
            # -----------------------------------------

            # 🔥 Check if ANY workflow has new data after gold
            has_active_run = False

            for wf, blobs in data["workflows"].items():
                if any(
                    (not gold_time or b["last_modified"] >= gold_time)
                    for b in blobs
                ):
                    has_active_run = True
                    break

            if not has_active_run:
                merge_status = "not_started"

            elif category_active:
                merge_status = "success"

            elif merge_time:
                merge_status = "in_progress"

            else:
                merge_status = "in_progress"

            # -----------------------------------------
            # INTEGRITY
            # -----------------------------------------
            if not integrity_data:
                integrity_status = "not_started"
                integrity_time = None
            else:
                try:
                    summary_path = f"approved/unified_integrity/vendor={vendor}/integrity_summary.json"
                    summary = read_json(container, summary_path)

                    integrity_time = integrity_data["time"]

                    if not summary.get("can_publish", False):
                        integrity_status = "failed"

                    elif is_fresh(integrity_time, merge_time):
                        integrity_status = "success"

                    else:
                        integrity_status = "not_started"

                except:
                    integrity_status = "failed"
                    integrity_time = None

            # -----------------------------------------
            # CATEGORY
            # -----------------------------------------
            if integrity_status == "failed":
                category_status = "failed"

            elif category_active:
                category_status = "in_progress"

            elif not category_completion:
                category_status = "not_started"

            elif is_fresh(category_completion["time"], merge_time):
                category_status = "success"

            else:
                category_status = "not_started"

            # -----------------------------------------
            # GOLD (FIXED)
            # -----------------------------------------

            if not gold_time:
                gold_status = "not_started"

            elif category_status == "success":
                gold_status = "success"


            elif category_status == "in_progress":
                gold_status = "in_progress"

            elif category_status == "failed":
                gold_status = "not_started"

            else:
                gold_status = "not_started"

            # -----------------------------------------
            # HISTORY SNAPSHOT (ONLY WHEN GOLD JUST COMPLETED)
            # -----------------------------------------

            if gold_status == "success":
                history_path = f"approved/unified_workflow/vendor={vendor}/_history.json"

                try:
                    history = read_json(container, history_path)
                except:
                    history = []

                # 🔥 Avoid duplicate entries (important)
                if not history or history[-1].get("timestamp") != str(gold_time):

                    history.append({
                        "timestamp": str(gold_time),
                        "status": {
                            "merge": merge_status,
                            "integrity": integrity_status,
                            "category": category_status,
                            "gold": gold_status
                        }
                    })

                    container.upload_blob(
                        history_path,
                        json.dumps(history, indent=2),
                        overwrite=True
                    )

                # MERGE FILTER
                if merge_time and latest_workflow_time and merge_time < latest_workflow_time:
                    merge_time = None

                # INTEGRITY FILTER
                if integrity_data and latest_workflow_time and integrity_data["time"] < latest_workflow_time:
                    integrity_data = None

                # CATEGORY FILTER
                if category_completion and latest_workflow_time and category_completion["time"] < latest_workflow_time:
                    category_completion = None

            # -----------------------------------------
            # WORKFLOW STATES
            # -----------------------------------------
            for wf, blobs in data["workflows"].items():
                run_start_time = gold_time

                filtered_blobs = blobs
                if run_start_time:
                    filtered_blobs = [
                        b for b in blobs
                        if b["last_modified"] >= run_start_time
                    ]
                wf_latest_time = None
                if blobs:
                    wf_latest_time = max(b["last_modified"] for b in blobs)

                if not filtered_blobs:
                    workflows_result[wf] = {"pre_review": "not_started", "review": "not_started"}
                    continue

                latest = max(filtered_blobs, key=lambda x: x["last_modified"])
                latest_time = latest["last_modified"]

                blob_names = [b["blob"] for b in filtered_blobs]

                # PRE REVIEW
                has_ingestion = any("raw/" in b for b in blob_names)
                has_mapping = any("mapped/mapped.xlsx" in b or "_asset_manifest.json" in b for b in blob_names)
                has_etl = any("etl_mapped.xlsx" in b or "transformed_assets.zip" in b for b in blob_names)

                # PRE-REVIEW (STRICT — submission based only)

                submission_type = latest.get("submission_type", "") or ""

                is_pre_submission = (
                    submission_type.endswith("_submission")
                    or submission_type.startswith("delta_") and submission_type.endswith("_submission")
                )

                # -----------------------------------------
                # PRE REVIEW (FIXED - independent)
                # -----------------------------------------

                # 🔥 Detect submission from FULL history
                all_blob_names = [b["blob"] for b in blobs]

                has_ingestion = any("raw/" in b for b in all_blob_names)
                has_mapping = any("mapped/mapped.xlsx" in b or "_asset_manifest.json" in b for b in all_blob_names)
                has_etl = any("etl_mapped.xlsx" in b or "transformed_assets.zip" in b for b in all_blob_names)

                has_submission = any(
                    "submission_type=" in b and "submission" in b
                    for b in all_blob_names
                )

                # 🔥 Detect processing (current run)
                has_processing = any(
                    "/mapped/" in b or
                    "/canonical/" in b or
                    "/autofix/" in b or
                    "transformed_assets.zip" in b or
                    "_asset_manifest.json" in b
                    for b in blob_names  # filtered
                )

                if has_ingestion and has_mapping and has_etl:
                    pre_status = "success"
                elif has_processing:
                    pre_status = "in_progress"
                elif has_submission:
                    pre_status = "in_progress"
                else:
                    pre_status = "not_started"

                # -----------------------------------------
                # REVIEW (FIXED)
                # -----------------------------------------

                submission_type = latest.get("submission_type", "") or ""
                is_review = "review" in submission_type

                # 🔥 Detect processing phase (purple)
                has_processing = any(
                    "/mapped/" in b or
                    "/canonical/" in b or
                    "/autofix/" in b or
                    "/analytics/" in b or
                    "transformed_assets.zip" in b or
                    "_asset_manifest.json" in b
                    for b in blob_names
                )

                if is_review:

                    if wf == "assets":
                        approved_assets = global_data.get("approved_assets", [])

                        has_assets_approved = any(
                            b["last_modified"] >= latest_time
                            for b in approved_assets
                        )

                        if has_assets_approved:
                            review_status = "success"
                        elif has_processing:
                            review_status = "in_progress"
                        else:
                            review_status = "not_started"

                    else:
                        has_analytics = any(
                            "analytics/vendor_scorecard" in b or
                            "analytics/vendor_profiling" in b
                            for b in blob_names
                        )

                        if has_analytics:
                            review_status = "success"
                        elif has_processing:
                            review_status = "in_progress"
                        else:
                            review_status = "not_started"

                else:
                    # 🔥 IMPORTANT: Preserve previous review success
                    previous_success = any(
                        "analytics/vendor_scorecard" in b or
                        "analytics/vendor_profiling" in b
                        for b in blobs  # FULL history (not filtered)
                    )

                    review_status = "success" if previous_success else "not_started"

                workflows_result[wf] = {
                    "pre_review": pre_status,
                    "review": review_status
                }

            # -----------------------------------------
            # FINAL STATE RESET AFTER GOLD
            # -----------------------------------------

            if gold_status == "success":
                workflows_result = {
                    "pricing": {"pre_review": "not_started", "review": "not_started"},
                    "products": {"pre_review": "not_started", "review": "not_started"},
                    "assets": {"pre_review": "not_started", "review": "not_started"},
                }

                merge_status = "not_started"
                integrity_status = "not_started"
                category_status = "not_started"
                gold_status = "not_started"

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
                
        latest_only_param = request.args.get("latest_only", "true").lower() in ("1", "true")

        if not latest_only_param:
            for vendor in list(vendor_data.keys()):

                history_path = f"approved/unified_workflow/vendor={vendor}/_history.json"

                try:
                    history = read_json(container, history_path)

                    for h in history:
                        results.append({
                            "vendor": vendor,
                            "workflows": {},
                            "global": h["status"],
                            "is_history": True,
                            "timestamp": h["timestamp"]
                        })

                except:
                    pass

        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500