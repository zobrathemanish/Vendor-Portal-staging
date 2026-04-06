#asset_transformations.py
import os
import sys

# =========================================================
# PATH SETUP (must run BEFORE other imports)
# =========================================================

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# =========================================================
# NORMAL IMPORTS
# =========================================================

import json
import time
import shutil
import tempfile
from io import BytesIO
from datetime import datetime
from typing import Optional, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from PIL import Image
import pyarrow.parquet as pq
import pyarrow as pa
import hashlib

from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError

from common.status_writer import write_status

import zipfile
from io import BytesIO
# =========================================================
# PATH SETUP
# =========================================================

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# =========================================================
# CONFIG
# =========================================================

LOCAL_TEST_MODE = False
LOCAL_OUTPUT_BASE = "./_local_asset_test_output"

REVIEW_ROOT = "in_review"
APPROVED_ROOT = "ready"

AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
if not AZURE_CONN_STR:
    raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set")

SILVER_CONTAINER = "silver"

CANONICAL_TABLE = "canonical/media_canonical.parquet"

MIN_WIDTH = 800
MIN_HEIGHT = 800
FINAL_SIZE = 1000
JPEG_QUALITY = 90
MAX_IMAGE_BYTES = 25 * 1024 * 1024

BACKGROUND_COLOR = (255, 255, 255)

DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_BACKOFF_SEC = 1.5

ASSET_CACHE_BASE = os.getenv("ASSET_CACHE_BASE", "").strip() or None

# =========================================================
# AZURE INIT
# =========================================================

blob_service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)

# Silver container (processing layer)
container = blob_service.get_container_client(SILVER_CONTAINER)

# Gold container (final assets for ERP / PIM)
GOLD_CONTAINER = "gold"
gold_container = blob_service.get_container_client(GOLD_CONTAINER)


# =========================================================
# HELPERS
# =========================================================

def log(msg, indent=0):
    try:
        print(" " * indent + msg, flush=True)
    except UnicodeEncodeError:
        safe = msg.encode("ascii", "replace").decode()
        print(" " * indent + safe, flush=True)


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# =========================================================
# AZURE IO
# =========================================================

def download_blob(path: str) -> bytes:

    last_err = None

    for attempt in range(1, DOWNLOAD_RETRIES + 1):

        try:
            return container.get_blob_client(path).download_blob(max_concurrency=8).readall()

        except ResourceNotFoundError:
            raise

        except ServiceRequestError as e:
            last_err = e
            wait = DOWNLOAD_RETRY_BACKOFF_SEC ** attempt
            log(f"Retrying download {path} in {wait:.1f}s", 4)
            time.sleep(wait)

    raise last_err if last_err else RuntimeError("download_failed")


def upload_blob(path: str, data: bytes, metadata=None):

    container.upload_blob(
        path,
        data,
        overwrite=True,
        metadata=metadata,
        max_concurrency=8
    )


