# asset-etl/asset_extraction_validation.py

import os
import json
import zipfile
import hashlib
from io import BytesIO
from datetime import datetime
from typing import List, Dict, Any

from PIL import Image
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError
import pandas as pd
from concurrent.futures import ThreadPoolExecutor

"""
Purpose:
Validate vendor assets before processing.
No transformations occur here.
Low resolution images are flagged but not rejected.

This version adds:
- deterministic staging manifest (_asset_manifest.json)
- source_blob_path tracking for downstream canonicalization / transformation
- ZIP deduplication
- staged asset indexing to avoid repeated list_blobs scans
- safer handling of duplicate filenames inside ZIPs
"""

# =========================================================
# CONFIG
# =========================================================

load_dotenv()

AZURE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

if not AZURE_CONNECTION_STRING:
    raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set")

BRONZE_CONTAINER = "bronze"
SILVER_CONTAINER = "silver"

SUPPORTED_IMAGE_FORMATS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff"}
SUPPORTED_DOCUMENT_FORMATS = {".pdf"}
SUPPORTED_FORMATS = SUPPORTED_IMAGE_FORMATS | SUPPORTED_DOCUMENT_FORMATS

MIN_WIDTH = 850
MIN_HEIGHT = 850
MAX_MB = 20

RUN_TS = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")

blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
bronze_container = blob_service.get_container_client(BRONZE_CONTAINER)
silver_container = blob_service.get_container_client(SILVER_CONTAINER)

FULL_SUBMISSIONS = {"asset_submission", "asset_review"}
DELTA_SUBMISSIONS = {"delta_asset_submission", "delta_asset_review"}

# =========================================================
# HELPERS
# =========================================================

def compute_file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def log(msg, indent=0):
    try:
        print(" " * indent + msg, flush=True)
    except UnicodeEncodeError:
        safe = msg.encode("ascii", "replace").decode()
        print(" " * indent + safe, flush=True)


def blob_exists(container, path: str) -> bool:
    try:
        container.get_blob_client(path).get_blob_properties()
        return True
    except ResourceNotFoundError:
        return False


def download_blob(container, blob_path):

    import tempfile
    import time

    blob = container.get_blob_client(blob_path)

    props = blob.get_blob_properties()
    total_size = props.size

    log(f"    Blob size: {total_size/1024/1024:.2f} MB", 4)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")

    stream = blob.download_blob(max_concurrency=8)

    downloaded = 0
    start = time.time()

    with open(tmp.name, "wb") as f:
        for chunk in stream.chunks():

            f.write(chunk)

            downloaded += len(chunk)

            if downloaded % (50 * 1024 * 1024) < len(chunk):
                log(
                    f"    Downloaded {downloaded/1024/1024:.1f} / {total_size/1024/1024:.1f} MB",
                    4
                )

    elapsed = time.time() - start

    log(f"    Download finished in {elapsed:.2f}s", 4)

    return tmp.name


def upload_json(payload: dict, blob_path: str):
    silver_container.upload_blob(
        name=blob_path,
        data=json.dumps(payload, indent=2),
        overwrite=True
    )


def vendor_paths(vendor: str, submission_type: str, submission_id: str) -> Dict[str, str]:

    base = f"in_review/assets_workflow/{vendor}/{submission_type}/{submission_id}"

    staging_prefix = f"{base}/assets_staging/"
    log_prefix = f"{base}/logs/"

    return {
        "asset_prefix": f"raw/vendor={vendor}/assets/submission={submission_id}/original_zip/",
        "staging_prefix": staging_prefix,
        "log_prefix": log_prefix,
        "zip_hash_log": f"{log_prefix}zip_hashes.json",
        "staging_manifest": f"{staging_prefix}_asset_manifest.json",
    }


def normalize_filename(filename: str) -> str:
    """
    Keep only the basename and normalize whitespace.
    """
    filename = os.path.basename(str(filename).strip())
    return filename


