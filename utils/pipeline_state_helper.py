# utils/pipeline_state_helper.py
from collections import defaultdict
from azure.storage.blob import BlobServiceClient


def read_json(container, path):
    import json
    blob_client = container.get_blob_client(path)
    data = blob_client.download_blob().readall()
    return json.loads(data)


def get_gold_container(azure_conn_str: str):
    svc = BlobServiceClient.from_connection_string(azure_conn_str)
    return svc.get_container_client("gold")


def _blob_exists(container, path):
    try:
        container.get_blob_client(path).get_blob_properties()
        return True
    except:
        return False


def _get_blob_time(container, path):
    try:
        return container.get_blob_client(path).get_blob_properties().last_modified
    except:
        return None


def _has_blob_after(container, prefix, reset_time):
    for blob in container.list_blobs(name_starts_with=prefix):
        if not reset_time or blob.last_modified >= reset_time:
            return True
    return False


def _latest_blob_time_after(container, prefix, reset_time):
    latest = None
    for blob in container.list_blobs(name_starts_with=prefix):
        if reset_time and blob.last_modified < reset_time:
            continue
        if latest is None or blob.last_modified > latest:
            latest = blob.last_modified
    return latest


def _get_latest_raw_submission(bronze_container, vendor, wf, submission_type):
    prefix = (
        f"raw/vendor={vendor}/workflow={wf}/"
        f"submission_type={submission_type}/"
    )

    latest_submission_id = None
    submission_time = None
    seen = set()

    for blob in bronze_container.list_blobs(name_starts_with=prefix):
        parts = blob.name.split("/")
        for p in parts:
            if p.startswith("submission="):
                submission_id = p.replace("submission=", "")
                if submission_id in seen:
                    continue
                seen.add(submission_id)

                if not latest_submission_id or submission_id > latest_submission_id:
                    latest_submission_id = submission_id
                    submission_time = blob.last_modified

    return latest_submission_id, submission_time