def write_output(path: str, data: bytes, metadata=None):

    if LOCAL_TEST_MODE:

        local_path = os.path.join(LOCAL_OUTPUT_BASE, path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        with open(local_path, "wb") as f:
            f.write(data)

    else:

        upload_blob(path, data, metadata)


def get_existing_blob_hash(path):

    try:

        props = container.get_blob_client(path).get_blob_properties()

        metadata = props.metadata or {}

        return metadata.get("content_hash")

    except ResourceNotFoundError:
        return None

#HELPER

def yes_no(value):
    if value in [True, 1, "TRUE", "true", "yes", "YES"]:
        return "Yes"
    return "No"

# =========================================================
# LOAD CANONICAL
# =========================================================

def load_media_canonical(vendor: str, submission_type: str, submission_id: str) -> pd.DataFrame:

    path = (
        f"in_review/assets_workflow/"
        f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/"
        f"canonical/media_canonical.parquet"
    )

    raw = download_blob(path)

    table = pq.read_table(BytesIO(raw))

    df = table.to_pandas()

    required = {
        "part_number",
        "media_category",
        "canonical_filename",
        "source_blob_path",
        "original_filename"
    }

    missing = required - set(df.columns)

    if missing:
        raise RuntimeError(f"canonical_table_missing_columns: {missing}")

    log(f"Loaded canonical rows: {len(df)}", 2)

    return df

# =========================================================
# VENDOR ACTION REPORT
# =========================================================

def create_vendor_action_report(vendor: str, submission_type:str, submission_id: str):

    log("Generating vendor action report", 2)

    rows = []

    # =====================================================
    # LOAD HEALTH REPORT (validation issues)
    # =====================================================

    health_path = (
        f"in_review/assets_workflow/"
        f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/logs/health_report.xlsx"
    )

    try:

        health_bytes = container.get_blob_client(health_path).download_blob().readall()
        df_health = pd.read_excel(BytesIO(health_bytes))

    except Exception:

        log("Health report not found — skipping vendor action report", 4)
        return
    
    # =====================================================
    # LOAD AUTOFIX REPORT
    # =====================================================

    autofix_path = (
        f"in_review/assets_workflow/"
        f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/logs/autofix_report.xlsx"
    )

    try:

        autofix_bytes = container.get_blob_client(autofix_path).download_blob().readall()
        df_autofix = pd.read_excel(BytesIO(autofix_bytes))

        for _, r in df_autofix.iterrows():

            rows.append({
                "filename": r.get("filename") or r.get("original_filename"),
                "issue": r.get("issue_type"),
                "severity": r.get("severity", "info"),
                "autofixable": "Yes",
                "action_taken": "fixed_automatically",
                "vendor_action_required": "none"
            })

    except Exception:

        df_autofix = pd.DataFrame()

    # =====================================================
    # LOAD INTEGRITY ISSUES (missing / extra assets)
    # =====================================================

    integrity_path = (
        f"in_review/assets_workflow/"
        f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/reports/asset_integrity_issues.json"
    )
    try:

        raw = container.get_blob_client(integrity_path).download_blob().readall()
        integrity_issues = json.loads(raw)

    except Exception:

        integrity_issues = []


    # =====================================================
    # LOAD TRANSFORMATION LOG
    # =====================================================

    transform_log_path = (
        f"in_review/assets_workflow/"
        f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/reports/asset_transform_log.json"
    )

    try:

        raw = container.get_blob_client(transform_log_path).download_blob().readall()
        transform_log = json.loads(raw)

        # =====================================================
        # ADD FORMAT CONVERSIONS
        # =====================================================

        for item in transform_log.get("format_conversions", []):

            rows.append({

                "filename": item.get("filename"),
                "issue": "format_normalized",
                "severity": "info",
                "autofixable": "Yes",
                "action_taken": "converted_to_jpg",
                "vendor_action_required": "none"

            })

    except Exception:

        transform_log = {}


    # =====================================================
    # BUILD TRANSFORMATION LOOKUP
    # =====================================================

    skipped_files = set(transform_log.get("skipped", []))


    # =====================================================
    # PROCESS VALIDATION ISSUES
    # =====================================================

    for _, r in df_health.iterrows():

        filename = r.get("original_filename") or r.get("filename")        
        issue = r.get("issue_type")
        severity = r.get("severity")
        autofixable = yes_no(r.get("autofixable"))

        action_taken = "none"
        vendor_action = "none"

        if pd.isna(issue):
            continue

        # autofix handled automatically
        elif autofixable in [True, 1, "TRUE", "true"]:
            action_taken = "fixed_automatically"
            vendor_action = "none"

        else:

            if severity == "blocking":
                vendor_action = "upload_correct_asset"

            elif severity == "warning":
                vendor_action = "optional_improvement"

        rows.append({

            "filename": filename,
            "issue": issue,
            "severity": severity,
            "autofixable": autofixable,
            "action_taken": action_taken,
            "vendor_action_required": vendor_action

        })

    df_out = pd.DataFrame(rows)


    # =====================================================
    # SAVE FINAL REPORT
    # =====================================================

    buf = BytesIO()

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:

        df_out = pd.DataFrame(rows)
        df_out = df_out.drop_duplicates()
        df_out = df_out.sort_values(["filename", "severity"])
        df_out.to_excel(writer, sheet_name="asset_summary", index=False)


    report_path = (
        f"in_review/assets_workflow/"
        f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/reports/asset_submission_summary.xlsx"
    )

    container.upload_blob(report_path, buf.getvalue(), overwrite=True)

    log("Vendor action report created", 2)


#Promote to approved and gold
def promote_assets(vendor, submission_type, submission_id):

    if submission_type not in ["asset_review", "delta_asset_review"]:
        log(f"Skipping promotion (pre-review): {submission_type}", 2)
        return

    log("Starting promotion (approved + gold)", 2)

    ready_prefix = (
        f"ready/assets_workflow/vendor={vendor}/"
        f"submission_type={submission_type}/submission={submission_id}/assets/"
    )

    approved_base = f"approved/assets_workflow/vendor={vendor}/"
    # gold_base = f"selected/asset_workflow/vendor={vendor}/"

    blobs = list(container.list_blobs(name_starts_with=ready_prefix))

    if not blobs:
        log("No assets found in ready layer", 2)
        return

    log(f"Found {len(blobs)} ready assets", 2)

    # =========================================================
    # Extract part_numbers
    # =========================================================
    parts_in_submission = set()

    for blob in blobs:
        path = blob.name
        if "part_number=" in path:
            part = path.split("part_number=")[1].split("/")[0]
            parts_in_submission.add(part)

    log(f"Parts in submission: {list(parts_in_submission)}", 2)

    # =========================================================
    # APPROVED LAYER LOGIC
    # =========================================================

    if submission_type == "asset_review":

        log("APPROVED → Full replace", 2)

        existing = list(container.list_blobs(name_starts_with=approved_base))

        for blob in existing:
            container.delete_blob(blob.name)

        log(f"Deleted {len(existing)} approved assets", 2)

    elif submission_type == "delta_asset_review":

        log("APPROVED → Delta replace", 2)

        for part in parts_in_submission:

            prefix = f"{approved_base}part_number={part}/"

            existing = list(container.list_blobs(name_starts_with=prefix))

            for blob in existing:
                container.delete_blob(blob.name)

            log(f"Cleared approved for part {part}", 4)

    # =========================================================
    # PROMOTE READY → APPROVED + GOLD
    # =========================================================

    promoted = 0

    for blob in blobs:

        src_path = blob.name

        if src_path.endswith("/"):
            continue

        relative = src_path.replace(ready_prefix, "")

        approved_path = f"{approved_base}{relative}"
        # gold_path = f"{gold_base}{relative}"

        data = container.get_blob_client(src_path).download_blob().readall()

        # APPROVED
        container.upload_blob(
            approved_path,
            data,
            overwrite=True
        )

        # # GOLD
        # gold_container.upload_blob(
        #     gold_path,
        #     data,
        #     overwrite=True
        # )

        promoted += 1

    log(f"Promoted {promoted} assets → APPROVED + GOLD", 2)


# =========================================================
# CACHE
# =========================================================

def get_vendor_cache_dir(vendor: str):

    if ASSET_CACHE_BASE:

        os.makedirs(ASSET_CACHE_BASE, exist_ok=True)

        base = os.path.join(ASSET_CACHE_BASE, f"asset_cache_{vendor}")

        os.makedirs(base, exist_ok=True)

        return base

    return tempfile.mkdtemp(prefix=f"asset_cache_{vendor}_")


def build_local_asset_cache(vendor: str, df: pd.DataFrame):

    cache_dir = get_vendor_cache_dir(vendor)

    file_map = {}
    miss_map = {}

    log("Building local asset cache", 2)

    unique = df[["original_filename", "source_blob_path"]].drop_duplicates()

    for _, row in unique.iterrows():

        original = str(row["original_filename"]).strip()
        src_path = row["source_blob_path"]

        if not src_path or pd.isna(src_path):
            miss_map[original] = "missing_source_blob_path"
            continue

        src_path = str(src_path).strip()

        local_path = os.path.join(cache_dir, original)

        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        if os.path.exists(local_path):

            file_map[original] = local_path
            continue

        try:

            data = download_blob(src_path)

        except ResourceNotFoundError:

            miss_map[original] = "missing"

            continue

        with open(local_path, "wb") as f:
            f.write(data)

        file_map[original] = local_path

    log(f"Cached assets: {len(file_map)}", 2)

    return cache_dir, file_map, miss_map


def cleanup_cache_dir(path):

    if ASSET_CACHE_BASE:
        return

    if os.path.exists(path):
        shutil.rmtree(path)


# =========================================================
# IMAGE NORMALIZATION
# =========================================================

def normalize_image_to_square(data: bytes):

    with Image.open(BytesIO(data)) as img:

        img = img.convert("RGB")

        orig_w, orig_h = img.size

        scale = FINAL_SIZE / max(orig_w, orig_h)

        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)

        img = img.resize((new_w, new_h), Image.LANCZOS)

        canvas = Image.new("RGB", (FINAL_SIZE, FINAL_SIZE), BACKGROUND_COLOR)

        offset_x = (FINAL_SIZE - new_w) // 2
        offset_y = (FINAL_SIZE - new_h) // 2

        canvas.paste(img, (offset_x, offset_y))

        buf = BytesIO()

        canvas.save(buf, format="JPEG", quality=JPEG_QUALITY)

        return buf.getvalue(), {
            "original_resolution": f"{orig_w}x{orig_h}",
            "final_resolution": "1000x1000",
        }