def build_staging_path(vendor: str, submission_type: str, submission_id: str, filename: str) -> str:

    return (
        f"in_review/assets_workflow/"
        f"{vendor}/{submission_type}/{submission_id}/"
        f"assets_staging/{filename}"
    )

# =========================================================
# ZIP HASH LOGS
# =========================================================

def load_zip_hashes(blob_path: str) -> Dict[str, Any]:
    try:
        data = silver_container.get_blob_client(blob_path).download_blob().readall()
        obj = json.loads(data)
        if not isinstance(obj, dict):
            return {"hashes": []}
        obj.setdefault("hashes", [])
        return obj
    except Exception:
        return {"hashes": []}


def save_zip_hashes(blob_path: str, payload: Dict[str, Any]):
    payload = {"hashes": list(dict.fromkeys(payload.get("hashes", [])))}
    upload_json(payload, blob_path)


# =========================================================
# STAGING MANIFEST
# =========================================================

def load_asset_manifest(vendor: str, submission_type:str, submission_id: str) -> Dict[str, Any]:
    paths = vendor_paths(vendor, submission_type, submission_id)
    try:
        data = silver_container.get_blob_client(paths["staging_manifest"]).download_blob().readall()
        manifest = json.loads(data)

        if not isinstance(manifest, dict):
            raise ValueError("manifest_not_dict")

        manifest.setdefault("vendor", vendor)
        manifest.setdefault("run_timestamp", RUN_TS)
        manifest.setdefault("assets", [])

        # normalize minimal schema
        normalized_assets = []
        for item in manifest["assets"]:
            if isinstance(item, dict) and item.get("filename") and item.get("source_blob_path"):
                normalized_assets.append(item)

        manifest["assets"] = normalized_assets
        return manifest

    except Exception:
        return {
            "vendor": vendor,
            "run_timestamp": RUN_TS,
            "assets": []
        }


def save_asset_manifest(vendor: str, submission_type: str, submission_id: str, manifest: Dict[str, Any]):
    paths = vendor_paths(vendor, submission_type, submission_id)

    dedup: Dict[str, Dict[str, Any]] = {}
    for asset in manifest.get("assets", []):
        key = asset.get("filename")
        if key:
            dedup[key] = asset

    payload = {
        "vendor": vendor,
        "run_timestamp": manifest.get("run_timestamp", RUN_TS),
        "asset_count": len(dedup),
        "assets": list(dedup.values())
    }

    upload_json(payload, paths["staging_manifest"])


def clear_staging(vendor: str, submission_type:str, submission_id: str):
    paths = vendor_paths(vendor, submission_type, submission_id)
    prefix = paths["staging_prefix"]

    blobs = silver_container.list_blobs(name_starts_with=prefix)
    for blob in blobs:
        silver_container.delete_blob(blob.name)


def list_staged_assets(vendor: str, submission_type:str, submission_id: str) -> List[str]:
    """
    Prefer manifest over Azure list_blobs for scale.
    Falls back to listing only if manifest is missing or empty.
    """
    manifest = load_asset_manifest(vendor, submission_type, submission_id)
    assets = manifest.get("assets", [])

    if assets:
        return [
            item["source_blob_path"]
            for item in assets
            if item.get("source_blob_path")
        ]

    paths = vendor_paths(vendor, submission_type, submission_id)
    blobs = silver_container.list_blobs(name_starts_with=paths["staging_prefix"])
    return [
        b.name
        for b in blobs
        if not b.name.endswith("/") and not b.name.endswith("_asset_manifest.json")
    ]


# =========================================================
# STEP 1 — ENUMERATE ASSETS FROM BRONZE
# =========================================================

def list_asset_blobs(asset_prefix: str) -> List[str]:
    blobs = bronze_container.list_blobs(name_starts_with=asset_prefix)
    return [
        b.name
        for b in blobs
        if not b.name.endswith("/")
    ]


