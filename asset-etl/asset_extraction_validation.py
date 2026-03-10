# asset_extraction_validation.py

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

# =========================================================
# HELPERS
# =========================================================

def compute_file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def log(msg: str, indent: int = 0):
    print(" " * indent + msg, flush=True)


def blob_exists(container, path: str) -> bool:
    try:
        container.get_blob_client(path).get_blob_properties()
        return True
    except ResourceNotFoundError:
        return False


def download_blob(container, blob_path: str) -> bytes:
    blob = container.get_blob_client(blob_path)
    return blob.download_blob().readall()


def upload_json(payload: dict, blob_path: str):
    silver_container.upload_blob(
        name=blob_path,
        data=json.dumps(payload, indent=2),
        overwrite=True
    )


def vendor_paths(vendor: str, submission_id: str) -> Dict[str, str]:
    staging_prefix = f"in_review/vendor={vendor}/assets_staging/submission={submission_id}/"
    return {
        "asset_prefix": f"raw/domain-based/vendor={vendor}/assets/",
        "log_prefix": f"logs/vendor={vendor}/assets/submission={submission_id}/",
        "zip_hash_log": f"logs/vendor={vendor}/assets/zip_hashes.json",
        "staging_prefix": staging_prefix,
        "staging_manifest": f"{staging_prefix}_asset_manifest.json",
    }


def normalize_filename(filename: str) -> str:
    """
    Keep only the basename and normalize whitespace.
    """
    filename = os.path.basename(str(filename).strip())
    return filename


def build_staging_path(vendor: str, submission_id: str, filename: str) -> str:
    return f"in_review/vendor={vendor}/assets_staging/submission={submission_id}/{filename}"

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

def load_asset_manifest(vendor: str, submission_id: str) -> Dict[str, Any]:
    paths = vendor_paths(vendor, submission_id)
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


def save_asset_manifest(vendor: str, manifest: Dict[str, Any], submission_id: str):
    paths = vendor_paths(vendor, submission_id)

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


def clear_staging(vendor: str, submission_id: str):
    paths = vendor_paths(vendor, submission_id)
    prefix = paths["staging_prefix"]

    blobs = silver_container.list_blobs(name_starts_with=prefix)
    for blob in blobs:
        silver_container.delete_blob(blob.name)


def list_staged_assets(vendor: str, submission_id: str) -> List[str]:
    """
    Prefer manifest over Azure list_blobs for scale.
    Falls back to listing only if manifest is missing or empty.
    """
    manifest = load_asset_manifest(vendor, submission_id)
    assets = manifest.get("assets", [])

    if assets:
        return [
            item["source_blob_path"]
            for item in assets
            if item.get("source_blob_path")
        ]

    paths = vendor_paths(vendor, submission_id)
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


def extract_zip_assets(vendor: str,submission_id: str, zip_blob_path: str):
    log(f"  Extracting ZIP: {zip_blob_path}")

    paths = vendor_paths(vendor, submission_id)

    # download zip from bronze
    zip_bytes = download_blob(bronze_container, zip_blob_path)

    # ----------------------------
    # ZIP DUPLICATE DETECTION
    # ----------------------------
    zip_hash = compute_file_hash(zip_bytes)
    hash_log = load_zip_hashes(paths["zip_hash_log"])

