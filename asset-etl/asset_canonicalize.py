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
    return os.path.basename(str(name).strip())


# =========================================================
# LOADERS
# =========================================================

def load_mapped_excel(vendor: str, submission_id: str) -> pd.DataFrame:

    path = f"in_review/vendor={vendor}/mapped/mapped.xlsx"

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


def load_asset_manifest(vendor: str, submission_id: str) -> Dict:

    path = f"in_review/vendor={vendor}/assets_workflow/submission={submission_id}/assets_staging/_asset_manifest.json"

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
# CANONICAL BUILD
# =========================================================

def build_media_canonical(vendor: str, submission_id: str):

    df = load_mapped_excel(vendor, submission_id)

    if df.empty:
        return pd.DataFrame()

    df = normalize_missing_values(df)

    df["part_number"] = df["part_number"].astype(str).str.strip()
    df["filename"] = df["filename"].apply(normalize_filename)

    manifest = load_asset_manifest(vendor, submission_id)

    asset_map = {
        a["filename"].lower(): a
        for a in manifest.get("assets", [])
        if "filename" in a
    }

    records = []
    sequence_tracker = {}

    missing_assets = []
    autofix_rows = []

    for _, row in df.iterrows():

        part = str(row["part_number"]).strip()
        media = str(row["mediatype"]).strip().upper()
        filename = normalize_filename(row["filename"])
        filetype = str(row["filetype"]).upper()

        if part.isdigit():
            part = part.zfill(5)

        # Ensure asset exists in staging
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

        # media category
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

        if media_category == "image":

            canonical_filetype = "JPG"
            canonical_filename = f"{part}_{media}_{seq}.jpg"

            if filetype == "GIF":
                transformations.append("gif_to_jpg")

            transformations.append("rename")

            if transformations:
                autofix_rows.append({
                    "vendor": vendor,
                    "part_number": part,
                    "original_filename": filename,
                    "canonical_filename": canonical_filename,
                    "actions": ",".join(transformations)
                })

        elif media_category == "document":

            canonical_filetype = filetype
            canonical_filename = f"{part}_{media}_{filename}"
            transformations.append("rename")

        else:

            canonical_filetype = filetype
            canonical_filename = filename

        entity_id = generate_entity_id(vendor, part)

        records.append({
            "_entity_id": entity_id,
            "vendor": vendor,
            "part_number": part,
            "media_type": media,
            "media_category": media_category,
            "original_filename": filename,
            "original_filetype": filetype,
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
            "created_at": datetime.utcnow().isoformat(),
        })

    if missing_assets:
        print(f"⚠ {len(missing_assets)} mapped assets missing in staging")

    return pd.DataFrame(records), pd.DataFrame(autofix_rows)


# =========================================================
# WRITE CANONICAL TABLE
# =========================================================

def write_media_canonical(vendor: str, df: pd.DataFrame, submission_id: str):

    base_path = f"in_review/vendor={vendor}/assets_workflow/submission={submission_id}/canonical/media_canonical"

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

    print(f"✅ Canonical table written ({len(df)} rows)")


# =========================================================
# ORCHESTRATOR
# =========================================================

def run_for_vendor(vendor: str, submission_type: str, submission_id: str):

    print(f"▶ Running Asset Canonicalization for vendor: {vendor}")
    print(f"Submission type: {submission_type}")
    print(f"Submission ID: {submission_id}")

    df, autofix_df = build_media_canonical(vendor, submission_id)

    if df.empty:
        print("No canonical rows generated.")
        return

    write_media_canonical(vendor, df, submission_id)

    buf = BytesIO()

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)

    container.upload_blob(
        f"in_review/vendor={vendor}/assets_workflow/submission={submission_id}/logs/mapped_autofixed.xlsx",
        buf.getvalue(),
        overwrite=True
    )

    if not autofix_df.empty:

        buf = BytesIO()

        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            autofix_df.to_excel(writer, index=False)

        container.upload_blob(
            f"in_review/vendor={vendor}/assets_workflow/submission={submission_id}/logs/autofix_report.xlsx",
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