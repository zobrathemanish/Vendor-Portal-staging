"""
Local bulk asset uploader for FGI Vendor Portal

Usage:
    python upload_assets_local.py "Dayton Parts" "C:/FGI/VendorAssets/assets.zip"

Behavior:
- Computes SHA256 hash
- Skips upload if unchanged
- Uploads new ZIP
- Deletes old assets ONLY after successful upload
"""

import os
import sys
from datetime import datetime
from azure.storage.blob import BlobServiceClient

# Ensure project root is on PYTHONPATH
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from services.azure_service import (
    get_blob_service_client,
    get_latest_asset_hash,
    delete_old_asset_zips,
    upload_blob,
)
from services.file_service import compute_file_hash


# -----------------------------
# VALIDATION
# -----------------------------
if len(sys.argv) != 3:
    print("❌ Usage: python upload_assets_local.py <VENDOR_NAME> <ZIP_PATH>")
    sys.exit(1)

vendor = sys.argv[1]
zip_path = sys.argv[2]

if not os.path.isfile(zip_path):
    print(f"❌ ZIP file not found: {zip_path}")
    sys.exit(1)

CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = "bronze"

if not CONNECTION_STRING:
    print("❌ AZURE_STORAGE_CONNECTION_STRING not set")
    sys.exit(1)


# -----------------------------
# SETUP
# -----------------------------
timestamp = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
vendor_folder = f"vendor={vendor}"
blob_service_client = get_blob_service_client(CONNECTION_STRING)
container_client = blob_service_client.get_container_client(CONTAINER_NAME)

assets_blob_path = f"raw/{vendor_folder}/assets/assets.zip"


# -----------------------------
# HASH CHECK
# -----------------------------
print("🔍 Computing local asset hash...")
new_hash = compute_file_hash(zip_path)

print("🔎 Checking existing asset hash...")
last_hash = get_latest_asset_hash(container_client, vendor_folder)

if last_hash == new_hash:
    print("🟡 Assets unchanged — upload skipped")
    sys.exit(0)


# -----------------------------
# UPLOAD
# -----------------------------
print("⬆️ Uploading assets ZIP...")
try:
    upload_blob(
        local_path=zip_path,
        blob_path=assets_blob_path,
        connection_string=CONNECTION_STRING,
        container_name=CONTAINER_NAME,
    )
except Exception as e:
    print(f"🔴 Upload failed: {e}")
    sys.exit(1)


# -----------------------------
# CLEANUP (SAFE)
# -----------------------------
print("🧹 Cleaning up old asset ZIPs...")
delete_old_asset_zips(container_client, vendor_folder)

print("✅ Asset upload completed successfully")