def compute_vendor_pipeline_state(
    vendor,
    silver_container,
    bronze_container,
    gold_container,
):
    vendor_data = {
        "workflows": {
            "pricing": [],
            "products": [],
            "assets": []
        },
        "global": {}
    }

    # -------------------------------------------------
    # 1. scan silver blobs for this vendor only
    # -------------------------------------------------
    for blob in silver_container.list_blobs():
        name = blob.name
        last_modified = blob.last_modified

        if f"vendor={vendor}" not in name:
            continue

        parts = name.split("/")

        parsed_vendor = None
        workflow = None
        submission_id = None
        submission_type = None

        for part in parts:
            if part.startswith("vendor="):
                parsed_vendor = part.replace("vendor=", "")
            elif part.endswith("_workflow"):
                workflow = part.replace("_workflow", "")
            elif part.startswith("submission="):
                submission_id = part.replace("submission=", "")
            elif part.startswith("submission_type="):
                submission_type = part.replace("submission_type=", "")

        if parsed_vendor != vendor:
            continue

        if workflow and submission_id and workflow in vendor_data["workflows"]:
            vendor_data["workflows"][workflow].append({
                "blob": name,
                "submission_id": submission_id,
                "submission_type": submission_type,
                "last_modified": last_modified
            })

        if f"approved/unified_workflow/vendor={vendor}/" in name:
            vendor_data["global"]["merge"] = max(
                vendor_data["global"].get("merge", last_modified),
                last_modified
            )

        if f"approved/unified_integrity/vendor={vendor}/" in name:
            current = vendor_data["global"].get("integrity")
            if not current or last_modified > current["time"]:
                vendor_data["global"]["integrity"] = {
                    "time": last_modified,
                    "blob": name
                }

        if name.endswith(f"category_queue/vendor={vendor}/_category_completion.json"):
            vendor_data["global"]["category_completion"] = {
                "time": last_modified,
                "blob": name
            }

        if (
            name.endswith(f"category_queue/vendor={vendor}/active/delta_mapped.parquet")
            or name.endswith(f"category_queue/vendor={vendor}/active/delta_mapped.xlsx")
            or name.endswith(f"category_queue/vendor={vendor}/active/decisions.parquet")
        ):
            vendor_data["global"].setdefault("category_active", []).append({
                "blob": name,
                "last_modified": last_modified
            })

        if f"approved/assets_workflow/vendor={vendor}/part_number=" in name:
            vendor_data["global"].setdefault("approved_assets", []).append({
                "blob": name,
                "last_modified": last_modified
            })

    global_data = vendor_data["global"]

    merge_time = global_data.get("merge")
    integrity_data = global_data.get("integrity")
    category_completion = global_data.get("category_completion")
    category_active_all = global_data.get("category_active", [])

    gold_time = _get_blob_time(
        gold_container,
        f"selected/unified_workflow/vendor={vendor}/unified_etl_mapped.xlsx"
    )
    reset_time = gold_time

    # only active-after-reset queue files should affect UI
    category_active = [
        x for x in category_active_all
        if not reset_time or x["last_modified"] >= reset_time
    ]

    # -------------------------------------------------
    # 2. global states
    # -------------------------------------------------
    has_active_run = False
    for wf, blobs in vendor_data["workflows"].items():
        if any((not gold_time or b["last_modified"] >= gold_time) for b in blobs):
            has_active_run = True
            break

    if not has_active_run:
        merge_status = "not_started"
    elif category_active:
        merge_status = "success"
    elif merge_time and (not reset_time or merge_time >= reset_time):
        merge_status = "in_progress"
    else:
        merge_status = "in_progress"

    def is_fresh(file_time, ref_time):
        return file_time and ref_time and file_time >= ref_time

    # 🔥 FIXED LOGIC

    if not merge_time or (reset_time and merge_time < reset_time):
        integrity_status = "not_started"

    elif not integrity_data:
        # 🔥 KEY FIX: merge done but no integrity file → must be in progress
        integrity_status = "in_progress"

    else:
        try:
            summary_path = f"approved/unified_integrity/vendor={vendor}/integrity_summary.json"
            summary = read_json(silver_container, summary_path)
            integrity_time = integrity_data["time"]

            if reset_time and integrity_time < reset_time:
                integrity_status = "not_started"

            elif not summary.get("can_publish", False):
                integrity_status = "failed"

            else:
                integrity_status = "success"

        except:
            integrity_status = "failed"

    if integrity_status == "failed":
        category_status = "failed"
    elif category_active:
        category_status = "in_progress"
    elif not category_completion:
        category_status = "not_started"
    elif reset_time and category_completion["time"] < reset_time:
        category_status = "not_started"
    elif is_fresh(category_completion["time"], merge_time):
        category_status = "success"
    else:
        category_status = "not_started"

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

    # -------------------------------------------------
    # 3. workflow states + ids/display
    # -------------------------------------------------
    workflows_result = {}
    ids = {
        "product_review_id": None,
        "pricing_review_id": None,
        "asset_review_id": None,
        "category_review_id": None,
    }

    display = {
        "products": None,
        "pricing": None,
        "assets": None,
        "category": None,
    }

    submission_type_map = {
        "products": "product_submission",
        "pricing": "pricing_submission",
        "assets": "asset_submission"
    }

    review_submission_type_map = {
        "products": "product_review",
        "pricing": "pricing_review",
        "assets": "asset_review"
    }

    approved_meta_map = {
        "products": "product_review_id",
        "pricing": "pricing_review_id",
        "assets": "asset_review_id"
    }

    for wf in ["products", "pricing", "assets"]:
        # pre-review
        latest_submission_id, submission_time = _get_latest_raw_submission(
            bronze_container,
            vendor,
            wf,
            submission_type_map[wf]
        )

        pre_status = "not_started"

        if latest_submission_id:
            is_new = not reset_time or (submission_time and submission_time >= reset_time)

            if is_new:
                if wf in ["products", "pricing"]:
                    ready_path = (
                        f"ready/{wf}_workflow/vendor={vendor}/"
                        f"submission_type={submission_type_map[wf]}/"
                        f"submission={latest_submission_id}/review/etl_mapped.parquet"
                    )
                    pre_status = "success" if _blob_exists(silver_container, ready_path) else "in_progress"

                elif wf == "assets":
                    asset_prefix = (
                        f"ready/assets_workflow/vendor={vendor}/"
                        f"submission_type=asset_submission/"
                        f"submission={latest_submission_id}/assets/"
                    )
                    pre_status = "success" if _has_blob_after(silver_container, asset_prefix, None) else "in_progress"

        # review
        review_status = "not_started"

        # raw review must respect reset_time
        _, raw_review_time = _get_latest_raw_submission(
            bronze_container,
            vendor,
            wf,
            review_submission_type_map[wf]
        )
        has_raw_review = raw_review_time is not None and (not reset_time or raw_review_time >= reset_time)

        if wf in ["products", "pricing"]:
            approved_path = f"approved/{wf}_workflow/vendor={vendor}/{wf}_etl_mapped.parquet"
            approved_time = _get_blob_time(silver_container, approved_path)

            if approved_time and (not reset_time or approved_time >= reset_time):
                review_status = "success"

            try:
                meta_path = f"approved/{wf}_workflow/vendor={vendor}/_meta.json"
                meta = read_json(silver_container, meta_path)
                review_id = meta.get("review_submission_id")
                ids[approved_meta_map[wf]] = review_id
                display[wf] = review_id[-6:] if review_id else None
            except:
                pass

        elif wf == "assets":
            approved_prefix = f"approved/assets_workflow/vendor={vendor}/part_number="
            latest_asset_time = _latest_blob_time_after(silver_container, approved_prefix, reset_time)

            if latest_asset_time:
                review_status = "success"

            try:
                meta_path = f"approved/assets_workflow/vendor={vendor}/_meta.json"
                meta = read_json(silver_container, meta_path)
                review_id = meta.get("review_submission_id")
                ids["asset_review_id"] = review_id
                display["assets"] = review_id[-6:] if review_id else None
            except:
                pass

        if review_status != "success" and has_raw_review:
            review_status = "in_progress"

        workflows_result[wf] = {
            "pre_review": pre_status,
            "review": review_status
        }

    # category id/display from latest category review log
    latest_category_id = None
    latest_category_time = None
    category_prefix = f"approved/logs/vendor={vendor}/category_review/"
    for blob in silver_container.list_blobs(name_starts_with=category_prefix):
        if reset_time and blob.last_modified < reset_time:
            continue
        file_name = blob.name.split("/")[-1]
        if file_name.endswith(".json"):
            category_id = file_name.replace(".json", "")
            if not latest_category_id or category_id > latest_category_id:
                latest_category_id = category_id
                latest_category_time = blob.last_modified

    ids["category_review_id"] = latest_category_id
    display["category"] = latest_category_id[-6:] if latest_category_id else None

    return {
        "vendor": vendor,
        "display": display,
        "ids": ids,
        "workflows": workflows_result,
        "global": {
            "merge": merge_status,
            "integrity": integrity_status,
            "category": category_status,
            "gold": gold_status
        },
        "times": {
            "gold_time": gold_time,
            "reset_time": reset_time,
            "latest_category_time": latest_category_time
        }
    }