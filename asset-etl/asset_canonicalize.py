# asset_canonicalize.py

import os
from datetime import datetime
from io import BytesIO
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from azure.storage.blob import BlobServiceClient
import hashlib
import argparse
from dotenv import load_dotenv

## Purpose: “What SHOULD exist?” (Truth definition)

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
    return hashlib.sha256(
        f"{vendor}|{part_number}".encode("utf-8")
    ).hexdigest()


def load_mapped_excel(vendor: str) -> pd.DataFrame:

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


def normalize_missing_values(df: pd.DataFrame) -> pd.DataFrame:

    if "orientation" in df.columns:
        df["orientation"] = df["orientation"].fillna("GEN").replace("", "GEN")

    if "representation" in df.columns:
        df["representation"] = df["representation"].fillna("GEN").replace("", "GEN")

    return df


# =========================================================
# CORE
# =========================================================

def run_for_vendor(vendor: str, submission_type: str):

    print(f"▶ Running Asset Canonicalization for vendor: {vendor}")
    print(f"Submission type: {submission_type}")

    # Build canonical rows from mapped data
    df = build_media_canonical(vendor)

    if df.empty:
        print("No asset rows found in mapped.xlsx")
        return

    write_media_canonical(vendor, df)


def build_media_canonical(vendor: str) -> pd.DataFrame:

    df = load_mapped_excel(vendor)

    df = normalize_missing_values(df)

    if "part_number" in df.columns:
        df["part_number"] = df["part_number"].astype(str).str.strip()

    records = []
    sequence_tracker = {}

    for _, row in df.iterrows():

        part = row["part_number"]
        media = row["mediatype"]
        filename = row["filename"]
        filetype = str(row["filetype"]).upper()

        if pd.isna(part) or pd.isna(media) or pd.isna(filename) or pd.isna(filetype):
            continue

        part = str(part).strip()

        if part.isdigit():
            part = part.zfill(5)

        media = str(media).strip()
        filename = str(filename).strip()

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

        elif media_category == "document":

            canonical_filetype = filetype
            canonical_filename = f"{part}_{media}_{filename}"
            transformations.append("rename")

        else:

            canonical_filetype = filetype
            canonical_filename = filename

        entity_id = generate_entity_id(vendor, part)

        records.append(
            {
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
                "status": "active",
                "created_at": datetime.utcnow().isoformat(),
            }
        )

    return pd.DataFrame(records)


from azure.core.exceptions import ResourceNotFoundError

def write_media_canonical(vendor: str, df: pd.DataFrame):

    base_path = f"in_review/vendor={vendor}/canonical/media_canonical"

    parquet_path = f"{base_path}.parquet"
    excel_path = f"{base_path}.xlsx"

    # -----------------------------------------------------
    # Try loading existing canonical
    # -----------------------------------------------------
    try:

        existing_bytes = container.get_blob_client(parquet_path).download_blob().readall()

        existing_table = pq.read_table(BytesIO(existing_bytes))
        existing_df = existing_table.to_pandas()

        print(f"Found existing canonical rows: {len(existing_df)}")

        # merge new + existing
        df = pd.concat([existing_df, df], ignore_index=True)

        # deduplicate
        df = df.drop_duplicates(
            subset=["vendor", "part_number", "media_type", "canonical_filename"],
            keep="last"
        )

        print(f"After merge rows: {len(df)}")

    except ResourceNotFoundError:

        print("No existing canonical table found — creating new.")

    # -----------------------------------------------------
    # Write PARQUET (machine)
    # -----------------------------------------------------

    table = pa.Table.from_pandas(df)

    parquet_buf = BytesIO()
    pq.write_table(table, parquet_buf)

    container.upload_blob(
        parquet_path,
        parquet_buf.getvalue(),
        overwrite=True
    )

    # -----------------------------------------------------
    # Write EXCEL (human readable)
    # -----------------------------------------------------

    excel_buf = BytesIO()

    with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:

        df.to_excel(
            writer,
            sheet_name="media_canonical",
            index=False
        )

    container.upload_blob(
        excel_path,
        excel_buf.getvalue(),
        overwrite=True
    )

    print(f"✅ Media canonical updated: {parquet_path} + {excel_path}")

# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-type", required=True)

    args = parser.parse_args()

    run_for_vendor(args.vendor, args.submission_type)