#pre_etl_ingest_mapping.py

import sys
import os

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
import json
import traceback
import datetime
from io import BytesIO

import yaml
import pandas as pd
from dotenv import load_dotenv
from lxml import etree
from azure.storage.blob import BlobServiceClient
from mapping_validation.mapping_engine.excel_mapper import ExcelMapper
from mapping_validation.vendor_adapters.vendor_factory import VendorAdapterFactory
import hashlib
from mapping_validation.helpers.file_ingestion import download_blob_bytes


from mapping_validation.mapping_engine.xml_mapper import XMLMapper
import argparse
from common.status_writer import write_status
from io import BytesIO
import pandas as pd
from datetime import datetime
from azure_writer import upload_parquet, upload_excel, upload_json

# PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# if PROJECT_ROOT not in sys.path:
#     sys.path.insert(0, PROJECT_ROOT)


load_dotenv()

# -------------------------------
# CONFIG
# -------------------------------
CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = "bronze"
RAW_PREFIX = "raw/vendor="  # inside the bronze container
PRODUCT_PREFIX = "products"
SILVER_CONTAINER = "silver"

MAPPING_OUTPUT_DIR = "mapped"
MAPPINGS_DIR = os.path.join(PROJECT_ROOT, "mapping_validation", "mappings")

blob_service = BlobServiceClient.from_connection_string(CONNECTION_STRING)

# ---------------------------------------------
# LOAD YAML WITH VENDOR NAME FALLBACK
# ---------------------------------------------
def load_vendor_mapping(vendor: str) -> dict | None:
    vendor_lower = vendor.lower().strip()
    candidates = [
        vendor_lower,
        vendor_lower.replace(" ", "_"),
        vendor_lower.split()[0],
        vendor_lower.split()[0].lower(),
    ]

    
    tried = []
    for base in dict.fromkeys(candidates):
        filename = os.path.join(MAPPINGS_DIR, f"{base}.yaml")
        tried.append(filename)
        if os.path.exists(filename):
            print(f"[OK] YAML found: {filename}")
            with open(filename, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)

    print(f"\n No YAML mapping found for vendor '{vendor}'.")
    print("   Tried the following paths:")
    for t in tried:
        print("   -", t)
    print()
    return None


def find_submission_files(vendor: str, workflow: str, submission_id: str):

    container = blob_service.get_container_client(CONTAINER_NAME)

    prefix = (
        f"{RAW_PREFIX}{vendor}/"
        f"{workflow}/"
        f"submission={submission_id}/"
    )

    blobs = []

    for blob in container.list_blobs(name_starts_with=prefix):
        blobs.append(blob.name)

    if not blobs:
        raise RuntimeError(f"No files found under {prefix}")

    return blobs

# ---------------------------------------------
# DISCOVER VENDORS
# ---------------------------------------------
def discover_vendors() -> list[str]:
    container = blob_service.get_container_client(CONTAINER_NAME)
    vendors = set()

    for blob in container.list_blobs(name_starts_with=RAW_PREFIX):
        parts = blob.name.split("/")
        for p in parts:
            if p.startswith("vendor="):
                vendors.add(p.split("=")[1])

    return sorted(vendors)


def compute_vendor_hash(vendor: str) -> str:
    """Reads product blobs for vendor and creates SHA-256 hash."""
    
    container = blob_service.get_container_client(CONTAINER_NAME)

    base_prefix = f"{RAW_PREFIX}{vendor}/product/"

    sha = hashlib.sha256()

    for blob in container.list_blobs(name_starts_with=base_prefix):
        data = download_blob_bytes(blob.name)
        sha.update(data)

    return sha.hexdigest()



