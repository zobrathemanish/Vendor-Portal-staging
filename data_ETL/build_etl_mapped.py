"""
build_etl_mapped.py

Purpose:
---------
Rehydrate data_autofixed.parquet into a review-ready Excel that mirrors
mapped.xlsx schema, with optional enrichments and error isolation.

Supports:
---------
- Azure mode (default)
- Local mode (--local)

LOCAL ROOT:
-----------
ETL/data_etl/

LOCAL OUTPUT:
-------------
silver/ready/vendor=<vendor>/submission=<submission_id>/review/
"""

import os
import json
from io import BytesIO
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from azure.storage.blob import BlobServiceClient
import hashlib
import posixpath

# =========================================================
# CONFIG
# =========================================================
AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = os.getenv("SILVER_CONTAINER", "silver")

IN_REVIEW_ROOT = "in_review"
PRICING_REVIEW_ROOT = "post_pricing_review"
READY_ROOT = "ready"
READY_PRICING_ROOT = "ready_pricing_review"

AUTOFIX_DIR = "autofix"
CANONICAL_DIR = "canonical"
INTEGRITY_DIR = "integrity"
REVIEW_DIR = "review"

ERRORS_ALL_FILENAME = "errors_all.xlsx"
ERROR_COLUMNS = ["Error Severity", "Error Type", "Error Field", "Error Details"]

SUBMISSION_PREFIX = "submission="
LAST_APPROVED_FILENAME = "_last_approved.json"

CATEGORY_QUEUE_ROOT = "category_queue"
CATEGORY_ACTIVE_DIR = "active"



# =========================================================
# DELTA / HASH CONFIG
# =========================================================

HASH_FIELDS_BY_TAB = {

    # -------------------------
    # ITEM MASTER
    # -------------------------
    "Item_Master": [
        "Brand Label",
        "UNSPSC",
        "HazmatFlag",
        "Product Status",
        "Barcode Type",
        "Barcode Number",
        "Quantity UOM",
        "Quantity Size",
        "Minimum Order Quantity UOM",
        "Minimum Order Quantity",
        "VMRS Code",
        "Category",
    ],

    # -------------------------
    # DESCRIPTIONS
    # -------------------------
    "Descriptions": [
        "Description Code",
        "Description Value",
        "Sequence",
    ],

    # -------------------------
    # EXTENDED INFO
    # -------------------------
    "Extended_Info": [
        "Extended Info Code",
        "Extended Info Value",
    ],

    # -------------------------
    # ATTRIBUTES
    # -------------------------
    "Attributes": [
        "Attribute Name",
        "Attribute Value",
    ],

    # -------------------------
    # PACKAGES
    # -------------------------
    "Packages": [
        "Package UOM",
        "Package Quantity of Eaches",
        "Weight UOM",
        "Weight",
        "Dimension UOM",
        "Merch Length",
        "Merch Width",
        "Merch Height",
        "Ship Length",
        "Ship Width",
        "Ship Height",
        "Package Content",
    ],

    # -------------------------
    # DIGITAL ASSETS
    # -------------------------
    "Digital_Assets": [
        "MediaType",
        "FileName",
        "FilePath",
        "FileType",
        "Representation",
        "Orientation",
        "Height",
        "Width",
    ],

    # -------------------------
    # PRICING
    # -------------------------
    "Pricing": [
        "Pricing Method",
        "Currency",
        "MOQ Unit",
        "MOQ",
        "Pricing Type",
        "List Price",
        "Jobber Price",
        "Discount %",
        "Dealer Price",
        "Net Price",
        "Category",
        "POP Code",
        "Effective Date",
        "Notes",
    ],
}

DELTA_INSERT = "insert"
DELTA_UPDATE = "update"
DELTA_DELETE = "delete"


#Helper
INTERNAL_COLUMNS = [
    "_row_hash_before",
    "_norm_part_number",
]

def strip_internal_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=[c for c in INTERNAL_COLUMNS if c in df.columns], errors="ignore")


# =========================================================
# PROJECT ROOT (LOCAL MODE)
# =========================================================
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))  # ETL/data_etl

def local_in_review_vendor(vendor: str, mode: str) -> str:
    root = PRICING_REVIEW_ROOT if mode == "post_review" else IN_REVIEW_ROOT
    return os.path.join(PROJECT_ROOT, "silver", root, f"vendor={vendor}")

def local_ready_vendor_submission(vendor: str, submission_id: str, mode: str) -> str:
    ready_root = READY_PRICING_ROOT if mode == "post_review" else READY_ROOT

    return os.path.join(
        PROJECT_ROOT,
        "silver",
        ready_root,
        f"vendor={vendor}",
        f"submission={submission_id}"
    )

def local_ready_vendor_root(vendor: str) -> str:
    return os.path.join(PROJECT_ROOT, "silver", READY_ROOT, f"vendor={vendor}")

