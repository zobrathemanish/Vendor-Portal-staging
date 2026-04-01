# asset_canonicalize.py

import os
import json
import argparse
import hashlib
from datetime import datetime
from io import BytesIO
from typing import Dict, List

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError
from dotenv import load_dotenv

"""
Purpose:
Define the canonical asset plan.

Input sources:
- mapped.xlsx (vendor asset mapping)
- _asset_manifest.json (actual extracted assets)

Output:
- media_canonical.parquet
- media_canonical.xlsx
"""

# =========================================================
# CONFIG
# =========================================================

load_dotenv()

AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

if not AZURE_CONN_STR:
    raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set")

SILVER_CONTAINER = "silver"

IMAGE_TYPES = {"JPG", "JPEG", "PNG", "GIF", "TIF", "TIFF"}
DOCUMENT_TYPES = {"PDF"}

ASSET_SHEET_NAME = "Digital_Assets"

# =========================================================
# INIT
# =========================================================

blob_service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
container = blob_service.get_container_client(SILVER_CONTAINER)

# =========================================================
# HELPERS
# =========================================================

def generate_entity_id(vendor: str, part_number: str) -> str:
    return hashlib.sha256(f"{vendor}|{part_number}".encode("utf-8")).hexdigest()


def normalize_filename(name: str) -> str:

    name = os.path.basename(str(name).strip())

    name = name.lower()
    name = name.replace(" ", "")
    name = name.strip()

    return name


# =========================================================
# LOADERS
# =========================================================

def load_mapped_excel(vendor: str, submission_id: str) -> pd.DataFrame:

    path = f"approved/products_workflow/vendor={vendor}/products_etl_mapped.xlsx"

    raw = container.get_blob_client(path).download_blob().readall()

    try:
        df = pd.read_excel(
            BytesIO(raw),
            sheet_name=ASSET_SHEET_NAME,
            dtype={"Part Number": str}
        )
    except ValueError:
        return pd.DataFrame()

    df.columns = (
        df.columns
        .str.strip()
        .str.lower()
        .str.replace(" ", "_")
    )

    required_cols = {"part_number", "filename", "filetype"}
    media_cols = {"mediatype"}

    if not required_cols.issubset(df.columns) or not media_cols.issubset(df.columns):
        raise RuntimeError(
            f"Wrong sheet loaded for vendor={vendor}. "
            f"Columns found: {list(df.columns)}"
        )

    return df


def load_asset_manifest(vendor: str, submission_type: str, submission_id: str) -> Dict:

    path = (
        f"in_review/assets_workflow/"
        f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/"
        f"assets_staging/_asset_manifest.json"
    )

    try:
        raw = container.get_blob_client(path).download_blob().readall()
        return json.loads(raw)
    except Exception:
        return {"assets": []}
    


# =========================================================
# DATA NORMALIZATION
# =========================================================

def normalize_missing_values(df: pd.DataFrame) -> pd.DataFrame:

    if "orientation" in df.columns:
        df["orientation"] = df["orientation"].fillna("GEN").replace("", "GEN")

    if "representation" in df.columns:
        df["representation"] = df["representation"].fillna("GEN").replace("", "GEN")

    return df

# =========================================================
# ASSET INTEGRITY CHECK
# =========================================================

def detect_asset_integrity_issues(mapped_df: pd.DataFrame, manifest: Dict):

    declared = set(
        mapped_df["normalized_filename"]
        .astype(str)
    )

    uploaded = {
        normalize_filename(a["filename"])
        for a in manifest.get("assets", [])
        if "filename" in a
    }

    missing_assets = declared - uploaded
    extra_assets = uploaded - declared

    issues = []

    for f in missing_assets:

        issues.append({
            "filename": f,
            "issue_type": "missing_asset",
            "severity": "blocking",
            "autofixable": False,
            "details": "Declared in mapped.xlsx but not uploaded"
        })

    for f in extra_assets:

        issues.append({
            "filename": f,
            "issue_type": "extra_asset",
            "severity": "warning",
            "autofixable": False,
            "details": "Uploaded asset not declared in mapped.xlsx"
        })

    return issues
# =========================================================
# CANONICAL BUILD
# =========================================================