# ================================================================
# UNIFIED INGESTION ERROR LOGGING (bronze + silver- vendor facing)
# ================================================================
def log_ingestion_error(
    vendor: str,
    stage: str,
    file: str | None,
    error: Exception,
    submission_id: str
):
    if not submission_id:
        raise RuntimeError("submission_id is REQUIRED for blocking errors")

    timestamp = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")

    log_record = {
        "vendor": vendor,
        "submission_id": submission_id,
        "file": file,
        "stage": stage,
        "severity": "ERROR",
        "error_type": type(error).__name__,
        "error_message": str(error),
        "logged_at": datetime.utcnow().isoformat() + "Z",
    }

    payload = json.dumps(log_record, indent=2)

    # 🔹 BRONZE (internal)
    bronze_path = f"raw/logs/vendor={vendor}/ingestion-{stage}-{timestamp}.json"
    blob_service.get_blob_client("bronze", bronze_path).upload_blob(payload, overwrite=True)

    # 🔴 SILVER (submission-scoped, blocking)
    silver_path = (
        f"rejected/logs/vendor={vendor}/"
        f"submission={submission_id}/"
        f"{stage.lower()}-error-{timestamp}.json"
    )

    blob_service.get_blob_client("silver", silver_path).upload_blob(payload, overwrite=True)


# ---------------------------------------------
# MAIN PROCESSOR
# ---------------------------------------------
def process_vendor(vendor: str, workflow:str, submission_id: str, submission_type:str):

    files = find_submission_files(vendor, workflow, submission_id)

    print(f"[FILES FOUND] {len(files)}")
    for f in files:
        print(" -", f)

    if not submission_id:
        raise RuntimeError("submission_id is REQUIRED")

    print(f"\n[CHECK] Checking vendor: {vendor}")


    # 4 — Perform mapping
    mapping = load_vendor_mapping(vendor)
    if mapping is None:
        print(f"[WARNING] Skipping vendor '{vendor}' — no YAML mapping found.")
        return

    adapter = VendorAdapterFactory.create(
        vendor,
        mapping,
        submission_id,
        workflow
    )
    try:
        combined = adapter.process()
        if workflow == "products":
            combined = {
                k: v for k, v in combined.items()
                if k not in ["pricing"]
            }

        elif workflow == "pricing":

            combined = {
                k: v for k, v in combined.items()
                if k == "pricing"
            }

    except Exception as e:
        write_status(
            vendor=vendor,
            stage="MAPPING",
            status="FAILED",
            message=str(e),
            submission_id=submission_id,
        )
        raise

    for k, df in combined.items():
        if df is None:
            print(f"[MAP] {k}: None")
        else:
            print(f"[MAP] {k}: rows={len(df)}, cols={list(df.columns)}")

    save_outputs(combined, vendor, workflow, submission_id, submission_type)

    # 5 — Save new hash
    outdir = os.path.join(
        MAPPING_OUTPUT_DIR,
        f"vendor={vendor}",
        f"submission_type={submission_type}",
        f"submission={submission_id}"
    )
    os.makedirs(outdir, exist_ok=True)