def load_current_state_snapshot(container, vendor: str, local: bool) -> Dict[str, pd.DataFrame]:
    """
    Load baseline from:
    silver/approved/current_state/vendor=<vendor>/etl_mapped.parquet
    """

    baseline_tabs = {}

    if local:
        path = os.path.join(
            PROJECT_ROOT,
            "silver",
            "approved",
            "current_state",
            f"vendor={vendor}",
            "etl_mapped.parquet"
        )

        if not os.path.exists(path):
            print("ℹ️ No current_state baseline found (first run).")
            return {}

        df = read_local_parquet(path)

    else:
        blob_path = (
            f"approved/current_state/"
            f"vendor={vendor}/etl_mapped.parquet"
        )

        try:
            b = download_blob_bytes(container, blob_path)
            df = df_from_parquet_bytes(b)
        except Exception:
            print("ℹ️ No current_state baseline found (first run).")
            return {}

    # Split back into tabs
    for tab_name, schema_cols in SCHEMA_TABS.items():
        chunk = df[df["__Section"] == tab_name].copy()

        if chunk.empty:
            continue

        # Ensure row hash exists
        if tab_name in HASH_FIELDS_BY_TAB:
            chunk["_row_hash_before"] = chunk.apply(
                lambda r: compute_row_hash(r, HASH_FIELDS_BY_TAB[tab_name]),
                axis=1
            )

        baseline_tabs[tab_name] = chunk

    print("✅ Loaded baseline from current_state (parquet)")

    return baseline_tabs


def promote_to_category_queue(container, vendor: str, submission_id: str, local: bool):
    print(f"📦 Promoting to CATEGORY_QUEUE | vendor={vendor}")

    source_prefix = (
        f"{READY_PRICING_ROOT}/vendor={vendor}/submission={submission_id}/review/"
    )

    dest_prefix = (
        f"{CATEGORY_QUEUE_ROOT}/vendor={vendor}/{CATEGORY_ACTIVE_DIR}/"
    )

    if local:
        src = os.path.join(
            PROJECT_ROOT,
            "silver",
            READY_PRICING_ROOT,
            f"vendor={vendor}",
            f"submission={submission_id}",
            "review"
        )

        dest = os.path.join(
            PROJECT_ROOT,
            "silver",
            CATEGORY_QUEUE_ROOT,
            f"vendor={vendor}",
            CATEGORY_ACTIVE_DIR
        )

        if not os.path.exists(src):
            print(" No ready_pricing_review review folder found")
            return

        # Clear previous active queue
        if os.path.exists(dest):
            import shutil
            shutil.rmtree(dest)

        shutil.copytree(src, dest)

    else:
        blobs = list(container.list_blobs(name_starts_with=source_prefix))

        if not blobs:
            print(" No ready_pricing_review files found")
            return

        # Delete existing active queue
        existing = list(container.list_blobs(name_starts_with=dest_prefix))
        for blob in existing:
            container.delete_blob(blob.name)

        # Copy new files
        for blob in blobs:
            data = download_blob_bytes(container, blob.name)
            rel = blob.name.replace(source_prefix, "")
            upload_blob_bytes(container, f"{dest_prefix}{rel}", data)

    # Write metadata
    metadata = {
        "vendor": vendor,
        "submission_id": submission_id,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat()
    }

    metadata_path = f"{CATEGORY_QUEUE_ROOT}/vendor={vendor}/{CATEGORY_ACTIVE_DIR}/metadata.json"

    if local:
        write_local_bytes(
            os.path.join(
                PROJECT_ROOT,
                "silver",
                CATEGORY_QUEUE_ROOT,
                f"vendor={vendor}",
                CATEGORY_ACTIVE_DIR,
                "metadata.json"
            ),
            json.dumps(metadata, indent=2).encode()
        )
    else:
        upload_blob_bytes(container, metadata_path, json.dumps(metadata, indent=2).encode())

    print("✅ CATEGORY_QUEUE promotion complete")


# =========================================================
# AZURE HELPERS
# =========================================================
def get_container():
    if not AZURE_CONN_STR:
        raise RuntimeError("Missing AZURE_STORAGE_CONNECTION_STRING")
    svc = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
    return svc.get_container_client(SILVER_CONTAINER)


def download_blob_bytes(container, path: str) -> bytes:
    return container.get_blob_client(path).download_blob().readall()


def upload_blob_bytes(container, path: str, data: bytes):
    container.upload_blob(path, data, overwrite=True)