# =========================================================
# PROCESS SINGLE ASSET
# =========================================================

def process_asset(row, vendor, output_root, file_map, submission_id, submission_type):

    part = str(row["part_number"]).strip()
    media_category = row["media_category"]
    original_filename = row["original_filename"]
    canonical_filename = row["canonical_filename"]

    ext = os.path.splitext(original_filename)[1].lower()
    image_formats = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff"]
    converted_to_jpg = ext in image_formats and ext not in [".jpg", ".jpeg"]

    local_path = file_map.get(original_filename)

    if not local_path:
        return {"status": "missing", "file": original_filename}

    with open(local_path, "rb") as f:
        data = f.read()

    base_out = f"{output_root}/assets_workflow/vendor={vendor}/submission_type={submission_type}/submission={submission_id}/assets/part_number={part}"

    try:

        if media_category == "image":

            out_data, meta = normalize_image_to_square(data)

            content_hash = compute_sha256(out_data)

            out_path = f"{base_out}/images/{canonical_filename}"

            existing = get_existing_blob_hash(out_path)

            if existing == content_hash:
                return {
                    "status": "skipped",
                    "file": canonical_filename,
                    "original_filename": original_filename
                }


            write_output(out_path, out_data, {"content_hash": content_hash})

            return {
                "status": "image_written",
                "file": canonical_filename,
                "original_filename": original_filename,
                "data": out_data,
                "hash": content_hash,
                "size": len(out_data),
                "meta": meta,
                "part": part,
                "converted_to_jpg": converted_to_jpg
            }

        elif media_category == "document":

            content_hash = compute_sha256(data)

            out_path = f"{base_out}/documents/{canonical_filename}"

            existing = get_existing_blob_hash(out_path)

            if existing == content_hash:
                return {
                    "status": "skipped",
                    "file": canonical_filename,
                    "original_filename": original_filename,
                    "converted_to_jpg": converted_to_jpg
                }

            write_output(out_path, data, {"content_hash": content_hash})

            return {
                "status": "document_written",
                "file": canonical_filename,
                "data": data
            }

        else:

            return {"status": "unsupported", "file": canonical_filename}

    except Exception as e:

        return {"status": "error", "file": canonical_filename, "error": str(e)}