def build_media_canonical(vendor: str, submission_type: str, submission_id: str):

    df = load_mapped_excel(vendor, submission_id)

    if df.empty:
        return pd.DataFrame(), pd.DataFrame()

    df = normalize_missing_values(df)

    df["part_number"] = df["part_number"].astype(str).str.strip()
    df["original_filename"] = df["filename"].astype(str).str.strip()
    df["normalized_filename"] = df["original_filename"].apply(normalize_filename)

    manifest = load_asset_manifest(vendor, submission_type, submission_id)

    failed_assets = {
        normalize_filename(a["filename"])
        for a in manifest.get("assets", [])
        if a.get("validation_status") == "fail"
    }

    # -------------------------------------------------
    # Asset integrity check
    # -------------------------------------------------

    integrity_issues = detect_asset_integrity_issues(df, manifest)

    asset_map = {}

    for a in manifest.get("assets", []):

        if "filename" not in a:
            continue

        fname = normalize_filename(a["filename"])

        if fname in failed_assets:
            continue

        if a.get("issue_type") == "corrupt_image":
            continue

        asset_map[fname] = a

    records = []
    autofix_rows = []
    sequence_tracker = {}

    missing_assets = []

    for _, row in df.iterrows():

        part = str(row["part_number"]).strip()
        media = str(row["mediatype"]).strip().upper()
        original_filename = row["original_filename"]
        filename = row["normalized_filename"]
        filetype = str(row["filetype"]).upper()

        if part.isdigit():
            part = part.zfill(5)

        # -------------------------------------------------
        # Asset existence check
        # -------------------------------------------------

        asset_info = asset_map.get(filename.lower())

        if not asset_info:

            missing_assets.append({
                "part_number": part,
                "filename": filename
            })

            continue

        source_blob_path = asset_info["source_blob_path"]
        content_hash = asset_info.get("content_hash")
        size_bytes = asset_info.get("size_bytes")

        # -------------------------------------------------
        # Determine media category
        # -------------------------------------------------

        if filetype in IMAGE_TYPES:
            media_category = "image"

        elif filetype in DOCUMENT_TYPES:
            media_category = "document"

        else:
            media_category = "other"

        key = (part, media)
        sequence_tracker[key] = sequence_tracker.get(key, 0) + 1
        seq = f"{sequence_tracker[key]:02d}"

        transformations = []

        # -------------------------------------------------
        # IMAGE RULES
        # -------------------------------------------------

        if media_category == "image":

            canonical_filetype = "JPG"
            canonical_filename = f"{part}_{media}_{seq}.jpg"

            # GIF conversion
            if filetype == "GIF":

                transformations.append("gif_to_jpg")

                autofix_rows.append({
                    "vendor": vendor,
                    "part_number": part,
                    "filename": original_filename,
                    "issue_type": "gif_format",
                    "severity": "info",
                    "autofixable": True,
                    "details": "GIF converted to JPG automatically"
                })

            # Rename detection
            if filename.lower() != canonical_filename.lower():

                transformations.append("rename")

                autofix_rows.append({
                    "vendor": vendor,
                    "part_number": part,
                    "filename": original_filename,
                    "issue_type": "filename_normalized",
                    "severity": "info",
                    "autofixable": True,
                    "details": f"Renamed to canonical format {canonical_filename}"
                })

        # -------------------------------------------------
        # DOCUMENT RULES
        # -------------------------------------------------

        elif media_category == "document":

            canonical_filetype = filetype
            canonical_filename = f"{part}_{media}.{filetype.lower()}"

            if filename.lower() != canonical_filename.lower():

                transformations.append("rename")

                autofix_rows.append({
                    "vendor": vendor,
                    "part_number": part,
                    "filename": original_filename,
                    "issue_type": "filename_normalized",
                    "severity": "info",
                    "autofixable": True,
                    "details": f"Renamed to canonical format {canonical_filename}"
                })

        # -------------------------------------------------
        # OTHER TYPES
        # -------------------------------------------------

        else:

            canonical_filetype = filetype
            canonical_filename = filename

        # -------------------------------------------------
        # Build canonical record
        # -------------------------------------------------

        entity_id = generate_entity_id(vendor, part)

        records.append({

            "_entity_id": entity_id,
            "vendor": vendor,
            "part_number": part,
            "media_type": media,
            "media_category": media_category,
            "original_filename": original_filename,
            "original_filetype": filetype,
            "normalized_filename": filename,
            "canonical_filename": canonical_filename,
            "canonical_filetype": canonical_filetype,
            "sequence": seq,
            "orientation": row.get("orientation"),
            "representation": row.get("representation"),
            "transformations_required": transformations,
            "source_blob_path": source_blob_path,
            "content_hash": content_hash,
            "size_bytes": size_bytes,
            "status": "active",

            "canonical_id": hashlib.sha256(
                f"{vendor}|{part}|{canonical_filename}".encode()
            ).hexdigest(),

            "created_at": datetime.utcnow().isoformat(),
        })

    # -------------------------------------------------
    # Logging missing assets
    # -------------------------------------------------

    if missing_assets:
        print(f"Missing mapped assets not found in staging: {len(missing_assets)}")

    # -------------------------------------------------
    # Write integrity issues
    # -------------------------------------------------

    if integrity_issues:

        path = (
            f"in_review/assets_workflow/"
            f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/"
            f"reports/asset_integrity_issues.json"
        )

        container.upload_blob(
            path,
            json.dumps(integrity_issues, indent=2),
            overwrite=True
        )

    return pd.DataFrame(records), pd.DataFrame(autofix_rows)