# =========================================================
# LOCAL HELPERS
# =========================================================
def read_local_parquet(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing file: {path}")
    return pd.read_parquet(path)


def read_local_json(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing file: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_local_bytes(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)

def write_last_approved(container, vendor, submission_id, local):
    payload = {
        "vendor": vendor,
        "submission_id": submission_id,
        "updated_at": datetime.utcnow().isoformat()
    }

    if local:
        path = os.path.join(
            local_ready_vendor_root(vendor),
            LAST_APPROVED_FILENAME
        )
        write_local_bytes(path, json.dumps(payload, indent=2).encode())
    else:
        path = f"{READY_ROOT}/vendor={vendor}/{LAST_APPROVED_FILENAME}"
        upload_blob_bytes(container, path, json.dumps(payload, indent=2).encode())

def promote_to_approved(container, vendor: str, submission_id: str, local: bool, mode: str):
    """
    Promote ready → approved ONLY for post_review mode.
    """

    if mode != "post_review":
        print("ℹ️  Promotion skipped (not post_review mode)")
        return

    print(f"🚀 Promoting submission → APPROVED | vendor={vendor}")

    source_root = READY_PRICING_ROOT
    approved_root = "approved"

    if local:
        src = os.path.join(
            PROJECT_ROOT,
            "silver",
            source_root,
            f"vendor={vendor}",
            f"submission={submission_id}"
        )

        dest = os.path.join(
            PROJECT_ROOT,
            "silver",
            approved_root,
            f"vendor={vendor}",
            f"submission={submission_id}"
        )

        if not os.path.exists(src):
            print(" Promotion failed: ready_pricing_review not found")
            return

        os.makedirs(dest, exist_ok=True)

        for root, dirs, files in os.walk(src):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), src)
                dest_path = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with open(os.path.join(root, f), "rb") as rf:
                    write_local_bytes(dest_path, rf.read())

        write_last_approved(container, vendor, submission_id, local=True)

    else:
        src_prefix = f"{source_root}/vendor={vendor}/submission={submission_id}/"
        dest_prefix = f"approved/vendor={vendor}/submission={submission_id}/"

        blobs = list(container.list_blobs(name_starts_with=src_prefix))

        if not blobs:
            print(" Promotion failed: no ready_pricing_review files found")
            return

        for blob in blobs:
            data = download_blob_bytes(container, blob.name)
            rel = blob.name.replace(src_prefix, "")
            dest_path = f"{dest_prefix}{rel}"
            upload_blob_bytes(container, dest_path, data)

        write_last_approved(container, vendor, submission_id, local=False)

    print(f"✅ PROMOTION COMPLETE → approved/vendor={vendor}/submission={submission_id}")



# =========================================================
# BYTES HELPERS
# =========================================================
def df_from_parquet_bytes(b: bytes) -> pd.DataFrame:
    return pq.read_table(BytesIO(b)).to_pandas()


def parquet_bytes_from_df(df: pd.DataFrame) -> bytes:
    df = df.copy()

    IDENTIFIER_COLUMNS = {"Part Number", "Vendor", "__Section"}

    for col in df.columns:
        if col in IDENTIFIER_COLUMNS:
            df[col] = df[col].astype("string")
            continue

        if df[col].dtype == "object":
            numeric = pd.to_numeric(df[col], errors="coerce")

            if numeric.notna().mean() > 0.9:
                df[col] = numeric
            else:
                df[col] = df[col].astype("string")

    buf = BytesIO()
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, buf)
    return buf.getvalue()

def load_last_approved_submission_id(container, vendor: str, local: bool) -> str | None:
    path = (
        os.path.join(local_ready_vendor_root(vendor), LAST_APPROVED_FILENAME)
        if local
        else f"{READY_ROOT}/vendor={vendor}/{LAST_APPROVED_FILENAME}"
    )

    try:
        if local:
            if not os.path.exists(path):
                print(f"ℹ️  Last-approved pointer NOT FOUND → {path}")
                return None
            with open(path, "r", encoding="utf-8") as f:
                sid = json.load(f).get("submission_id")
        else:
            b = download_blob_bytes(container, path)
            sid = json.loads(b.decode("utf-8")).get("submission_id")

        print(f"✅ Last-approved pointer FOUND → submission_id={sid}")
        return sid

    except Exception as e:
        print(f"⚠️ Failed to read last-approved pointer → {path}")
        print(f"    Reason: {e}")
        return None
    


def load_previous_etl_mapped(container, vendor: str, approved_submission_id: str | None, local: bool, mode: str) -> Dict[str, pd.DataFrame]:
    """
    Load baseline etl_mapped.xlsx for the last approved submission.
    Returns {} if missing / not set.
    """
    if not approved_submission_id:
        return {}

    try:
        if local:
            path = os.path.join(
                local_ready_vendor_submission(vendor, approved_submission_id, mode),
                REVIEW_DIR,
                "etl_mapped.xlsx"
            )
            if not os.path.exists(path):
                return {}
            xls = pd.ExcelFile(path)
        else:
            blob_path = (
                f"{READY_ROOT}/vendor={vendor}/"
                f"submission={approved_submission_id}/"
                f"{REVIEW_DIR}/etl_mapped.xlsx"
            )
            b = download_blob_bytes(container, blob_path)
            xls = pd.ExcelFile(BytesIO(b))

        return {sheet: xls.parse(sheet) for sheet in xls.sheet_names}
    except Exception:
        return {}

    