def _build_unique_filename(filename: str, existing_names: set) -> str:
    """
    Avoid collisions within staging when ZIP contains duplicate basenames.
    Example:
      image.jpg
      image.jpg -> image__2.jpg
    """
    if filename not in existing_names:
        return filename

    base, ext = os.path.splitext(filename)
    counter = 2
    while True:
        candidate = f"{base}__{counter}{ext}"
        if candidate not in existing_names:
            return candidate
        counter += 1


def extract_zip_assets(vendor: str, submission_type, submission_id: str, zip_blob_path: str):

    log(f"  Extracting ZIP: {zip_blob_path}")

    # -------------------------------------------------
    # STEP 1 — PATH SETUP
    # -------------------------------------------------

    log("  Step 1: preparing vendor paths", 2)
    paths = vendor_paths(vendor, submission_type, submission_id)

    # -------------------------------------------------
    # STEP 2 — LOAD HASH LOG
    # -------------------------------------------------

    log("  Step 2: loading ZIP hash log", 2)
    hash_log = load_zip_hashes(paths["zip_hash_log"])

    # -------------------------------------------------
    # STEP 3 — LOAD EXISTING MANIFEST
    # -------------------------------------------------

    log("  Step 3: loading asset manifest", 2)
    manifest = load_asset_manifest(vendor, submission_type, submission_id)

    # -------------------------------------------------
    # STEP 4 — DOWNLOAD ZIP
    # -------------------------------------------------

    log("  Step 4: downloading ZIP from Azure", 2)
    zip_path = download_blob(bronze_container, zip_blob_path)

    log(f"  ZIP downloaded to {zip_path}", 2)

    # -------------------------------------------------
    # STEP 5 — DUPLICATE ZIP DETECTION
    # -------------------------------------------------

    with open(zip_path, "rb") as f:
        zip_hash = compute_file_hash(open(zip_path, "rb").read())

    # if zip_hash in hash_log["hashes"]:
    #     log(f"  Skipping duplicate ZIP: {zip_blob_path}")
    #     return

    # -------------------------------------------------
    # STEP 6 — EXISTING ASSET INDEX
    # -------------------------------------------------

    existing_assets = {
        asset["filename"]: asset
        for asset in manifest.get("assets", [])
        if asset.get("filename")
    }

    existing_names = set(existing_assets.keys())

    extracted_count = 0
    skipped_existing_count = 0
    unsupported_count = 0

    # -------------------------------------------------
    # STEP 7 — OPEN ZIP
    # -------------------------------------------------

    log("  Step 5: opening ZIP archive", 2)

    with zipfile.ZipFile(zip_path) as z:

        files = [f for f in z.namelist() if not f.endswith("/")]
        total = len(files)

        log(f"  ZIP contains {total} files")

        # -------------------------------------------------
        # STEP 8 — PROCESS FILES
        # -------------------------------------------------

    def process_file(file_in_zip, zip_path):

        nonlocal extracted_count
        nonlocal skipped_existing_count
        nonlocal unsupported_count

        raw_filename = os.path.basename(file_in_zip)

        if not raw_filename:
            return

        filename = normalize_filename(raw_filename)
        ext = os.path.splitext(filename)[1].lower()

        if ext not in SUPPORTED_FORMATS:
            unsupported_count += 1
            return

        with zipfile.ZipFile(zip_path) as z:
            data = z.read(file_in_zip)

        size_mb = round(len(data) / (1024 * 1024), 2)
        content_hash = compute_file_hash(data)

        validation_status = "pass"
        issue_type = None
        severity = None
        autofixable = None
        width = None
        height = None

        if size_mb > MAX_MB:
            validation_status = "fail"
            issue_type = "file_too_large"
            severity = "blocking"
            autofixable = False

        elif ext in SUPPORTED_IMAGE_FORMATS:

            try:
                with Image.open(BytesIO(data)) as img:

                    width, height = img.size

                    if width < MIN_WIDTH or height < MIN_HEIGHT:

                        issue_type = "low_resolution"
                        severity = "warning"
                        autofixable = False

            except Exception:

                validation_status = "fail"
                issue_type = "corrupt_image"
                severity = "blocking"
                autofixable = False

        # duplicate filename handling
        if filename in existing_assets:

            existing_asset = existing_assets[filename]

            if existing_asset.get("content_hash") == content_hash:
                skipped_existing_count += 1
                return

            filename = _build_unique_filename(filename, existing_names)

        staging_path = build_staging_path(
            vendor,
            submission_type,
            submission_id,
            filename
        )

        silver_container.upload_blob(
            name=staging_path,
            data=data,
            overwrite=True,
            max_concurrency=8
        )

        if extracted_count % 50 == 0:
            log(f"    Extracted {extracted_count} assets...", 4)

        manifest["assets"].append({
            "filename": filename,
            "source_blob_path": staging_path,
            "content_hash": content_hash,
            "size_bytes": len(data),
            "size_mb": size_mb,
            "width": width,
            "height": height,
            "validation_status": validation_status,
            "issue_type": issue_type,
            "severity": severity,
            "autofixable": autofixable,
            "source_zip_blob_path": zip_blob_path,
            "source_zip_hash": zip_hash,
            "extracted_at": datetime.utcnow().isoformat()
        })

        existing_names.add(filename)

        extracted_count += 1


    with ThreadPoolExecutor(max_workers=min(16, os.cpu_count() * 4)) as executor:
        list(executor.map(lambda f: process_file(f, zip_path), files))

    # -------------------------------------------------
    # STEP 9 — SAVE MANIFEST
    # -------------------------------------------------

    log("  Step 6: saving asset manifest", 2)

    save_asset_manifest(
        vendor,
        submission_type,
        submission_id,
        manifest
    )

    # -------------------------------------------------
    # STEP 10 — SAVE ZIP HASH
    # -------------------------------------------------

    hash_log["hashes"].append(zip_hash)

    save_zip_hashes(
        paths["zip_hash_log"],
        hash_log
    )

    # -------------------------------------------------
    # FINAL SUMMARY
    # -------------------------------------------------

    log(
        f"  ZIP done: extracted={extracted_count}, "
        f"skipped_identical={skipped_existing_count}, "
        f"unsupported={unsupported_count}"
    )