# =========================================================
# MAIN TRANSFORMATION
# =========================================================

def apply_asset_transformations(vendor: str, submission_type: str, submission_id: str):

    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")

    log(f"[STEP] Starting asset transformations")
    log(f"Vendor: {vendor}", 2)
    log(f"Submission: {submission_id}", 2)

    output_root = APPROVED_ROOT

    log_data = {
        "vendor": vendor,
        "submission_id": submission_id,
        "timestamp": timestamp,
        "images_written": 0,
        "documents_written": 0,
        "skipped": [],
        "errors": [],
        "missing_assets": [],
        "format_conversions": []
    }

    assets_for_zip = []
    cache_dir = None

    try:

        df = load_media_canonical(vendor, submission_type, submission_id)

        cache_dir, file_map, miss_map = build_local_asset_cache(vendor, df)

        for k in miss_map:
            log_data["missing_assets"].append(k)

        rows = list(df.iterrows())

        total_assets = len(rows)
        processed_assets = 0

        workers = min(16, (os.cpu_count() or 4) * 4)

        with ThreadPoolExecutor(max_workers=workers) as executor:

            futures = [
                executor.submit(
                    process_asset,
                    row,
                    vendor,
                    output_root,
                    file_map,
                    submission_id,
                    submission_type
                )
                for _, row in rows
            ]

            for f in as_completed(futures):

                result = f.result()

                status = result["status"]

                processed_assets += 1

                write_status(
                    vendor,
                    "ASSET TRANSFORMATION",
                    "PROCESSING",
                    f"{processed_assets}/{total_assets} assets processed",
                    submission_id
                )

                if status == "image_written":
                    log_data["images_written"] += 1
                    if result.get("converted_to_jpg"):
                        log_data["format_conversions"].append({
                            "filename": result["original_filename"],
                            "action": "converted_to_jpg"
                        })
                    assets_for_zip.append(
                        (result["file"], result["data"])
                    )

                elif status == "document_written":
                    log_data["documents_written"] += 1
                    assets_for_zip.append(
                        (result["file"], result["data"])
                    )

                elif status == "missing":
                    log_data["missing_assets"].append(result["file"])

                elif status == "skipped":

                    log_data["skipped"].append(result["file"])

                    original_file = result.get("original_filename")
                    local_path = file_map.get(original_file)

                    if local_path:
                        with open(local_path, "rb") as fh:
                            data = fh.read()

                        assets_for_zip.append((result["file"], data))


                elif status == "error":
                    log_data["errors"].append(result)

        log_path = (
            f"in_review/assets_workflow/"
            f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/reports/asset_transform_log.json"
        )
                
        log_data["finished_at"] = datetime.utcnow().isoformat()
        write_output(log_path, json.dumps(log_data, indent=2).encode())
        log(f"Assets added to ZIP: {len(assets_for_zip)}", 2)

        create_assets_zip(vendor, submission_type, submission_id, assets_for_zip)

        create_vendor_action_report(vendor,submission_type, submission_id)

        promote_assets(vendor, submission_type, submission_id)


        if log_data["errors"]:

            write_status(
                vendor,
                "ASSET TRANSFORMATION",
                "COMPLETED_WITH_ERRORS",
                f"{len(log_data['errors'])} failures",
                submission_id
            )

        else:

            write_status(
                vendor,
                "ASSET TRANSFORMATION",
                "COMPLETED",
                "",
                submission_id
            )

        log(
            f"Completed {vendor} | "
            f"images={log_data['images_written']} "
            f"docs={log_data['documents_written']}"
        )

    finally:

        if cache_dir:
            cleanup_cache_dir(cache_dir)


import zipfile
from io import BytesIO

def create_assets_zip(vendor, submission_type, submission_id, assets):

    prefix = f"ready/assets_workflow/vendor={vendor}/submission_type={submission_type}/submission={submission_id}/assets/"
    zip_buffer = BytesIO()

    asset_count = 0

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as z:

        for filename, data in assets:

            z.writestr(filename, data)

            asset_count += 1

        # add transform log into zip
        try:

            log_blob = (
                f"in_review/assets_workflow/"
                f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/reports/asset_transform_log.json"
            )

            log_data = container.get_blob_client(log_blob).download_blob().readall()

            z.writestr("asset_transform_log.json", log_data)

        except:
            pass

    zip_path = (
        f"in_review/assets_workflow/"
        f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/reports/transformed_assets.zip"
    )

    container.upload_blob(zip_path, zip_buffer.getvalue(), overwrite=True)

    log(f"Review ZIP created | assets={asset_count}", 2)

# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-type", required=True)
    parser.add_argument("--submission-id", required=True)

    args = parser.parse_args()

    write_status(args.vendor, "ASSET TRANSFORMATION", "PROCESSING", args.submission_id)

    apply_asset_transformations(args.vendor, args.submission_type, args.submission_id)