def normalize_part_number(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip().lstrip("0")

def normalize_value(value):
    if pd.isna(value):
        return ""

    # Numbers (prices, quantities)
    if isinstance(value, (int, float)):
        return f"{round(float(value), 4):.4f}"

    # Timestamps / dates
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.date().isoformat()

    # Strings
    return str(value).strip().upper()

# =========================================================
# SCHEMA CONTRACT (LOCKED)
# =========================================================
SCHEMA_TABS: Dict[str, List[str]] = {
    "Item_Master": [
        "Brand Label", "Part Number", "UNSPSC", "HazmatFlag",
        "Product Status", "Barcode Type", "Barcode Number",
        "Quantity UOM", "Quantity Size",
        "Minimum Order Quantity UOM", "Minimum Order Quantity",
        "VMRS Code", "Category",
    ],
    "Descriptions": [
        "Part Number", "Description Change Type",
        "Description Code", "Description Value", "Sequence",
    ],
    "Extended_Info": [
        "Part Number", "Extended Info Change Type",
        "Extended Info Code", "Extended Info Value",
    ],
    "Attributes": [
        "Part Number", "Attribute Change Type",
        "Attribute Name", "Attribute Value",
    ],
    "Packages": [
        "Part Number", "Package Change Type",
        "Package UOM", "Package Quantity of Eaches",
        "Weight UOM", "Weight",
        "Dimension UOM",
        "Merch Length", "Merch Width", "Merch Height",
        "Ship Length", "Ship Width", "Ship Height",
        "Package Content",
    ],
    "Digital_Assets": [
        "Part Number", "Digital Change Type",
        "MediaType", "FileName", "FilePath", "FileType",
        "Representation", "Orientation", "Height", "Width",
    ],
    "Pricing": [
        "Vendor", "Part Number", "Pricing Method",
        "Currency", "MOQ Unit", "MOQ",
        "Pricing Change Type", "Pricing Type",
        "List Price", "Jobber Price", "Discount %",
        "Dealer Price", "Net Price",
        "Category", "POP Code", "Effective Date", "Notes",
    ],
}


# =========================================================
# CORE HELPERS
# =========================================================
def _ensure_columns(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = pd.NA
    return out[cols]


def _split_tabs_from_autofixed(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """
    Split flat autofixed dataframe back into mapped.xlsx tabs.

    Accepts:
      - '__Section' (preferred / future)
      - '_sheet'    (current local autofix output)
    """

    if "__Section" in df.columns:
        section_col = "__Section"
    elif "_sheet" in df.columns:
        section_col = "_sheet"
    else:
        raise RuntimeError(
            " data_autofixed.parquet must contain '__Section' or '_sheet' "
            "to rehydrate mapped tabs."
        )

    out: Dict[str, pd.DataFrame] = {}

    for tab_name, schema_cols in SCHEMA_TABS.items():
        chunk = df[df[section_col] == tab_name].copy()

        if chunk.empty:
            out[tab_name] = pd.DataFrame(columns=schema_cols)
            continue

        # Remove internal columns
        drop_cols = [c for c in ["__Section", "_sheet"] if c in chunk.columns]
        chunk = chunk.drop(columns=drop_cols, errors="ignore")

        # Ensure schema columns exist
        for c in schema_cols:
            if c not in chunk.columns:
                chunk[c] = pd.NA

        # Drop fully empty rows
        chunk = chunk.dropna(how="all", subset=schema_cols)

        out[tab_name] = chunk[schema_cols].reset_index(drop=True)

    return out

def compute_row_hash(row: pd.Series, fields: List[str]) -> str:
    values = [normalize_value(row.get(f)) for f in fields]
    payload = "|".join(values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_dataset_hash(tabs: Dict[str, pd.DataFrame]) -> str:
    payload = []

    for tab, df in tabs.items():
        if tab not in HASH_FIELDS_BY_TAB or df.empty:
            continue

        for _, r in df.iterrows():
            payload.append(compute_row_hash(r, HASH_FIELDS_BY_TAB[tab]))

    joined = "||".join(payload)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()

def build_delta_tabs(
    current_tabs: Dict[str, pd.DataFrame],
    previous_tabs: Dict[str, pd.DataFrame],
) -> Dict[str, pd.DataFrame]:
    """
    Build delta tabs with explicit delta types:
    - insert
    - update
    - delete
    """

    delta_tabs: Dict[str, pd.DataFrame] = {}

    for tab, curr_df in current_tabs.items():

        if tab not in HASH_FIELDS_BY_TAB:
            continue

        prev_df = previous_tabs.get(tab)

        # =====================================================
        # 🟢 FIRST RUN / NO BASELINE → EVERYTHING IS INSERT
        # =====================================================
        if prev_df is None or prev_df.empty:
            if curr_df.empty:
                continue

            print(f"   🟢 No baseline for tab={tab} → marking all rows as INSERT")

            delta_df = curr_df.copy()
            delta_df["_delta_type"] = DELTA_INSERT
            delta_df["_review_decision"] = pd.NA
            delta_df["_review_comment"] = pd.NA

            # Drop internal-only helpers if present
            delta_df = delta_df.drop(
                columns=[c for c in ["_norm_part_number"] if c in delta_df.columns],
                errors="ignore",
            )

            delta_tabs[tab] = delta_df.reset_index(drop=True)
            continue

        # =====================================================
        # BOTH EMPTY → NO DELTA
        # =====================================================
        if curr_df.empty and prev_df.empty:
            continue

        if "_row_hash_before" not in curr_df.columns or "_row_hash_before" not in prev_df.columns:
            continue

        # =====================================================
        # NORMAL DELTA MODE (baseline exists)
        # =====================================================
        curr_df = curr_df.copy()
        prev_df = prev_df.copy()

        curr_df["_norm_part_number"] = curr_df["Part Number"].apply(normalize_part_number)
        prev_df["_norm_part_number"] = prev_df["Part Number"].apply(normalize_part_number)

        print(
            f"   Join key preview (current): "
            f"{curr_df['_norm_part_number'].dropna().unique()[:5]}"
        )
        print(
            f"   Join key preview (baseline): "
            f"{prev_df['_norm_part_number'].dropna().unique()[:5]}"
        )

        curr_idx = curr_df.set_index("_norm_part_number", drop=False)
        prev_idx = prev_df.set_index("_norm_part_number", drop=False)

        changed_rows = []

        # --------------------
        # INSERTS & UPDATES
        # --------------------
        for part, curr_row in curr_idx.iterrows():

            # INSERT
            if part not in prev_idx.index:
                row = curr_row.copy()
                row["_delta_type"] = DELTA_INSERT
                changed_rows.append(row)
                continue

            prev_matches = prev_idx.loc[part]

            # UPDATE (multiple baseline rows)
            if isinstance(prev_matches, pd.DataFrame):
                prev_hashes = prev_matches["_row_hash_before"].tolist()
                if curr_row["_row_hash_before"] not in prev_hashes:
                    row = curr_row.copy()
                    row["_delta_type"] = DELTA_UPDATE
                    changed_rows.append(row)

            # UPDATE (single baseline row)
            else:
                if curr_row["_row_hash_before"] != prev_matches["_row_hash_before"]:
                    row = curr_row.copy()
                    row["_delta_type"] = DELTA_UPDATE
                    changed_rows.append(row)

        # --------------------
        # DELETES
        # --------------------
        for part, prev_row in prev_idx.iterrows():
            if part not in curr_idx.index:
                row = prev_row.copy()
                row["_delta_type"] = DELTA_DELETE
                changed_rows.append(row)

        # --------------------
        # FINALIZE TAB
        # --------------------
        if changed_rows:
            delta_df = pd.DataFrame(changed_rows).reset_index(drop=True)

            # 🔒 STRICT SCHEMA ENFORCEMENT
            base_cols = SCHEMA_TABS[tab]
            system_cols = ["_delta_type", "_review_decision", "_review_comment", "_row_hash_before"]

            keep_cols = [c for c in base_cols if c in delta_df.columns] + \
                        [c for c in system_cols if c in delta_df.columns]

            delta_df = delta_df[keep_cols]


            delta_df = delta_df.drop(
                columns=[c for c in ["_norm_part_number"] if c in delta_df.columns],
                errors="ignore",
            )

            delta_df["_review_decision"] = pd.NA
            delta_df["_review_comment"] = pd.NA

            delta_tabs[tab] = delta_df

    return delta_tabs




# =========================================================
# LOAD OPTIONAL UPSTREAM OUTPUTS
# =========================================================
def load_optional_parquet(container, path_azure: str, path_local: str, local: bool):
    try:
        if local:
            return read_local_parquet(path_local)
        return df_from_parquet_bytes(download_blob_bytes(container, path_azure))
    except Exception:
        return None


def load_optional_json(container, path_azure: str, path_local: str, local: bool):
    try:
        if local:
            return read_local_json(path_local)
        return json.loads(download_blob_bytes(container, path_azure).decode())
    except Exception:
        return None
    
def build_review_reports(container, vendor: str, submission_id: str, local: bool, mode: str):
    """
    Builds etl_review_reports.xlsx which contains:
    - Health report
    - Autofix summary
    - Autofix transformations
    - Integrity issues
    - Canonical summary
    - Business review TODOs
    - Run metadata
    """
    print(f"📝 Building review report workbook for vendor: {vendor}")

    # -----------------------------------------------------
    # Paths
    # -----------------------------------------------------
    if local:
        base = os.path.join(
            local_in_review_vendor(vendor, mode),
            f"submission={submission_id}"
        )
    else:
        root = PRICING_REVIEW_ROOT if mode == "post_review" else IN_REVIEW_ROOT
        base = f"{root}/vendor={vendor}/submission={submission_id}"


    # Optional inputs
    health_path_parquet  = os.path.join(base, "profiling", "health_issues.parquet")
    health_path_json     = os.path.join(base, "profiling", "health_summary.json")

    autofix_report_path = os.path.join(base, AUTOFIX_DIR, "autofix_report.parquet")

    canonical_summary_path = os.path.join(base, CANONICAL_DIR, "canonical_summary.json")
    
    integrity_issues_path  = os.path.join(base, INTEGRITY_DIR, "integrity_issues.parquet")
    integrity_summary_path = os.path.join(base, INTEGRITY_DIR, "integrity_summary.json")

    media_canonical_path = os.path.join(base, CANONICAL_DIR, "media_canonical.parquet")
    media_canonical_excel_path = os.path.join(base, CANONICAL_DIR, "media_canonical.xlsx")

    # -----------------------------------------------------
    # Load everything if exists
    # -----------------------------------------------------
    def load_parquet_optional(path_azure, path_local):
        try:
            if local:
                return read_local_parquet(path_local)
            return df_from_parquet_bytes(download_blob_bytes(container, path_azure))
        except:
            return pd.DataFrame()

    def load_json_optional(path_azure, path_local):
        try:
            if local:
                return read_local_json(path_local)
            return json.loads(download_blob_bytes(container, path_azure).decode())
        except:
            return {}
        
    def _pick_issue_label(r: pd.Series) -> str:
        """
        Safely extract an issue label across profiling / integrity schema versions.
        """
        return (
            r.get("issue_type")
            or r.get("issue")
            or r.get("issue_code")
            or r.get("rule")
            or r.get("message")
            or "UNKNOWN"
        )


    health_df     = load_parquet_optional(health_path_parquet, health_path_parquet)
    health_summary = load_json_optional(health_path_json, health_path_json)

    autofix_report_df = load_parquet_optional(
        autofix_report_path,
        autofix_report_path
    )

    if not autofix_report_df.empty:
        autofix_summary = {
            "total_rows": len(autofix_report_df),
            "resolved_count": int((autofix_report_df.get("resolved") == True).sum())
                if "resolved" in autofix_report_df.columns else None,
            "converted_count": int((autofix_report_df.get("conversion_applied") == True).sum())
                if "conversion_applied" in autofix_report_df.columns else None,
        }
    else:
        autofix_summary = {}

    canonical_summary = load_json_optional(canonical_summary_path, canonical_summary_path)
    integrity_df      = load_parquet_optional(integrity_issues_path, integrity_issues_path)
    integrity_summary = load_json_optional(integrity_summary_path, integrity_summary_path)

    # -----------------------------------------------------
    # Load Media Canonical (prefer parquet, fallback excel)
    # -----------------------------------------------------
    media_canonical_df = load_parquet_optional(
        media_canonical_path,
        media_canonical_path
    )

    if media_canonical_df is None or media_canonical_df.empty:
        try:
            if local:
                media_canonical_df = pd.read_excel(media_canonical_excel_path)
            else:
                b = download_blob_bytes(container, media_canonical_excel_path)
                media_canonical_df = pd.read_excel(BytesIO(b))
        except:
            media_canonical_df = pd.DataFrame()

    # -----------------------------------------------------
    # Generate Business Review TO-DOs
    # -----------------------------------------------------
    review_tasks = []

    if not integrity_df.empty:
        for _, r in integrity_df.iterrows():
            severity = str(r.get("severity", "info")).upper()
            task = f"[{severity}] {_pick_issue_label(r)} → {r.get('join_value', '')}"
            review_tasks.append({"issue": task})


    if not health_df.empty:
        for _, r in health_df.iterrows():
            task = f"[HEALTH] {_pick_issue_label(r)} → {r.get('column', '')}"
            review_tasks.append({"issue": task})

    print("Health columns:", health_df.columns.tolist())
    print("Integrity columns:", integrity_df.columns.tolist())


    review_df = pd.DataFrame(review_tasks)

    # -----------------------------------------------------
    # Write Excel file
    # -----------------------------------------------------
    ready_root = READY_PRICING_ROOT if mode == "post_review" else READY_ROOT

    if local:
        out_path = os.path.join(
            PROJECT_ROOT,
            "silver",
            ready_root,
            f"vendor={vendor}",
            f"submission={submission_id}",
            REVIEW_DIR,
            "etl_review_reports.xlsx"
        )
    else:
        out_path = (
            f"{ready_root}/vendor={vendor}/"
            f"submission={submission_id}/"
            f"{REVIEW_DIR}/etl_review_reports.xlsx"
        )

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        if not health_df.empty:
            health_df.to_excel(writer, sheet_name="Health_Issues", index=False)
        pd.DataFrame([health_summary]).to_excel(writer, sheet_name="Health_Summary", index=False)

        if not autofix_report_df.empty:
            autofix_report_df.to_excel(writer, sheet_name="Autofix_Transformations", index=False)

        pd.DataFrame([autofix_summary]).to_excel(writer, sheet_name="Autofix_Summary", index=False)

        if not integrity_df.empty:
            integrity_df.to_excel(writer, sheet_name="Integrity_Issues", index=False)
        pd.DataFrame([integrity_summary]).to_excel(writer, sheet_name="Integrity_Summary", index=False)

        pd.DataFrame([canonical_summary]).to_excel(writer, sheet_name="Canonical_Summary", index=False)

        if not review_df.empty:
            review_df.to_excel(writer, sheet_name="Business_Review", index=False)

        if not media_canonical_df.empty:
            media_canonical_df.to_excel(
                writer,
                sheet_name="Media_Canonical",
                index=False
            )
   
        meta = {
            "vendor": vendor,
            "generated_at": datetime.utcnow().isoformat(),
        }
        pd.DataFrame([meta]).to_excel(writer, sheet_name="Run_Metadata", index=False)

    # Save
    if local:
        write_local_bytes(out_path, buf.getvalue())
    else:
        upload_blob_bytes(container, out_path, buf.getvalue())

    print(f"✅ Created review report → {out_path}")


# =========================================================
# BUILD PER VENDOR
# =========================================================
def build_etl_mapped_for_vendor(container, vendor: str, submission_id: str, local: bool, mode: str):
    print(f"\n🧩 Building ETL mapped for vendor: {vendor}")

    if local:
        in_autofix = os.path.join(
            local_in_review_vendor(vendor, mode),
            f"submission={submission_id}",
            AUTOFIX_DIR,
            "data_autofixed.parquet"
        )

        out_base = os.path.join(
        local_ready_vendor_submission(vendor, submission_id, mode),
        REVIEW_DIR
    )

        print(f"📁 LOCAL input  = {in_autofix}")
        print(f"📁 LOCAL output = {out_base}")

        df = read_local_parquet(in_autofix)

        integrity_issues = load_optional_parquet(
            None,
            "",
            os.path.join(
                local_in_review_vendor(vendor, mode),
                f"submission={submission_id}",
                INTEGRITY_DIR,
                "integrity_issues.parquet"
            ),
            local=True
        )
        integrity_summary = load_optional_json(
            None,
            "",
           os.path.join(
                local_in_review_vendor(vendor, mode),
                f"submission={submission_id}",
                INTEGRITY_DIR,
            "integrity_summary.json"),
            local=True
        )

    else:
        input_root = PRICING_REVIEW_ROOT if mode == "post_review" else IN_REVIEW_ROOT
        in_autofix = f"{input_root}/vendor={vendor}/submission={submission_id}/{AUTOFIX_DIR}/data_autofixed.parquet"

        ready_root = READY_PRICING_ROOT if mode == "post_review" else READY_ROOT
        out_base = (
            f"{ready_root}/vendor={vendor}/"
            f"submission={submission_id}/"
            f"{REVIEW_DIR}"
        )


        print(f"☁️ AZURE input  = {in_autofix}")
        print(f"☁️ AZURE output = {out_base}")

        df = df_from_parquet_bytes(download_blob_bytes(container, in_autofix))

        integrity_issues = load_optional_parquet(
            container,
            f"{IN_REVIEW_ROOT}/vendor={vendor}/submission={submission_id}/{INTEGRITY_DIR}/integrity_issues.parquet",
            "",
            local=False
        )
        integrity_summary = load_optional_json(
            container,
            f"{IN_REVIEW_ROOT}/vendor={vendor}/submission={submission_id}/{INTEGRITY_DIR}/integrity_summary.json",
            "",
            local=False
        )

    # -----------------------------------------------------
    # Rehydrate tabs
    # -----------------------------------------------------
    tabs = _split_tabs_from_autofixed(df)

    # -----------------------------------------------------
    # Inject row-level hash (baseline)
    # -----------------------------------------------------
    for tab, df_tab in tabs.items():
        if tab in HASH_FIELDS_BY_TAB and not df_tab.empty:
            df_tab["_row_hash_before"] = df_tab.apply(
                lambda r: compute_row_hash(r, HASH_FIELDS_BY_TAB[tab]),
                axis=1
            )

    # -----------------------------------------------------
    # Write canonical parquet snapshot (machine baseline)
    # -----------------------------------------------------
    canonical_df = pd.concat(
        [
            df_tab.assign(__Section=tab_name)
            for tab_name, df_tab in tabs.items()
            if not df_tab.empty
        ],
        ignore_index=True
    )

    parquet_bytes = parquet_bytes_from_df(canonical_df)

    parquet_path = (
        os.path.join(out_base, "etl_mapped.parquet")
        if local else
        f"{out_base}/etl_mapped.parquet"
    )

    if local:
        write_local_bytes(parquet_path, parquet_bytes)
    else:
        upload_blob_bytes(container, parquet_path, parquet_bytes)

    print("📦 etl_mapped.parquet written (canonical snapshot)")


    # -----------------------------------------------------
    # Load previous etl_mapped BEFORE overwrite
    # -----------------------------------------------------

    previous_tabs = load_current_state_snapshot(
        container,
        vendor,
        local
    )

    # -----------------------------------------------------
    # Dataset-level hash
    # -----------------------------------------------------
    current_dataset_hash = compute_dataset_hash(tabs)
   
    has_changes = True

    summary = {
        "vendor": vendor,
        "submission_id": submission_id,
        "generated_at": datetime.utcnow().isoformat(),

        # Row counts
        "row_counts": {k: len(v) for k, v in tabs.items()},

        # Integrity
        "has_integrity_issues": bool(integrity_issues is not None),

        # Baseline metadata (🔥 THIS IS KEY)
        "baseline": {
            "type": "current_state_snapshot",
            "exists": bool(previous_tabs),
        },

        # Change flags
        "has_changes_vs_baseline": has_changes,
    }


    # -----------------------------------------------------
    # Write etl_mapped.xlsx
    # -----------------------------------------------------
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for tab, df_tab in tabs.items():
            # df_tab.to_excel(writer, sheet_name=tab, index=False)
            clean_df = strip_internal_columns(df_tab)
            clean_df.to_excel(writer, sheet_name=tab, index=False)

        # pd.DataFrame([summary]).to_excel(writer, sheet_name="Summary", index=False)

    if local:
        write_local_bytes(os.path.join(out_base, "etl_mapped.xlsx"), buf.getvalue())

    else:
        upload_blob_bytes(container, f"{out_base}/etl_mapped.xlsx", buf.getvalue())

    print("📄 etl_mapped.xlsx written. Now building review reports...")

    # -----------------------------------------------------
    # Delta generation
    # -----------------------------------------------------
    if has_changes:
        print("🔍 Dataset changed since last run → generating delta_mapped.xlsx")
        print("🔎 Delta comparison scope:")
        print(f"  Current tabs  : {sorted(current_tabs := list(tabs.keys()))}")
        print(f"  Baseline tabs : {sorted(previous_tabs.keys())}")


        delta_tabs = build_delta_tabs(tabs, previous_tabs)

        print("\n📊 Delta summary:")
        for tab, df in delta_tabs.items():
                print(f"  {tab}: {len(df)} change(s)")
        if not delta_tabs:
            print("📊 Delta summary: NO ROW-LEVEL DELTAS DETECTED")

        if delta_tabs:
            delta_buf = BytesIO()
            with pd.ExcelWriter(delta_buf, engine="openpyxl") as writer:
                for tab, df_tab in delta_tabs.items():
                    # df_tab.to_excel(writer, sheet_name=tab, index=False)
                    clean_df = strip_internal_columns(df_tab)
                    clean_df.to_excel(writer, sheet_name=tab, index=False)


                # pd.DataFrame([{
                #     "vendor": vendor,
                #     "generated_at": datetime.utcnow().isoformat(),
                #     "delta_tabs": list(delta_tabs.keys()),
                # }]).to_excel(writer, sheet_name="Summary", index=False)

            delta_path = (
                os.path.join(out_base, "delta_mapped.xlsx")
                if local else
                f"{out_base}/delta_mapped.xlsx"
            )

            if local:
                write_local_bytes(delta_path, delta_buf.getvalue())
            else:
                upload_blob_bytes(container, delta_path, delta_buf.getvalue())

            print(f"📄 delta_mapped.xlsx written with {len(delta_tabs)} tabs")
            
            # --------------------------------------------
            # ALSO WRITE MACHINE-READABLE DELTA PARQUET
            # --------------------------------------------
            delta_parquet_df = pd.concat(
                [
                    df_tab.assign(__Section=tab_name)
                    for tab_name, df_tab in delta_tabs.items()
                    if not df_tab.empty
                ],
                ignore_index=True
            )

            delta_parquet_bytes = parquet_bytes_from_df(delta_parquet_df)

            delta_parquet_path = (
                os.path.join(out_base, "delta_mapped.parquet")
                if local else
                f"{out_base}/delta_mapped.parquet"
            )

            if local:
                write_local_bytes(delta_parquet_path, delta_parquet_bytes)
            else:
                print("DELTA DEBUG:", delta_parquet_df["Part Number"].head(5).tolist())
                upload_blob_bytes(container, delta_parquet_path, delta_parquet_bytes)

            print("📦 delta_mapped.parquet written (machine snapshot)")

        else:
            print("ℹ Dataset hash changed but no row-level deltas detected")

    else:
        print(" No dataset-level changes detected → skipping delta_mapped.xlsx")


    # -----------------------------------------------------
    # Generate the new review workbook
    # -----------------------------------------------------
    build_review_reports(container=container, vendor=vendor, submission_id = submission_id, local=local, mode=mode)

    # -----------------------------------------------------
    # Write errors_all.xlsx
    # -----------------------------------------------------
    if integrity_issues is None:
        print("⚠️ No integrity issues file found. Skipping errors_all.xlsx")

    else:
        if integrity_issues.empty:
            print("ℹ️ Integrity issues file exists but has 0 rows. No errors_all.xlsx needed.")
            
        else:
            print(f"📝 Generating errors_all.xlsx with {len(integrity_issues)} issues...")

            err_buf = BytesIO()
            with pd.ExcelWriter(err_buf, engine="openpyxl") as writer:
                integrity_issues.to_excel(writer, sheet_name="Errors", index=False)
                pd.DataFrame([summary]).to_excel(writer, sheet_name="Summary", index=False)

            if local:
                write_local_bytes(os.path.join(out_base, ERRORS_ALL_FILENAME), err_buf.getvalue())
            else:
                upload_blob_bytes(container, f"{out_base}/{ERRORS_ALL_FILENAME}", err_buf.getvalue())


    # -----------------------------------------------------
    # Write JSON summary
    # -----------------------------------------------------
    summary_bytes = json.dumps(summary, indent=2).encode("utf-8")

    if local:
        write_local_bytes(os.path.join(out_base, "etl_run_metadata.json"), summary_bytes)
    else:
        upload_blob_bytes(container, f"{out_base}/etl_run_metadata.json", summary_bytes)


    print(f"✅ Done: {vendor}")

    if mode == "post_review":
        promote_to_category_queue(container, vendor, submission_id, local)

    print(f"   - etl_mapped.xlsx")
    if integrity_issues is not None:
        print(f"   - {ERRORS_ALL_FILENAME}")
    print(f"   - etl_mapped_summary.json")

    # # -----------------------------------------------------
    # # ETL COMPLETION MARKER (Logic App trigger)
    # # -----------------------------------------------------
    # marker_path = (
    #     f"{out_base}/_review_complete_"
    #     f"{datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%S-%fZ')}.done"
    # )
    # marker_payload = json.dumps({
    #     "vendor": vendor,
    #     "completed_at": datetime.utcnow().isoformat(),
    #     "dataset_hash": summary["dataset_hash"],
    # }).encode("utf-8")

    # if local:
    #     write_local_bytes(marker_path, marker_payload)
    # else:
    #     upload_blob_bytes(container, marker_path, marker_payload)

    # print(f"🚩 ETL completion marker written → {marker_path}")


# =========================================================
# External Pipeline Entry Point
# =========================================================

def build_etl_mapped(vendor: str, submission_id: str, source: str = "full") -> None:
    """
    Entry point for orchestrated pipelines (e.g. post-review).

    source:
        "full"      → in_review → ready
        "post_review" → post_pricing_review → ready_pricing_review
    """

    if source == "post_review":
        mode = "post_review"
    else:
        mode = "full"

    container = get_container()

    build_etl_mapped_for_vendor(
        container=container,
        vendor=vendor,
        submission_id=submission_id,
        local=False,
        mode=mode
    )


# =========================================================
# CLI
# =========================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--mode", default="full")

    args = parser.parse_args()

    submission_id = args.submission_id

    container = None if args.local else get_container()
    build_etl_mapped_for_vendor(container, args.vendor, submission_id, local=args.local, mode = args.mode)