# =========================================================
# STEP 2 — VALIDATE ASSETS
# =========================================================

def validate_assets(vendor, submission_type, submission_id):

    manifest = load_asset_manifest(vendor, submission_type, submission_id)

    passed = []
    failed = []

    assets = manifest.get("assets", [])

    total = len(assets)

    for idx, asset in enumerate(assets, start=1):

        if idx == 1 or idx % 50 == 0 or idx == total:
            log(f"  Validating assets... ({idx}/{total})")

        record = {
            "filename": asset.get("filename"),
            "blob_path": asset.get("source_blob_path"),
            "source_blob_path": asset.get("source_blob_path"),
            "status": asset.get("validation_status", "pass"),
            "issue_type": asset.get("issue_type"),
            "severity": asset.get("severity"),
            "autofixable": asset.get("autofixable"),
            "details": None,
            "size_mb": asset.get("size_mb"),
            "width": asset.get("width"),
            "height": asset.get("height"),
            "content_hash": asset.get("content_hash")
        }

        if record["status"] == "pass":
            passed.append(record)
        else:
            failed.append(record)

    return {
        "passed": passed,
        "failed": failed
    }

# =========================================================
# STEP 2B — DECLARED VS ACTUAL RECONCILIATION
# =========================================================

def reconcile_declared_vs_actual(vendor: str, submission_id: str, validation: Dict[str, List[Dict[str, Any]]]):

    parquet_path = f"in_review/vendor={vendor}/mapped/mapped.parquet"
    excel_path = f"in_review/vendor={vendor}/mapped/mapped.xlsx"

    try:
        blob = silver_container.get_blob_client(parquet_path)
        data = blob.download_blob().readall()
        df = pd.read_parquet(BytesIO(data))
        log("  Declared assets loaded from mapped.parquet")

    except Exception:

        log("  mapped.parquet not found — falling back to mapped.xlsx")

        try:
            blob = silver_container.get_blob_client(excel_path)
            data = blob.download_blob().readall()

            df = pd.read_excel(BytesIO(data), sheet_name="Digital_Assets")

            log("  Declared assets loaded from mapped.xlsx")

        except Exception:

            log("  ⚠ Unable to load mapped.parquet or mapped.xlsx")

            return {
                "missing_assets": [],
                "extra_assets": [],
                "present_but_failed": []
            }

    # ------------------------------------------
    # Normalize declared filenames
    # ------------------------------------------

    declared = set(
        df["FileName"]
        .astype(str)
        .str.lower()
        .str.strip()
        .str.replace(" ", "", regex=False)
    )

    # ------------------------------------------
    # Normalize actual filenames
    # ------------------------------------------

    passed = validation["passed"]
    failed = validation["failed"]

    actual_pass = set(
        os.path.basename(r["filename"]).lower().replace(" ", "")
        for r in passed
    )

    actual_fail = set(
        os.path.basename(r["filename"]).lower().replace(" ", "")
        for r in failed
    )

    actual_all = actual_pass | actual_fail

    # ------------------------------------------
    # Compute reconciliation
    # ------------------------------------------

    missing_assets = declared - actual_all
    extra_assets = actual_all - declared
    present_but_failed = declared & actual_fail

    return {
        "missing_assets": list(missing_assets),
        "extra_assets": list(extra_assets),
        "present_but_failed": list(present_but_failed)
    }