def save_outputs(
        combined: dict,
        vendor: str,
        workflow: str,
        submission_id: str,
        submission_type: str
    ):
    """
    Writes (LOCAL + AZURE):
      - Section-level parquet files
      - Unified parquet
      - Multi-sheet Excel
      - _SUCCESS.json marker with hash (Azure only)

    Safe drop-in replacement:
    - Keeps ALL existing local behavior
    - Adds Azure Silver persistence
    """

    import os
    import json
    import hashlib
    import datetime
    import pandas as pd
    from io import BytesIO
    from azure.storage.blob import BlobServiceClient

    # =========================================================
    # LOCAL OUTPUT (UNCHANGED)
    # =========================================================

    outdir = os.path.join(
        MAPPING_OUTPUT_DIR,
        f"vendor={vendor}",
        f"submission_type={submission_type}",
        f"submission={submission_id}"
    )
    os.makedirs(outdir, exist_ok=True)

    if not combined or len(combined.keys()) == 0:
        print(f" No mapped sections produced for vendor '{vendor}'. Skipping save_outputs().")
        return

    frames = []

    # Write section parquet (LOCAL)
    for section, df in combined.items():

        if df is None:
            print(f" Section '{section}' is None — skipping.")
            continue

        safe = section.replace(" ", "_")
        path = os.path.join(outdir, f"{safe}.parquet")

        df_copy = df.copy()
        for col in df_copy.columns:
            if df_copy[col].dtype == "object":
                df_copy[col] = df_copy[col].astype(str)

        df_copy.to_parquet(path, index=False)
        print(f"  Saved {safe}.parquet")

        df_copy["__Section"] = section
        frames.append(df_copy)

    if not frames:
        print(f"[WARNING] No valid DataFrames to unify for vendor '{vendor}'. Skipping unified outputs.")
        return

    # Unified parquet (LOCAL)
    df_unified = pd.concat(frames, ignore_index=True)
    unified_path = os.path.join(outdir, "mapped.parquet")

    IDENTIFIER_COLS = ["PN", "Part Number", "SKU", "Item Number"]
    for col in IDENTIFIER_COLS:
        if col in df_unified.columns:
            df_unified[col] = (
                df_unified[col]
                .astype(str)
                .str.strip()
                .replace({"nan": None})
            )

    df_unified.to_parquet(unified_path, index=False)
    print(f" Unified parquet saved: {unified_path}")

    # Excel (LOCAL)
    excel_path = os.path.join(outdir, "mapped.xlsx")
    with pd.ExcelWriter(excel_path, engine="xlsxwriter") as writer:
        for section, df in combined.items():
            if df is None or df.empty:
                continue
            safe = section.replace(" ", "_")
            df.to_excel(writer, sheet_name=safe[:31], index=False)

    print(f" Excel saved: {excel_path}")

    # Marker (LOCAL)
    marker_path = os.path.join(outdir, ".last_mapped")
    with open(marker_path, "w") as f:
        f.write(datetime.datetime.utcnow().isoformat())

    # =========================================================
    # AZURE SILVER UPLOAD 
    # =========================================================

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn:
        print(" Azure connection string not set — skipping Azure upload.")
        return

    blob_service = BlobServiceClient.from_connection_string(conn)
    container = blob_service.get_container_client("silver")

    base_prefix = (
        f"in_review/"
        f"workflow={workflow}/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}/"
        f"mapped"
    )

    def upload_bytes(blob_path: str, data: bytes):
        container.upload_blob(
            name=blob_path,
            data=data,
            overwrite=True
        )

    # Upload section parquets
    for section, df in combined.items():
        if df is None:
            continue

        safe = section.replace(" ", "_")
        buf = BytesIO()
        df_copy = df.copy()
        for col in df_copy.columns:
            if df_copy[col].dtype == "object":
                df_copy[col] = df_copy[col].astype(str)

        df_copy.to_parquet(buf, index=False)
        upload_bytes(f"{base_prefix}/{safe}.parquet", buf.getvalue())

    # Upload unified parquet + hash
    unified_buf = BytesIO()
    df_unified.to_parquet(unified_buf, index=False)
    unified_bytes = unified_buf.getvalue()


    upload_bytes(f"{base_prefix}/mapped.parquet", unified_bytes)

    mapped_hash = hashlib.sha256(unified_bytes).hexdigest()

    # Upload Excel
    excel_buf = BytesIO()
    with pd.ExcelWriter(excel_buf, engine="xlsxwriter") as writer:
        for section, df in combined.items():
            if df is None or df.empty:
                continue
            safe = section.replace(" ", "_")
            df.to_excel(writer, sheet_name=safe[:31], index=False)

    upload_bytes(f"{base_prefix}/mapped.xlsx", excel_buf.getvalue())

    # SUCCESS MARKER (Azure)
    success_payload = {
        "vendor": vendor,
        "submission_id": submission_id,
        "submission_type": submission_type,
        "status": "MAPPED",
        "mapped_hash": mapped_hash,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z"
    }

    upload_bytes(
        f"{base_prefix}/_SUCCESS.json",
        json.dumps(success_payload, indent=2).encode("utf-8")
    )

    print(f" Azure Silver mapping outputs written | hash={mapped_hash}")


# ---------------------------------------------
# ENTRYPOINT
# ---------------------------------------------
def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--submission-type", dest="submission_type", required=True)

    args = parser.parse_args()

    process_vendor(
        args.vendor,
        args.workflow,
        args.submission_id,
        args.submission_type
    )



if __name__ == "__main__":
    main()
