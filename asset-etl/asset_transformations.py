import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# asset_transformations.py
import os
import json
import time
import shutil
import tempfile
from io import BytesIO
from datetime import datetime
from typing import Optional, Dict, List, Tuple

import pandas as pd
from PIL import Image
import pyarrow.parquet as pq
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError
from common.status_writer import write_status
import pyarrow as pa
import hashlib

####Purpose: “Make it real” (Execution)
"""
Purpose:
- Normalize assets according to FGI Akeneo policy
- Enforces final resolution, format, and naming
- Source of truth: media_canonical.parquet
"""


"""
asset_transformations.py

Purpose:
---------
This module performs the physical Asset ETL transformation and promotion step.
It converts validated vendor assets into standardized, optimized, and
canonically named Silver assets ready for downstream systems
(Akeneo, ERP, E-commerce).

Pipeline Position:
-------------------
Vendor Upload / Bronze
    → Silver / in_review / assets (staging)
    → asset_extraction_validation.py   (inspect & validate)
    → asset_canonicalize.py            (define canonical plan)
    → asset_transformations.py         (PHYSICAL TRANSFORMATION)
    → Silver / in_review / assets (final)

Core Responsibilities:
----------------------
1. Load the media canonical table (media_canonical.parquet) to determine:
   - which assets to process
   - media category (image vs document)
   - canonical filenames and output structure

2. Locate source assets in Azure Blob Storage using known candidate paths.
   Missing assets are gracefully skipped (common in sample or partial data).

3. Cache each unique source asset locally once per run to avoid repeated
   Azure downloads and significantly reduce network I/O.

4. Apply physical transformations:
   - Images:
     • Convert to RGB
     • Convert format to JPEG
     • Enforce minimum and maximum resolution
     • Resize oversized images while preserving aspect ratio
     • Apply standardized JPEG quality
   - Documents (PDF):
     • No content modification
     • Canonical rename and promotion only

5. Promote transformed assets to Silver under a canonical folder structure:
   silver/in_review/vendor=<vendor>/assets/part_number=<part>/{images|documents}/

6. Generate an audit log per vendor capturing:
   - assets written
   - skipped or missing assets
   - transformation errors
   - cache strategy used

Design Notes:
-------------
• This script is intentionally tolerant of missing assets to support
  sample data and iterative onboarding.
• Validation and naming decisions are delegated to upstream stages.
• Local caching is scoped to this step to maintain stateless, re-runnable
  pipeline stages.

"""

# =========================================================
# MODE
# =========================================================
LOCAL_TEST_MODE = False   # ← set to False when Azure enabled
LOCAL_OUTPUT_BASE = "./_local_asset_test_output"

# =========================================================
# OUTPUT ZONES
# =========================================================
REVIEW_ROOT = "in_review"
APPROVED_ROOT = "ready"

# =========================================================
# CONFIG
# =========================================================
AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
if not AZURE_CONN_STR:
    raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set")

SILVER_CONTAINER = "silver"

# Canonical table path (per vendor)
CANONICAL_TABLE = "canonical/media_canonical.parquet"

# Candidate source folders (we will try in this order)
# NOTE: Keep this short (probing costs requests). Add more only if needed.
ASSET_SOURCE_CANDIDATES = [
    "assets_staging",  # preferred
    "assets",          # common in sample data
]

# Output / logs
CANONICAL_ROOT = "in_review"
LOG_ROOT = "in_review"

# Image policies
MIN_WIDTH = 800
MIN_HEIGHT = 800
MAX_WIDTH = 1000
MAX_HEIGHT = 1000
JPEG_QUALITY = 90
MAX_IMAGE_BYTES = 25 * 1024 * 1024  # 25 MB safety cap
FINAL_SIZE = 1000
BACKGROUND_COLOR = (255, 255, 255)
JPEG_QUALITY = 90


# Download behavior
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_BACKOFF_SEC = 1.5  # exponential base

# Cache behavior
# If you want to keep caches across runs, set ASSET_CACHE_BASE=/some/path
# Otherwise, a temp dir is used and cleaned per vendor.
ASSET_CACHE_BASE = os.getenv("ASSET_CACHE_BASE", "").strip() or None


# =========================================================
# INIT
# =========================================================
blob_service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
container = blob_service.get_container_client(SILVER_CONTAINER)


# =========================================================
# LOGGING
# =========================================================
def log(msg: str, indent: int = 0):
    prefix = " " * indent
    print(f"{prefix}{msg}", flush=True)


