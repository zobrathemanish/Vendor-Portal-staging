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
container = blob_service.get_container_client(SILVER_CONTAINER)


# =========================================================
# HELPERS
# =========================================================

def log(msg: str, indent: int = 0):
    print(" " * indent + msg, flush=True)


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# =========================================================
# AZURE IO
# =========================================================

def download_blob(path: str) -> bytes:

    last_err = None

    for attempt in range(1, DOWNLOAD_RETRIES + 1):

        try:
            return container.get_blob_client(path).download_blob().readall()

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
        metadata=metadata
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


# =========================================================
# LOAD CANONICAL
# =========================================================

def load_media_canonical(vendor: str, submission_id: str) -> pd.DataFrame:

    path = f"in_review/vendor={vendor}/canonical/submission={submission_id}/media_canonical.parquet"

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

def process_asset(row, vendor, output_root, file_map):

    part = str(row["part_number"]).strip()
    media_category = row["media_category"]
    original_filename = row["original_filename"]
    canonical_filename = row["canonical_filename"]

    local_path = file_map.get(original_filename)

    if not local_path:
        return {"status": "missing", "file": original_filename}

    with open(local_path, "rb") as f:
        data = f.read()

    base_out = f"{output_root}/vendor={vendor}/assets/part_number={part}"

    try:

        if media_category == "image":

            out_data, meta = normalize_image_to_square(data)

            content_hash = compute_sha256(out_data)

            out_path = f"{base_out}/images/{canonical_filename}"

            existing = get_existing_blob_hash(out_path)

            if existing == content_hash:
                return {"status": "skipped", "file": canonical_filename}

            write_output(out_path, out_data, {"content_hash": content_hash})

            return {
                "status": "image_written",
                "file": canonical_filename,
                "data": out_data,
                "hash": content_hash,
                "size": len(out_data),
                "meta": meta,
                "part": part
            }

        elif media_category == "document":

            content_hash = compute_sha256(data)

            out_path = f"{base_out}/documents/{canonical_filename}"

            existing = get_existing_blob_hash(out_path)

            if existing == content_hash:
                return {"status": "skipped", "file": canonical_filename}

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

    log(f"▶ Starting asset transformations")
    log(f"Vendor: {vendor}", 2)
    log(f"Submission: {submission_id}", 2)

    if submission_type in ["asset_review", "delta_asset_review"]:

        write_status(vendor, "ASSET TRANSFORMATION", "SKIPPED_REVIEW_MODE", submission_id)

        return

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
    }

    assets_for_zip = []
    cache_dir = None

    try:

        df = load_media_canonical(vendor, submission_id)

        cache_dir, file_map, miss_map = build_local_asset_cache(vendor, df)

        for k in miss_map:
            log_data["missing_assets"].append(k)

        rows = list(df.iterrows())

        total_assets = len(rows)
        processed_assets = 0

        workers = min(8, os.cpu_count() * 2)

        with ThreadPoolExecutor(max_workers=workers) as executor:

            futures = [
                executor.submit(
                    process_asset,
                    row,
                    vendor,
                    output_root,
                    file_map
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

                elif status == "error":
                    log_data["errors"].append(result)

        log_path = (
            f"logs/vendor={vendor}/assets/submission={submission_id}/"
            f"asset_transform_log.json"
        )
                
        log_data["finished_at"] = datetime.utcnow().isoformat()
        write_output(log_path, json.dumps(log_data, indent=2).encode())
        create_assets_zip(vendor, submission_id, assets_for_zip)

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

def create_assets_zip(vendor, submission_id, asset_paths):

    zip_buffer = BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as z:

        for path, data in asset_paths:
            z.writestr(path, data)

    zip_path = (
        f"logs/vendor={vendor}/assets/"
        f"submission={submission_id}/"
        f"transformed_assets.zip"
    )

    container.upload_blob(zip_path, zip_buffer.getvalue(), overwrite=True)
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