####Commented temporarily
    # if zip_hash in hash_log["hashes"]:
    #     log(f"  Skipping duplicate ZIP: {zip_blob_path}")
    #     return

    # ----------------------------
    # LOAD CURRENT STAGING MANIFEST
    # ----------------------------
    manifest = load_asset_manifest(vendor, submission_id)

    existing_assets = {
        asset["filename"]: asset
        for asset in manifest.get("assets", [])
        if asset.get("filename")
    }
    existing_names = set(existing_assets.keys())

    extracted_count = 0
    skipped_existing_count = 0
    unsupported_count = 0

    with zipfile.ZipFile(BytesIO(zip_bytes)) as z:
        for file_in_zip in z.namelist():
            if file_in_zip.endswith("/"):
                continue

            raw_filename = os.path.basename(file_in_zip)
            if not raw_filename:
                continue

            filename = normalize_filename(raw_filename)
            ext = os.path.splitext(filename)[1].lower()

            if ext not in SUPPORTED_FORMATS:
                unsupported_count += 1
                continue

            data = z.read(file_in_zip)
            content_hash = compute_file_hash(data)

            # If same filename already exists in manifest, compare hash
            if filename in existing_assets:
                existing_asset = existing_assets[filename]
                if existing_asset.get("content_hash") == content_hash:
                    skipped_existing_count += 1
                    log(f"    → skipped existing identical {filename}", 4)
                    continue

                # same basename but different content: create deterministic unique name
                filename = _build_unique_filename(filename, existing_names)

            staging_path = build_staging_path(vendor, submission_id, filename)

            silver_container.upload_blob(
                name=staging_path,
                data=data,
                overwrite=True
            )

            manifest["assets"].append(
                {
                    "filename": filename,
                    "source_blob_path": staging_path,
                    "content_hash": content_hash,
                    "size_bytes": len(data),
                    "source_zip_blob_path": zip_blob_path,
                    "source_zip_hash": zip_hash,
                    "extracted_at": datetime.utcnow().isoformat()
                }
            )

            existing_names.add(filename)
            existing_assets[filename] = manifest["assets"][-1]

            extracted_count += 1
            log(f"    → extracted {filename}", 4)

    # persist manifest and zip hash only after successful ZIP processing
    save_asset_manifest(vendor, manifest, submission_id)

    hash_log["hashes"].append(zip_hash)
    save_zip_hashes(paths["zip_hash_log"], hash_log)

    log(
        f"  ZIP done: extracted={extracted_count}, "
        f"skipped_identical={skipped_existing_count}, "
        f"unsupported={unsupported_count}"
    )


# =========================================================
# STEP 2 — VALIDATE ASSETS
# =========================================================

def validate_assets(blob_paths: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    passed = []
    failed = []

    total = len(blob_paths)

    for idx, blob_path in enumerate(blob_paths, start=1):
        if idx == 1 or idx % 10 == 0 or idx == total:
            log(f"  Validating assets... ({idx}/{total})")

        filename = os.path.basename(blob_path)
        ext = os.path.splitext(filename)[1].lower()

        record = {
            "filename": filename,
            "blob_path": blob_path,
            "source_blob_path": blob_path,
            "status": "pass",
            "warnings": []
        }

        try:
            blob = silver_container.get_blob_client(blob_path)
            data = blob.download_blob().readall()

            size_mb = round(len(data) / (1024 * 1024), 2)
            record["size_mb"] = size_mb
            record["content_hash"] = compute_file_hash(data)

        except Exception:
            record["status"] = "fail"
            record["reason"] = "blob_read_error"
            failed.append(record)
            continue

        # FORMAT VALIDATION
        if ext not in SUPPORTED_FORMATS:
            record["status"] = "fail"
            record["reason"] = "unsupported_format"

        elif size_mb > MAX_MB:
            record["status"] = "fail"
            record["reason"] = "file_too_large"

        # IMAGE VALIDATION
        elif ext in SUPPORTED_IMAGE_FORMATS:
            try:
                with Image.open(BytesIO(data)) as img:
                    w, h = img.size
                    record["width"] = w
                    record["height"] = h

                    if w < MIN_WIDTH or h < MIN_HEIGHT:
                        record["warnings"].append("low resolution")

            except Exception:
                record["status"] = "fail"
                record["reason"] = "corrupt_image"

        # DOCUMENT VALIDATION
        elif ext in SUPPORTED_DOCUMENT_FORMATS:
            pass

        if record["status"] == "pass":
            passed.append(record)
        else:
            failed.append(record)

    return {
        "passed": passed,
        "failed": failed
    }


# =========================================================
# STEP 3 — MAIN ORCHESTRATION
# =========================================================

def run_asset_etl_for_vendor(vendor: str, submission_type: str, submission_id: str):
    if submission_type in ["asset_submission", "delta_asset_submission"]:
        clear_staging(vendor, submission_id)

    log(f"▶ Running Asset Validation for vendor: {vendor}")
    log(f"  Submission type: {submission_type}")

    paths = vendor_paths(vendor, submission_id)

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
        extract_zip_assets(vendor, submission_id, blob)

    staged_assets = list_staged_assets(vendor, submission_id)

    log(f"  Found {len(staged_assets)} staged assets")
    log("  Validating assets (this may take a moment)...")

    validation = validate_assets(staged_assets)

    safe_vendor = vendor.replace(" ", "_").lower()

    asset_manifest = {
        "vendor": vendor,
        "submission_type": submission_type,
        "submission_id" : submission_id,
        "run_timestamp": RUN_TS,
        "summary": {
            "total_raw_blobs": len(asset_blobs),
            "zip_blobs": len(zip_blobs),
            "direct_supported_files": len(direct_supported_files),
            "total_assets": len(staged_assets),
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