# =========================================================
# AZURE HELPERS
# =========================================================
def download_blob(path: str) -> bytes:
    """
    Download blob bytes with basic retry on transient failures.
    Raises ResourceNotFoundError if the blob is not found.
    """
    last_err = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            return container.get_blob_client(path).download_blob().readall()
        except ResourceNotFoundError:
            # Not found is not transient — surface immediately
            raise
        except (ServiceRequestError,) as e:
            last_err = e
            wait = (DOWNLOAD_RETRY_BACKOFF_SEC ** attempt)
            log(
                f"⚠️ Transient download error (attempt {attempt}/{DOWNLOAD_RETRIES}). "
                f"Retrying in {wait:.1f}s",
                6
            )
            time.sleep(wait)
        except Exception:
            # unknown — fail fast
            raise
    raise last_err if last_err else RuntimeError("download_failed_unknown")


def upload_blob(path: str, data: bytes):
    container.upload_blob(path, data, overwrite=True)

def write_output(path: str, data: bytes):
    """
    Write output either to Azure Blob or local filesystem.
    Mirrors Azure folder structure when local.
    """
    if LOCAL_TEST_MODE:
        local_path = os.path.join(LOCAL_OUTPUT_BASE, path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
    else:
        upload_blob(path, data)

def blob_exists(path: str) -> bool:
    try:
        container.get_blob_client(path).get_blob_properties()
        return True
    except ResourceNotFoundError:
        return False

# =========================================================
# CANONICAL TABLE
# =========================================================
def load_media_canonical(vendor: str) -> pd.DataFrame:

    path = f"in_review/vendor={vendor}/{CANONICAL_TABLE}"

    raw = download_blob(path)
    table = pq.read_table(BytesIO(raw))
    df = table.to_pandas()

    print("MEDIA CANONICAL PARTS:")
    print(df["part_number"].astype(str).unique())

    required = {"part_number", "media_category", "original_filename", "canonical_filename"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"canonical_table_missing_columns: {sorted(missing)}")

    log(f"📊 Canonical rows: {len(df)}", 2)
    return df

# =========================================================
# LOCAL CACHE HELPERS
# =========================================================
def _safe_relpath(name: str) -> str:
    """
    Make a safe relative path for cache, preserving folder structure if present.
    Prevents absolute paths and path traversal.
    """
    name = str(name).replace("\\", "/").lstrip("/")
    while name.startswith("../"):
        name = name[3:]
    name = name.replace("/../", "/")
    return name


def get_vendor_cache_dir(vendor: str) -> str:
    if ASSET_CACHE_BASE:
        os.makedirs(ASSET_CACHE_BASE, exist_ok=True)
        base = os.path.join(ASSET_CACHE_BASE, f"asset_cache_{vendor}")
        os.makedirs(base, exist_ok=True)
        return base
    return tempfile.mkdtemp(prefix=f"asset_cache_{vendor}_")


def resolve_blob_path(vendor: str, original_filename: str):
    """
    Try to find the blob in known candidate folders.
    Returns the first matching blob path, otherwise None.

    We do NOT list blobs (too expensive); we probe using get_blob_properties().
    """
    safe_rel = _safe_relpath(original_filename)
    base = os.path.basename(safe_rel)

    # Try these filename variants (preserve path then basename)
    filename_variants = [safe_rel]
    if base != safe_rel:
        filename_variants.append(base)

    # Try in each candidate folder
    for folder in ASSET_SOURCE_CANDIDATES:
        for fn in filename_variants:
            candidate = (
                f"in_review/vendor={vendor}/"
                f"{folder}/{fn}"
            )
            try:
                # lightweight probe
                container.get_blob_client(candidate).get_blob_properties()
                return candidate
            except ResourceNotFoundError:
                continue

    return None


def build_local_asset_cache(vendor: str, df: pd.DataFrame) -> Tuple[str, Dict[str, str], Dict[str, str]]:
    """
    Downloads each unique original asset ONCE into a local directory.
    Returns:
      - cache_dir
      - file_map: original_filename -> local_path
      - miss_map: original_filename -> reason (e.g., "blob_not_found")
    """
    cache_dir = get_vendor_cache_dir(vendor)
    log("📦 Building local asset cache", 2)

    unique_files = (
        df["original_filename"]
        .dropna()
        .astype(str)
        .map(lambda s: s.strip())
        .unique()
        .tolist()
    )

    file_map: Dict[str, str] = {}
    miss_map: Dict[str, str] = {}

    total = len(unique_files)

    for i, original in enumerate(unique_files, start=1):
        safe_rel = _safe_relpath(original)
        local_path = os.path.join(cache_dir, safe_rel)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        # Allow persistent reuse
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            file_map[original] = local_path
            continue

        log(f"Locating source asset ...  ({i}/{total}) {original}", 3)

        src_path = resolve_blob_path(vendor, original)
        if not src_path:
            miss_map[original] = "blob_not_found"
            continue

        try:
            data = download_blob(src_path)
        except ResourceNotFoundError:
            miss_map[original] = "blob_not_found"
            continue
        except Exception as e:
            miss_map[original] = f"download_error:{e}"
            continue

        with open(local_path, "wb") as f:
            f.write(data)

        file_map[original] = local_path

    log(f"✅ Cached {len(file_map)} / {len(unique_files)} assets locally", 2)
    if miss_map:
        log(f"⚠️ Missing in Azure: {len(miss_map)}", 2)

    return cache_dir, file_map, miss_map


def cleanup_cache_dir(cache_dir: str):
    if not cache_dir:
        return
    if ASSET_CACHE_BASE:
        return
    if os.path.exists(cache_dir):
        shutil.rmtree(cache_dir, ignore_errors=True)

#Helpers
def guard_block_if_rejected_logs_exist(vendor: str):
    prefix = (
        f"rejected/logs/vendor={vendor}/"
    )
    blobs = list(container.list_blobs(name_starts_with=prefix))
    if blobs:
        raise RuntimeError(
            f"🚫 BLOCKED: rejected logs exist for {vendor}/"
        )
    
def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

# =========================================================
# IMAGE NORMALIZATION
# =========================================================
def normalize_image(data: bytes) -> bytes:
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image_file_too_large")

    with Image.open(BytesIO(data)) as img:
        img = img.convert("RGB")
        w, h = img.size

        if w < MIN_WIDTH or h < MIN_HEIGHT:
            raise ValueError("resolution_too_low")

        if w > MAX_WIDTH or h > MAX_HEIGHT:
            img.thumbnail((MAX_WIDTH, MAX_HEIGHT), Image.LANCZOS)

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()


def normalize_image_to_square(data: bytes) -> tuple[bytes, dict]:
    """
    Normalize image to 1000x1000:
    - preserve aspect ratio
    - upscale or downscale
    - pad with white background
    """
    with Image.open(BytesIO(data)) as img:
        img = img.convert("RGB")
        orig_w, orig_h = img.size

        # Scale so longest side = FINAL_SIZE
        scale = FINAL_SIZE / max(orig_w, orig_h)
        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)

        img = img.resize((new_w, new_h), Image.LANCZOS)

        # Create white canvas
        canvas = Image.new(
            "RGB",
            (FINAL_SIZE, FINAL_SIZE),
            BACKGROUND_COLOR
        )

        offset_x = (FINAL_SIZE - new_w) // 2
        offset_y = (FINAL_SIZE - new_h) // 2
        canvas.paste(img, (offset_x, offset_y))

        buf = BytesIO()
        canvas.save(buf, format="JPEG", quality=JPEG_QUALITY)

        meta = {
            "original_resolution": f"{orig_w}x{orig_h}",
            "final_resolution": "1000x1000",
            "scaled_to": f"{new_w}x{new_h}",
            "padding": "white"
        }

        return buf.getvalue(), meta