# =========================================================
# WRITE CANONICAL TABLE
# =========================================================

def write_media_canonical(vendor: str, df: pd.DataFrame, submission_type:str, submission_id: str):

    base_path = (
        f"in_review/assets_workflow/"
        f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/"
        f"canonical/media_canonical"
    )

    parquet_path = f"{base_path}.parquet"
    excel_path = f"{base_path}.xlsx"

    try:

        existing_bytes = container.get_blob_client(parquet_path).download_blob().readall()

        existing_table = pq.read_table(BytesIO(existing_bytes))
        existing_df = existing_table.to_pandas()

        df = pd.concat([existing_df, df], ignore_index=True)

        df = df.drop_duplicates(
            subset=["vendor", "part_number", "media_type", "canonical_filename"],
            keep="last"
        )

        print(f"Canonical rows after merge: {len(df)}")

    except ResourceNotFoundError:

        print("No existing canonical found — creating new.")

    table = pa.Table.from_pandas(df)

    buf = BytesIO()
    pq.write_table(table, buf)

    container.upload_blob(
        parquet_path,
        buf.getvalue(),
        overwrite=True
    )

    excel_buf = BytesIO()

    with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="media_canonical", index=False)

    container.upload_blob(
        excel_path,
        excel_buf.getvalue(),
        overwrite=True
    )

    print(f"Canonical table written ({len(df)} rows)")


# =========================================================
# ORCHESTRATOR
# =========================================================

def run_for_vendor(vendor: str, submission_type: str, submission_id: str):

    print(f"[STEP] Running Asset Canonicalization for vendor: {vendor}")
    print(f"Submission type: {submission_type}")
    print(f"Submission ID: {submission_id}")

    df, autofix_df = build_media_canonical(vendor, submission_type, submission_id)

    if df.empty:

        print(" No assets matched mapped.xlsx")

        error_log = {
            "vendor": vendor,
            "submission_id": submission_id,
            "error": "NO_ASSETS_MATCHED_MAPPING",
            "message": "Uploaded assets do not match mapped.xlsx",
            "timestamp": datetime.utcnow().isoformat()
        }

        container.upload_blob(
            f"in_review/assets_workflow/vendor={vendor}/submission_type={submission_type}/submission={submission_id}/logs/canonical_error.json",
            json.dumps(error_log, indent=2),
            overwrite=True
        )

        raise RuntimeError("No assets matched mapped.xlsx")



    write_media_canonical(vendor, df, submission_type, submission_id)

    buf = BytesIO()

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)


    if not autofix_df.empty:

        buf = BytesIO()

        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            autofix_df.to_excel(writer, index=False)

        container.upload_blob(
            f"in_review/assets_workflow/vendor={vendor}/submission_type={submission_type}/submission={submission_id}/logs/autofix_report.xlsx",
            buf.getvalue(),
          
            overwrite=True
        )


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-type", required=True)
    parser.add_argument("--submission-id", required=True)

    args = parser.parse_args()

    run_for_vendor(args.vendor, args.submission_type, args.submission_id)