# =========================================================
# STEP 3 — MAIN ORCHESTRATION
# =========================================================

def run_asset_etl_for_vendor(vendor: str, submission_type: str, submission_id: str):
    print("Starting asset extraction step")
    log("Starting asset extraction step")

    if submission_type in ["asset_submission", "delta_asset_submission"]:
        clear_staging(vendor, submission_type, submission_id)

    log(f"▶ Running Asset Validation for vendor: {vendor}")
    log(f"  Submission type: {submission_type}")

    paths = vendor_paths(vendor, submission_type, submission_id)

    log("  Discovering assets in Azure...")
    asset_blobs = list_asset_blobs(paths["asset_prefix"])

    zip_blobs = [blob for blob in asset_blobs if blob.lower().endswith(".zip")]
    direct_supported_files = [
        blob for blob in asset_blobs
        if os.path.splitext(blob)[1].lower() in SUPPORTED_FORMATS
    ]

    log(f"  Found {len(asset_blobs)} raw asset blob(s)")
    log(f"  ZIP files to inspect: {len(zip_blobs)}")
    if direct_supported_files:
        log(f"  Direct non-ZIP asset files detected: {len(direct_supported_files)}", 2)

    log("  Extracting ZIP assets...")
    for blob in zip_blobs:
        extract_zip_assets(vendor, submission_type, submission_id, blob)

    manifest = load_asset_manifest(vendor, submission_type, submission_id)

    total_assets = len(manifest.get("assets", []))

    log(f"  Found {total_assets} staged assets")
    log(f"  Validating {total_assets} staged assets...")

    validation = validate_assets(vendor, submission_type, submission_id)

    # =========================================================
    # DECLARED VS ACTUAL RECONCILIATION
    # =========================================================

    log("  Running declared vs actual asset reconciliation...")

    recon = reconcile_declared_vs_actual(vendor, submission_id, validation)

    missing_assets = recon["missing_assets"]
    extra_assets = recon["extra_assets"]
    present_but_failed = recon["present_but_failed"]

    log(f"    Missing assets: {len(missing_assets)}", 2)
    log(f"    Extra assets: {len(extra_assets)}", 2)
    log(f"    Present but failed validation: {len(present_but_failed)}", 2)

    safe_vendor = vendor.replace(" ", "_").lower()

    asset_manifest = {
        "vendor": vendor,
        "submission_type": submission_type,
        "submission_id": submission_id,
        "run_timestamp": RUN_TS,
        "summary": {
            "total_raw_blobs": len(asset_blobs),
            "zip_blobs": len(zip_blobs),
            "direct_supported_files": len(direct_supported_files),
            "total_assets": total_assets,
            "passed": len(validation["passed"]),
            "failed": len(validation["failed"]),
        },
        "assets": validation["passed"] + validation["failed"],
    }

    validation_log = {
        "vendor": vendor,
        "submission_type": submission_type,
        "submission_id": submission_id,
        "run_timestamp": RUN_TS,
        "failed": validation["failed"]
    }

    log("  Writing validation logs...")

    upload_json(
        asset_manifest,
        f"{paths['log_prefix']}asset_manifest_{safe_vendor}_{RUN_TS}.json"
    )

    upload_json(
        validation_log,
        f"{paths['log_prefix']}asset_validation_{safe_vendor}_{RUN_TS}.json"
    )

    log(
        f"✔ Asset validation completed for vendor '{vendor}' — "
        f"passed={len(validation['passed'])}, "
        f"failed={len(validation['failed'])}"
    )

    health_rows = validation["passed"] + validation["failed"]

    # ------------------------------------------
    # Add missing asset records
    # ------------------------------------------

    is_full_submission = submission_type in FULL_SUBMISSIONS

    missing_severity = "blocking" if is_full_submission else "warning"
    missing_status = "fail" if is_full_submission else "warning"

    # ------------------------------------------
    # Add missing asset records (only for FULL submissions)
    # ------------------------------------------

    if is_full_submission:

        for filename in missing_assets:
            health_rows.append({
                "filename": filename,
                "status": "fail",
                "issue_type": "missing_asset",
                "severity": "blocking",
                "autofixable": False,
                "details": "Declared in Product File but not found in your submission"
            })
    # ------------------------------------------
    # Add extra asset records
    # ------------------------------------------

    for filename in extra_assets:
        health_rows.append({
            "filename": filename,
            "status": "warning",
            "issue_type": "extra_asset",
            "severity": "warning",
            "autofixable": False,
            "details": "Asset exists but not declared in mapped.parquet"
        })

    df_health = pd.DataFrame(health_rows)

    # Columns intended for human report
    report_columns = [
        "filename",
        "issue_type",
        "severity",
        "autofixable",
        "details",
        "size_mb"
    ]

    df_report = df_health[[c for c in report_columns if c in df_health.columns]]

    buf = BytesIO()

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_report.to_excel(writer, index=False)

    silver_container.upload_blob(
        f"{paths['log_prefix']}health_report.xlsx",
        buf.getvalue(),
        overwrite=True
    )

    blocking = [
        r for r in health_rows
        if r.get("severity") == "blocking"
    ]

    if blocking:

        log(f"DEBUG submission_type normalized = [{submission_type}]")
        log(f"DEBUG missing_assets count = {len(missing_assets)}")
        log(f"DEBUG blocking count = {len(blocking)}")

        df_block = pd.DataFrame(blocking)

        report_columns = [
            "filename",
            "issue_type",
            "severity",
            "autofixable",
            "details",
            "size_mb"
        ]

        df_block = df_block[[c for c in report_columns if c in df_block.columns]]

        buf = BytesIO()

        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df_block.to_excel(writer, index=False)

        silver_container.upload_blob(
            f"{paths['log_prefix']}validation_report.xlsx",
            buf.getvalue(),
            overwrite=True
        )

        log("❌ Blocking validation issues detected. Pipeline will stop.")
        raise SystemExit(1)


    
    return {
            "status": "success",
            "blocking_issues": len(blocking),
            "passed": len(validation["passed"]),
            "failed": len(validation["failed"])
            }

        


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

    run_asset_etl_for_vendor(args.vendor, args.submission_type, args.submission_id)