# =========================================================
# CORE
# =========================================================
def apply_asset_transformations(vendor: str, submission_type: str):
    guard_block_if_rejected_logs_exist(vendor)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    log(f"▶ Starting asset transformations for vendor: {vendor}")

    log(f"Submission type: {submission_type}")

    # Review modes should NOT transform assets
    if submission_type in ["asset_review", "delta_asset_review"]:
        log("⚠ Review mode detected — skipping physical asset transformations")

        write_status(
            vendor,
            "ASSET TRANSFORMATION",
            "SKIPPED_REVIEW_MODE"
        )

        return
    
    if submission_type == "asset_submission":
        output_root = APPROVED_ROOT

    elif submission_type == "delta_asset_submission":
        output_root = APPROVED_ROOT

    else:
        output_root = None

    log_data = {
        "vendor": vendor,
        "timestamp": timestamp,
        "images_written": 0,
        "documents_written": 0,
        "skipped": [],
        "errors": [],
        "missing_assets": [],
        "cache": {
            "strategy": "persistent" if ASSET_CACHE_BASE else "temp",
            "base": ASSET_CACHE_BASE,
        },
        "source_candidates": ASSET_SOURCE_CANDIDATES,
    }

    # =====================================================
    # ASSET METADATA COLLECTOR (PHYSICAL QUALITY TABLE)
    # =====================================================
    asset_meta_rows = []


    cache_dir: Optional[str] = None

    try:
        df = load_media_canonical(vendor)
        total_rows = len(df)

        # Build cache once
        cache_dir, file_map, miss_map = build_local_asset_cache(vendor, df)
        log_data["cache"]["dir"] = cache_dir

        # Record missing upfront (so you can see it quickly in logs)
        for k, reason in miss_map.items():
            log_data["missing_assets"].append({"file": k, "reason": reason})

        # Transform per row (skip if missing)
        for idx, row in df.iterrows():
            part = str(row["part_number"]).strip()
            media_category = str(row["media_category"]).strip().lower()
            original_filename = str(row["original_filename"]).strip()
            canonical_filename = str(row["canonical_filename"]).strip()

            log(f"[{idx + 1}/{total_rows}] {media_category.upper()} | {original_filename}", 2)

            local_path = file_map.get(original_filename)
            if not local_path or not os.path.exists(local_path):
                # common for sample data — just continue
                log_data["skipped"].append({
                    "file": original_filename,
                    "reason": "missing_source_asset"
                })
                continue

            try:
                with open(local_path, "rb") as f:
                    data = f.read()

                base_out = (
                    f"{output_root}/"
                    f"vendor={vendor}/"
                    f"assets/"
                    f"part_number={part}"
                )

                if media_category == "image":
                    out_data,transform_meta = normalize_image_to_square(data)
                    
                    content_hash = compute_sha256(out_data)

                    out_path = f"{base_out}/images/{canonical_filename}"

                    # Skip existing assets during delta submission
                    if submission_type == "delta_asset_submission" and blob_exists(out_path):

                        log_data["skipped"].append({
                            "file": canonical_filename,
                            "reason": "already_exists"
                        })

                        continue

                    write_output(out_path, out_data)
                    log_data["images_written"] += 1

                    
                    log_data.setdefault("image_transforms", []).append({
                    "file": original_filename,
                    **transform_meta})

                    asset_meta_rows.append({
                    "vendor": vendor,
                    "part_number": part,
                    "original_filename": original_filename,
                    "canonical_filename": canonical_filename,
                    "media_category": "image",
                    "original_resolution": transform_meta.get("original_resolution"),
                    "final_resolution": transform_meta.get("final_resolution"),
                    "scaled_to": transform_meta.get("scaled_to"),
                    "padding": transform_meta.get("padding"),
                    "file_size_bytes": len(out_data),
                    "content_hash": content_hash,
                })



                elif media_category == "document":
                    out_path = f"{base_out}/documents/{canonical_filename}"
                    
                    if submission_type == "delta_asset_submission" and blob_exists(out_path):

                        log_data["skipped"].append({
                            "file": canonical_filename,
                            "reason": "already_exists"
                        })

                        continue

                    write_output(out_path, data)
                    log_data["documents_written"] += 1


                else:
                    log_data["skipped"].append({
                        "file": original_filename,
                        "reason": "unsupported_media_category",
                        "media_category": media_category
                    })

            except Exception as e:
                log_data["errors"].append({
                    "file": original_filename,
                    "error": str(e)
                })

        # =====================================================
        # WRITE ASSET QUALITY PARQUET (PHYSICAL METADATA)
        # =====================================================
        if asset_meta_rows:
            meta_df = pd.DataFrame(asset_meta_rows)
            meta_buf = BytesIO()
            pq.write_table(pa.Table.from_pandas(meta_df), meta_buf)

            meta_path = (
                f"{output_root}/"
                f"vendor={vendor}/"
                f"assets/_metadata/asset_quality.parquet"
            )

            write_output(meta_path, meta_buf.getvalue())


        # Write transformation log
        log_path = (
            f"{LOG_ROOT}/"
            f"vendor={vendor}/"
            f"logs/asset-transform-{timestamp}.json"
        )


        write_output(log_path, json.dumps(log_data, indent=2).encode("utf-8"))

        log(
            f"✔ Vendor completed: {vendor} | "
            f"images={log_data['images_written']} | "
            f"documents={log_data['documents_written']} | "
            f"missing={len(log_data['missing_assets'])} | "
            f"errors={len(log_data['errors'])}",
            1
        )

        if log_data["errors"]:
            write_status(
                vendor,
                "ASSET TRANSFORMATION",
                "COMPLETED_WITH_ERRORS",
                f"{len(log_data['errors'])} asset(s) failed"
            )
        else:
            write_status(
                vendor,
                "ASSET TRANSFORMATION",
                "COMPLETED"
            )


    finally:
        if cache_dir and not ASSET_CACHE_BASE:
            cleanup_cache_dir(cache_dir)


# =========================================================
# ORCHESTRATOR
# =========================================================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-type", required=True)

    args = parser.parse_args()
    write_status(args.vendor, "ASSET TRANSFORMATION", "RUNNING")
    apply_asset_transformations(args.vendor, args.submission_type)