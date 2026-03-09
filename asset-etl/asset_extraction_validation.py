# asset_extraction_validation.py

import os
import json
from io import BytesIO
from datetime import datetime
from typing import List, Dict

from PIL import Image
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient
import zipfile

"""
Purpose:
Validate vendor assets before processing.
No transformations occur here.
Low resolution images are flagged but not rejected.
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

def log(msg: str, indent: int = 0):
    print(" " * indent + msg, flush=True)


def vendor_paths(vendor: str) -> Dict[str, str]:

    return {

        "asset_prefix": (
            f"raw/domain-based/vendor={vendor}/assets/"
        ),

        "log_prefix": (
            f"logs/vendor={vendor}/assets/"
        )
    }


def download_blob(container, blob_path: str) -> bytes:

    blob = container.get_blob_client(blob_path)
    return blob.download_blob().readall()


def upload_json(payload: dict, blob_path: str):

    silver_container.upload_blob(
        name=blob_path,
        data=json.dumps(payload, indent=2),
        overwrite=True
    )


# =========================================================
# STEP 1 — ENUMERATE ASSETS FROM AZURE
# =========================================================

def list_asset_blobs(asset_prefix: str) -> List[str]:

    blobs = bronze_container.list_blobs(
        name_starts_with=asset_prefix
    )

    return [
        b.name
        for b in blobs
        if not b.name.endswith("/")
    ]

def extract_zip_assets(vendor: str, zip_blob_path: str):

    print(f"  Extracting ZIP: {zip_blob_path}", flush=True)

    # download zip from bronze
    zip_bytes = download_blob(bronze_container, zip_blob_path)

    with zipfile.ZipFile(BytesIO(zip_bytes)) as z:

        for file in z.namelist():

            if file.endswith("/"):
                continue

            filename = os.path.basename(file)

            if not filename:
                continue

            ext = os.path.splitext(filename)[1].lower()

            if ext not in SUPPORTED_FORMATS:
                continue

            data = z.read(file)

            staging_path = (
                f"in_review/vendor={vendor}/"
                f"assets_staging/{filename}"
            )

            silver_container.upload_blob(
                name=staging_path,
                data=data,
                overwrite=True
            )

            print(f"    → extracted {filename}", flush=True)

def clear_staging(vendor):

    prefix = f"in_review/vendor={vendor}/assets_staging/"

    blobs = silver_container.list_blobs(name_starts_with=prefix)

    for blob in blobs:
        silver_container.delete_blob(blob.name)
# =========================================================
# STEP 2 — VALIDATE ASSETS
# =========================================================

def validate_assets(blob_paths: List[str]) -> Dict:

    passed = []
    failed = []

    total = len(blob_paths)

    for idx, blob_path in enumerate(blob_paths, start=1):

        if idx == 1 or idx % 10 == 0 or idx == total:
            print(f"  Validating assets... ({idx}/{total})", flush=True)

        filename = os.path.basename(blob_path)
        ext = os.path.splitext(filename)[1].lower()

        record = {
            "filename": filename,
            "blob_path": blob_path,
            "status": "pass",
            "warnings": []
        }

        try:

            blob = bronze_container.get_blob_client(blob_path)

            data = blob.download_blob().readall()

            size_mb = round(len(data) / (1024 * 1024), 2)

            record["size_mb"] = size_mb

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

def run_asset_etl_for_vendor(vendor: str, submission_type: str):
    if submission_type in ["asset_submission", "delta_asset_submission"]:
        clear_staging(vendor)

    print(f"▶ Running Asset Validation for vendor: {vendor}", flush=True)

    paths = vendor_paths(vendor)

    print("  Discovering assets in Azure...", flush=True)

    asset_blobs = list_asset_blobs(paths["asset_prefix"])

    print("  Extracting ZIP assets...", flush=True)

    for blob in asset_blobs:

        if blob.lower().endswith(".zip"):
            extract_zip_assets(vendor, blob)

    print(f"  Found {len(asset_blobs)} assets", flush=True)

    print("  Validating assets (this may take a moment)...", flush=True)

    validation = validate_assets(asset_blobs)

    asset_manifest = {

        "vendor": vendor,

        "run_timestamp": RUN_TS,

        "summary": {

            "total_assets": len(asset_blobs),

            "failed": len(validation["failed"])
        }

    }

    validation_log = {

        "vendor": vendor,

        "run_timestamp": RUN_TS,

        "failed": validation["failed"]

    }

    safe_vendor = vendor.replace(" ", "_").lower()

    print("  Writing validation logs...", flush=True)

    upload_json(

        asset_manifest,

        f"{paths['log_prefix']}asset_manifest_{safe_vendor}_{RUN_TS}.json"

    )

    upload_json(

        validation_log,

        f"{paths['log_prefix']}asset_validation_{safe_vendor}_{RUN_TS}.json"

    )

    print(
        f"✔ Asset validation completed for vendor '{vendor}' — "
        f"passed={len(validation['passed'])}, "
        f"failed={len(validation['failed'])}",
        flush=True
    )


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-type", required=False)

    args = parser.parse_args()

    run_asset_etl_for_vendor(args.vendor, args.submission_type)