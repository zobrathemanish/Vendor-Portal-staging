#build_etl_mapped.py
import os
import json
from io import BytesIO
from datetime import datetime
from typing import Dict, List

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from azure.storage.blob import BlobServiceClient
import hashlib
from data_ETL.data_unified_integrity import run_unified_integrity

# =========================================================
# CONFIG
# =========================================================
AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = os.getenv("SILVER_CONTAINER", "silver")

IN_REVIEW_ROOT = "in_review"
PRICING_REVIEW_ROOT = "in_review"

READY_ROOT = "ready"
READY_PRICING_ROOT = "ready"

AUTOFIX_DIR = "autofix"
INTEGRITY_DIR = "integrity"
REVIEW_DIR = "review"

CATEGORY_QUEUE_ROOT = "category_queue"
CATEGORY_ACTIVE_DIR = "active"

ERRORS_ALL_FILENAME = "errors_all.xlsx"

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))

SCHEMA_TABS: Dict[str, List[str]] = {
    "Item_Master": [
        "Brand Label", "Part Number", "UNSPSC", "HazmatFlag",
        "Product Status", "Barcode Type", "Barcode Number",
        "Quantity UOM", "Quantity Size",
        "Minimum Order Quantity UOM", "Minimum Order Quantity",
        "VMRS Code", "PartTerminologyID",
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
         "POP Code", "Effective Date", "Notes",
    ],
}

def get_hash_columns(df):
    EXCLUDE_COLS = {
        "_delta_type", "_merge", "__Section", "_sheet",
        "_entity_key", "_entity_id", "_row_id",
        "_vendor", "_autofix_run_id", "_autofix_timestamp",
        "_transformation_applied",

        # 🔥 CRITICAL FIXES
        "delta_status",
        "_domain",
        "_workflow",
        "_row_hash_before",
        "_row_hash_after",
        "_hash"
    }

    return [c for c in df.columns if c not in EXCLUDE_COLS]

# =========================================================
# SUBMISSION TYPE
# =========================================================
def parse_submission_type(submission_type: str, workflow_override: str = None):
    derived = "pricing" if "pricing" in submission_type else "products"

    return {
        "is_review": "review" in submission_type,
        "is_delta": "delta" in submission_type,
        "workflow": workflow_override if workflow_override else derived
    }

# =========================================================
# PATHS
# =========================================================
def resolve_roots(submission_type, workflow_override):
    meta = parse_submission_type(submission_type, workflow_override)

    if meta["workflow"] == "pricing":
        return PRICING_REVIEW_ROOT, READY_PRICING_ROOT
    return IN_REVIEW_ROOT, READY_ROOT


def build_base_paths(vendor, submission_type, submission_id, local, workflow_override):
    meta = parse_submission_type(submission_type, workflow_override)
    workflow = meta["workflow"]

    in_root, ready_root = resolve_roots(submission_type, workflow_override)

    if local:
        base_in = os.path.join(
            PROJECT_ROOT, "silver", in_root,
            f"{workflow}_workflow",
            f"vendor={vendor}",
            f"submission_type={submission_type}",
            f"submission={submission_id}"
        )

        base_out = os.path.join(
            PROJECT_ROOT, "silver", ready_root,
            f"{workflow}_workflow",
            f"vendor={vendor}",
            f"submission_type={submission_type}",
            f"submission={submission_id}",
            REVIEW_DIR
        )
    else:
        base_in = (
            f"{in_root}/{workflow}_workflow/"
            f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}"
        )

        base_out = (
            f"{ready_root}/{workflow}_workflow/"
            f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/{REVIEW_DIR}"
        )


    return base_in, base_out


# =========================================================
# IO
# =========================================================
def get_container():
    svc = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
    return svc.get_container_client(SILVER_CONTAINER)


def download_blob(container, path):
    return container.get_blob_client(path).download_blob().readall()


def upload_blob(container, path, data):
    container.upload_blob(path, data, overwrite=True)


def read_parquet_local(path):
    df = pd.read_parquet(path)

    if "Part Number" in df.columns:
        df["Part Number"] = df["Part Number"].astype("string").str.strip()

    return df

def write_local(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def df_from_bytes(b):
    df = pq.read_table(BytesIO(b)).to_pandas()

    # 🔥 CRITICAL: enforce string dtype for identifiers
    if "Part Number" in df.columns:
        df["Part Number"] = df["Part Number"].astype("string").str.strip()

    return df


def df_to_bytes(df):
    buf = BytesIO()
    pq.write_table(pa.Table.from_pandas(df), buf)
    return buf.getvalue()

def get_gold_container():
    svc = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
    return svc.get_container_client("gold")

def load_gold_selected(container, workflow, local, vendor):
    path = (
        os.path.join(
            PROJECT_ROOT,
            "gold",
            "selected",
            "unified_workflow",
            f"vendor={vendor}",
            "unified_etl_mapped.xlsx"
        )
        if local else
        f"selected/unified_workflow/vendor={vendor}/unified_etl_mapped.xlsx"
    )

    try:
        if local:
            tabs = pd.read_excel(path, sheet_name=None, dtype=str)
        else:
            blob = container.get_blob_client(path).download_blob().readall()
            tabs = pd.read_excel(BytesIO(blob), sheet_name=None, dtype=str)

        # 🔥 CRITICAL: enforce string + strip
        for tab, df in tabs.items():
            if "Part Number" in df.columns:
                df["Part Number"] = df["Part Number"].astype("string").str.strip()

        return tabs

    except Exception as e:
        print("[GOLD LOAD ERROR]", e)
        return {}


def excel_tabs_to_flat(tabs: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    dfs = []
    for tab, df in tabs.items():
        if df.empty:
            continue
        tmp = df.copy()
        tmp["_sheet"] = tab
        dfs.append(tmp)

    if not dfs:
        return pd.DataFrame()

    return pd.concat(dfs, ignore_index=True)
# =========================================================
# HASH
# =========================================================
def norm(v):

    if pd.isna(v):
        return ""

    # if str(v).startswith("002"):
    #     print("🔥 NORM INPUT:", v, type(v))

    if isinstance(v, str):
        return v.strip()

    if isinstance(v, (int, float)):
        print("❌ NUMERIC DETECTED:", v, type(v))
        return str(v)

    return str(v).strip()


def hash_row(row, fields):
    return hashlib.sha256("|".join([norm(row.get(f)) for f in fields]).encode()).hexdigest()


# =========================================================
# DELTA ENGINE
# =========================================================
def normalize_df(df):
    df = df.copy()

    for col in df.columns:

        # 🔥 DO NOT TOUCH identifiers
        if col == "Part Number":
            df[col] = df[col].astype("string").str.strip()
            continue

        df[col] = df[col].astype(str)
        df[col] = df[col].str.strip()

        df[col] = df[col].replace({
            "nan": None,
            "None": None,
            "": None
        })

    return df

def compute_delta(curr, base, is_delta_review=None, is_delta=None):

    if is_delta is not None:
        is_delta_review = is_delta

    # -----------------------------
    # Ensure section column
    # -----------------------------
    for df in [curr, base]:
        if "__Section" not in df.columns and "_sheet" in df.columns:
            df["__Section"] = df["_sheet"]

    curr = normalize_df(curr)
    base = normalize_df(base)

    if base.empty:
        curr["_delta_type"] = "insert"
        return curr

    # -----------------------------
    # ALIGN COLUMNS
    # -----------------------------
    all_cols = sorted(set(curr.columns) | set(base.columns))

    for col in all_cols:
        if col not in curr.columns:
            curr[col] = pd.NA
        if col not in base.columns:
            base[col] = pd.NA

    curr = curr[all_cols]
    base = base[all_cols]

    # -----------------------------
    # HASHES (for before/after)
    # -----------------------------
    hash_cols = get_hash_columns(curr)

    curr["_hash"] = curr.apply(lambda r: hash_row(r, hash_cols), axis=1)
    base["_hash"] = base.apply(lambda r: hash_row(r, hash_cols), axis=1)

    # -----------------------------
    # 🔥 BUSINESS KEY FUNCTION
    # -----------------------------
    def build_key(row):
        section = row.get("__Section")

        key_map = {
            "Attributes": ["Part Number", "Attribute Name"],
            "Descriptions": ["Part Number", "Description Code", "Sequence"],
            "Digital_Assets": ["Part Number", "FileName"],
            "Extended_Info": ["Part Number", "Extended Info Code"],
            "Item_Master": ["Part Number"],
            "Packages": ["Part Number", "Package UOM"],
            "Pricing": ["Part Number", "Pricing Type", "Currency"],
        }

        keys = key_map.get(section, ["Part Number"])

        return tuple(str(row.get(k)).strip() for k in keys)

    # -----------------------------
    # BUILD MAPS
    # -----------------------------
    curr_map = {build_key(r): r for _, r in curr.iterrows()}
    base_map = {build_key(r): r for _, r in base.iterrows()}

    all_keys = set(curr_map) | set(base_map)

    rows = []

    for k in all_keys:
        c = curr_map.get(k)
        b = base_map.get(k)

        # -------------------------
        # INSERT
        # -------------------------
        if c is not None and b is None:
            row = c.copy()
            row["_delta_type"] = "insert"
            row["_row_hash_after"] = c["_hash"]
            row["_row_hash_before"] = None
            rows.append(row)

        # -------------------------
        # DELETE
        # -------------------------
        elif b is not None and c is None:
            row = b.copy()
            row["_delta_type"] = "delete"
            row["_row_hash_before"] = b["_hash"]
            row["_row_hash_after"] = None
            rows.append(row)

        # -------------------------
        # UPDATE
        # -------------------------
        elif b is not None and c is not None:

            if c["_hash"] != b["_hash"]:
                row = c.copy()
                row["_delta_type"] = "update"
                row["_row_hash_before"] = b["_hash"]
                row["_row_hash_after"] = c["_hash"]
                rows.append(row)

    if not rows:
        return pd.DataFrame()

    delta_df = pd.DataFrame(rows)

    # -----------------------------
    # DELTA REVIEW MODE (INSERT ONLY)
    # -----------------------------
    if is_delta_review:
        return delta_df[delta_df["_delta_type"] == "insert"]

    return delta_df

# =========================================================
# CATEGORY QUEUE (UNIFIED PRODUCT + PRICING)
# =========================================================
def load_approved_workflow_delta(container, vendor, workflow, local) -> pd.DataFrame:


    path = (
        os.path.join(
            PROJECT_ROOT,
            "silver",
            "approved",
            f"{workflow}_workflow",
            f"vendor={vendor}",
            f"{workflow}_delta.parquet"
        )
        if local else
        f"approved/{workflow}_workflow/vendor={vendor}/{workflow}_delta.parquet"
    )

    try:
        if local:
            df = read_parquet_local(path)
        else:
            df = df_from_bytes(download_blob(container, path))

        if df is None or df.empty:
            return pd.DataFrame()

        return df

    except Exception as e:
        print(f"[QUEUE LOAD FAIL] {workflow} delta not found →", e)
        return pd.DataFrame()

def enrich_with_product_context(container, vendor, unified, local):

    if unified.empty:
        return unified

    if "__Section" not in unified.columns and "_sheet" in unified.columns:
        unified["__Section"] = unified["_sheet"]

    # Identify parts missing Item_Master
    missing_parts = []

    for part in unified["Part Number"].dropna().astype(str).unique():
        part_rows = unified[unified["Part Number"].astype(str) == part]

        if "Item_Master" not in part_rows["__Section"].values:
            missing_parts.append(part)

    if not missing_parts:
        return unified

    print(f"[ENRICH] Missing Item_Master for {len(missing_parts)} parts")

    # --------------------------------------------------
    # Load approved baseline (SAFE)
    # --------------------------------------------------
    baseline_path = (
        os.path.join(
            PROJECT_ROOT,
            "silver",
            "approved",
            "products_workflow",
            f"vendor={vendor}",
            "products_etl_mapped.parquet"
        )
        if local else
        f"approved/products_workflow/vendor={vendor}/products_etl_mapped.parquet"
    )

    try:
        if local:
            baseline = read_parquet_local(baseline_path)
        else:
            baseline = df_from_bytes(download_blob(container, baseline_path))

        if baseline is None or baseline.empty:
            print("[ENRICH] Baseline empty → skipping enrichment")
            return unified

    except Exception as e:
        print("[ENRICH] Baseline not available → first run, skipping enrichment")
        return unified

    # --------------------------------------------------
    # Normalize baseline
    # --------------------------------------------------
    if "Part Number" not in baseline.columns:
        print("[ENRICH] Baseline missing Part Number → skipping")
        return unified

    baseline["Part Number"] = baseline["Part Number"].astype("string").str.strip()

    # --------------------------------------------------
    # Extract rows for missing parts
    # --------------------------------------------------
    enrich_rows = baseline[
        baseline["Part Number"].isin(missing_parts)
    ]

    if enrich_rows.empty:
        print("[ENRICH] No matching baseline rows found")
        return unified

    print(f"[ENRICH] Adding {len(enrich_rows)} rows from baseline")

    # --------------------------------------------------
    # Merge
    # --------------------------------------------------
    unified = pd.concat([unified, enrich_rows], ignore_index=True)

    unified = unified.drop_duplicates()

    return unified

def upload_parquet(container, path, df):
    from io import BytesIO
    import pyarrow as pa
    import pyarrow.parquet as pq

    if df is None or df.empty:
        print(f"[UPLOAD] Skipping empty parquet for {path}")
        return

    buf = BytesIO()
    table = pa.Table.from_pandas(df)
    pq.write_table(table, buf)

    buf.seek(0)

    container.upload_blob(name=path, data=buf.getvalue(), overwrite=True)
    print(f"[UPLOAD] Parquet uploaded → {path} ({len(df)} rows)")

def build_unified_category_queue(container, vendor, local):

    print("[QUEUE] Building category queue using APPROVED vs GOLD")

    # =====================================================
    # LOAD APPROVED (NEW STATE)
    # =====================================================
    approved_path = (
        os.path.join(PROJECT_ROOT, "silver", "approved",
                     f"unified_workflow/vendor={vendor}/unified_etl_mapped.parquet")
        if local else
        f"approved/unified_workflow/vendor={vendor}/unified_etl_mapped.parquet"
    )

    try:
        if local:
            df_approved = read_parquet_local(approved_path)
        else:
            df_approved = df_from_bytes(download_blob(container, approved_path))

        print(f"[QUEUE] Approved rows: {len(df_approved)}")

    except:
        print("[QUEUE] No approved unified data found — skipping")
        return

    # =====================================================
    # LOAD GOLD (CURRENT STATE)
    # =====================================================
    gold_path = (
        os.path.join(PROJECT_ROOT, "silver", "selected",
                     f"unified_workflow/vendor={vendor}/unified_etl_mapped.parquet")
        if local else
        f"selected/unified_workflow/vendor={vendor}/unified_etl_mapped.parquet"
    )

    try:
        if local:
            df_gold = read_parquet_local(gold_path)
        else:
            df_gold = df_from_bytes(download_blob(container, gold_path))

        print(f"[QUEUE] Gold rows: {len(df_gold)}")

    except:
        print("[QUEUE] No gold baseline found — treating as full insert")
        df_gold = pd.DataFrame(columns=df_approved.columns)

    # =====================================================
    # ENSURE REQUIRED STRUCTURE
    # =====================================================
    if "__Section" not in df_approved.columns:
        df_approved["__Section"] = df_approved.get("_sheet", "")

    if "__Section" not in df_gold.columns:
        df_gold["__Section"] = df_gold.get("_sheet", "")

    # Add domain/workflow tags
    df_approved["_domain"] = df_approved.get("_domain", "product")
    df_gold["_domain"] = df_gold.get("_domain", "product")

    df_approved["_workflow"] = df_approved.get("_workflow", "products")
    df_gold["_workflow"] = df_gold.get("_workflow", "products")

    # =====================================================
    # COMPUTE DELTA (APPROVED vs GOLD)
    # =====================================================
    unified_delta = compute_delta(df_approved.copy(), df_gold.copy(), is_delta=False)

    if unified_delta.empty:
        print("[QUEUE] No delta rowsz")
        return

    print(f"[QUEUE] Delta rows: {len(unified_delta)}")

    # =====================================================
    # OPTIONAL: ENRICH PRODUCT CONTEXT
    # =====================================================
    unified_delta = enrich_with_product_context(
        container,
        vendor,
        unified_delta,
        local
    )

    # =====================================================
    # FILTER TO VALID SCHEMA TABS
    # =====================================================
    unified_delta = unified_delta[
        unified_delta["__Section"].isin(SCHEMA_TABS)
    ]

    # =====================================================
    # REMOVE DUPLICATES (SAFETY)
    # =====================================================
    unified_delta = unified_delta.drop_duplicates(
        subset=["Part Number", "__Section", "_hash"]
    )

    # =====================================================
    # SAVE OUTPUTS
    # =====================================================
    approved_unified_delta_path = (
        os.path.join(PROJECT_ROOT, "silver", "approved",
                     f"unified_workflow/vendor={vendor}/unified_delta.parquet")
        if local else
        f"approved/unified_workflow/vendor={vendor}/unified_delta.parquet"
    )

    category_queue_path = (
        os.path.join(PROJECT_ROOT, "silver", "category_queue",
                     f"vendor={vendor}/active/delta_mapped.parquet")
        if local else
        f"category_queue/vendor={vendor}/active/delta_mapped.parquet"
    )

    if local:
        os.makedirs(os.path.dirname(approved_unified_delta_path), exist_ok=True)
        os.makedirs(os.path.dirname(category_queue_path), exist_ok=True)

        unified_delta.to_parquet(approved_unified_delta_path, index=False)
        unified_delta.to_parquet(category_queue_path, index=False)

    else:
        upload_parquet(container, approved_unified_delta_path, unified_delta)
        upload_parquet(container, category_queue_path, unified_delta)

    print(f"[QUEUE] Category queue updated with {len(unified_delta)} rows")

def filter_tabs_by_workflow(tabs: Dict[str, pd.DataFrame], workflow: str) -> Dict[str, pd.DataFrame]:

    # -------------------------------------------------
    # CASE 1: Already split correctly
    # -------------------------------------------------
    if tabs:
        if workflow == "products":
            print("[REVIEW] Product workflow → excluding Pricing tab")
            return {k: v for k, v in tabs.items() if k != "Pricing"}

        if workflow == "pricing":
            print("[REVIEW] Pricing workflow → keeping only Pricing tab")
            return {k: v for k, v in tabs.items() if k == "Pricing"}

        return tabs


def _split_tabs_from_autofixed(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:

    if "__Section" in df.columns:
        section_col = "__Section"
    elif "_sheet" in df.columns:
        section_col = "_sheet"
    else:
        raise RuntimeError(
            "data_autofixed.parquet must contain '__Section' or '_sheet'"
        )

    out: Dict[str, pd.DataFrame] = {}

    for tab_name, schema_cols in SCHEMA_TABS.items():
        chunk = df[df[section_col] == tab_name].copy()

        if chunk.empty:
            if tab_name == "Pricing":
                continue
            out[tab_name] = pd.DataFrame(columns=schema_cols)
            continue

        drop_cols = [c for c in ["__Section", "_sheet"] if c in chunk.columns]
        chunk = chunk.drop(columns=drop_cols, errors="ignore")

        extra_cols = []
        if "_delta_type" in chunk.columns:
            chunk["Delta Type"] = chunk["_delta_type"]
            extra_cols.append("Delta Type")

        if tab_name == "Pricing":
            pricing_cols = SCHEMA_TABS["Pricing"]

            keep_cols = [
                c for c in chunk.columns
                if c in pricing_cols or c in ["_delta_type"]
            ]

            out[tab_name] = chunk[keep_cols]

        for c in schema_cols:
            if c not in chunk.columns:
                chunk[c] = pd.NA

        chunk = chunk.dropna(how="all", subset=schema_cols)

        out[tab_name] = chunk[schema_cols + extra_cols].reset_index(drop=True)

    return out

def build_unified_approved_state(container, vendor, local):

    def load(path):
        try:
            if local:
                return read_parquet_local(path)
            else:
                return df_from_bytes(download_blob(container, path))
        except:
            return pd.DataFrame()

    base_path = (
        os.path.join(PROJECT_ROOT, "silver", "approved")
        if local else "approved"
    )

    products = load(f"{base_path}/products_workflow/vendor={vendor}/products_etl_mapped.parquet")
    pricing  = load(f"{base_path}/pricing_workflow/vendor={vendor}/pricing_etl_mapped.parquet")

    frames = [df for df in [products, pricing] if not df.empty]

    if not frames:
        print("[UNIFIED] No data to merge")
        return

    unified = pd.concat(frames, ignore_index=True)

    # REQUIRED
    unified = enrich_with_product_context(
        container,
        vendor,
        unified,
        local
    )

    # ENSURE SECTION
    if "__Section" not in unified.columns and "_sheet" in unified.columns:
        unified["__Section"] = unified["_sheet"]

    # CLEAN
    unified = unified.drop_duplicates()

    if "Part Number" in unified.columns:
        unified["Part Number"] = unified["Part Number"].astype("string").str.strip()

    
    # --------------------------------------------------
    # 🔥 ADD HASH (CRITICAL FOR UPDATE MATCHING)
    # --------------------------------------------------
    hash_cols = get_hash_columns(unified)

    unified["_hash"] = unified.apply(
        lambda r: hash_row(r, hash_cols),
        axis=1
    )

    print(unified[["_hash", "Part Number"]].head())

    # --------------------------
    # SAVE PATHS
    # --------------------------
    out_parquet = f"{base_path}/unified_workflow/vendor={vendor}/unified_etl_mapped.parquet"
    out_excel   = f"{base_path}/unified_workflow/vendor={vendor}/unified_etl_mapped.xlsx"

    queue_parquet = (
        os.path.join(
            PROJECT_ROOT,
            "silver",
            "category_queue",
            f"vendor={vendor}",
            "active",
            "unified_etl_mapped.parquet"
        )
        if local else
        f"category_queue/vendor={vendor}/active/unified_etl_mapped.parquet"
    )

    queue_excel = (
        os.path.join(
            PROJECT_ROOT,
            "silver",
            "category_queue",
            f"vendor={vendor}",
            "active",
            "unified_etl_mapped.xlsx"
        )
        if local else
        f"category_queue/vendor={vendor}/active/unified_etl_mapped.xlsx"
    )

    # --------------------------
    # SAVE PARQUET
    # --------------------------
    data = df_to_bytes(unified)

    if local:
        write_local(out_parquet, data)
        write_local(queue_parquet, data)
    else:
        upload_blob(container, out_parquet, data)
        upload_blob(container, queue_parquet, data)

    print("[UNIFIED] Approved unified_etl_mapped parquet saved")
    print("[QUEUE] unified_etl_mapped parquet synced")

    # --------------------------
    # SAVE EXCEL
    # --------------------------
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for section, df_sec in unified.groupby("__Section"):
            if df_sec.empty:
                continue
            df_sec.to_excel(writer, sheet_name=section[:31], index=False)

    excel_bytes = buf.getvalue()

    if local:
        write_local(out_excel, excel_bytes)
        write_local(queue_excel, excel_bytes)
    else:
        upload_blob(container, out_excel, excel_bytes)
        upload_blob(container, queue_excel, excel_bytes)

    print("[UNIFIED] Approved unified_etl_mapped excel saved")
    print("[QUEUE] unified_etl_mapped excel synced")

    
# =========================================================
# MAIN
# =========================================================
def build_etl_mapped_for_vendor(container, vendor, submission_type, submission_id, local, workflow_override):

    meta = parse_submission_type(submission_type, workflow_override)
    workflow = meta["workflow"]

    base_in, base_out = build_base_paths(vendor, submission_type, submission_id, local, workflow_override)

    # load
    path = f"{base_in}/{AUTOFIX_DIR}/data_autofixed.parquet"
    df = read_parquet_local(path) if local else df_from_bytes(download_blob(container, path))

    # --------------------------------------------------
    # FORCE STRING TYPES (CRITICAL)
    # --------------------------------------------------
    STRING_COLS = [
        "Part Number",
        "Barcode Number",
        "VMRS Code",
        "PartTerminologyID"
    ]

    for col in STRING_COLS:
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip()
    
    print("\n[DEBUG AFTER STRING CAST]")
    print(df["Part Number"].head(10))
    print(df["Part Number"].dtype)

    # write mapped
    # -------------------------------------------------
    # SPLIT INTO TABS 
    # -------------------------------------------------
    tabs = _split_tabs_from_autofixed(df)
    tabs = filter_tabs_by_workflow(tabs, workflow)

    # -------------------------------------------------
    # WRITE PARQUET (flattened for system)
    # -------------------------------------------------
    flat_df = pd.concat(
        [t.assign(__Section=name) for name, t in tabs.items()],
        ignore_index=True
    )
    # --- remove system columns ---
    SYSTEM_COLS = [
        "_entity_key",
        "_entity_id",
        "_vendor",
        "_row_id",
        "_autofix_run_id",
        "_autofix_timestamp",
        "_transformation_applied"
    ]

    flat_df = flat_df.drop(columns=[c for c in SYSTEM_COLS if c in flat_df.columns], errors="ignore")

    out_path = f"{base_out}/etl_mapped.parquet"
    data = df_to_bytes(flat_df)

    if local:
        write_local(out_path, data)
    else:
        upload_blob(container, out_path, data)

    # -------------------------------------------------
    # WRITE EXCEL (multi-tab for users)
    # -------------------------------------------------
    excel_path = f"{base_out}/etl_mapped.xlsx"

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for tab, df_tab in tabs.items():
            df_tab.to_excel(writer, sheet_name=tab[:31], index=False)

    if local:
        write_local(excel_path, buf.getvalue())
    else:
        upload_blob(container, excel_path, buf.getvalue())

        # -------------------------------------------------
        # DELTA GENERATION (for BOTH submission + review)
        # -------------------------------------------------
        print("[DELTA] Using GOLD baseline")

        
    # -------------------------------------------------
    # 🔥 LOAD UNIFIED GOLD BASELINE (NEW)
    # -------------------------------------------------

    gold_path = (
        os.path.join(
            PROJECT_ROOT,
            "gold",
            "selected",
            "unified_workflow",
            f"vendor={vendor}",
            "unified_etl_mapped.parquet"
        )
        if local else
        f"selected/unified_workflow/vendor={vendor}/unified_etl_mapped.parquet"
    )

    try:
        if local:
            gold_flat = read_parquet_local(gold_path)
        else:
            gold_flat = df_from_bytes(download_blob(get_gold_container(), gold_path))

        print("✅ GOLD UNIFIED LOADED:", gold_path)
        print("🔍 GOLD SHAPE:", gold_flat.shape)

    except Exception as e:
        print("❌ GOLD UNIFIED NOT FOUND:", e)
        gold_flat = pd.DataFrame()

    # align section column
    def align(df):
        df = df.copy()
        if "__Section" in df.columns:
            df["_sheet"] = df["__Section"]
            # df.drop(columns=["__Section"], inplace=True)
        return df

    curr_flat = align(flat_df)
    gold_flat = align(gold_flat)

    # print("\n========== CURRENT (PRE-DELTA) CHECK ==========")

    # curr_check = curr_flat[
    #     (curr_flat["Part Number"] == "00211") &
    #     (curr_flat["__Section"] == "Extended_Info") &
    #     (curr_flat["Extended Info Code"] == "LIF")
    # ]

    # print(curr_check[[
    #     "Part Number",
    #     "Extended Info Code",
    #     "Extended Info Value"
    # ]])
    delta = compute_delta(curr_flat.copy(), gold_flat.copy(), meta["is_delta"])
    # print("\n[DEBUG AFTER DELTA]")
    # print(delta["Part Number"].head(20))
    # print(delta["Part Number"].dtype)

    # 🔥 CRITICAL FIX: enforce workflow isolation

    if workflow == "products":
        delta = delta[
            delta["__Section"] != "Pricing"
        ]

    if workflow == "pricing":
        delta = delta[
            delta["__Section"] == "Pricing"
    ]

    if delta.empty:
        print("[DELTA] No changes detected")

        if not meta["is_review"]:
            return
    else:
        delta_path = f"{base_out}/delta_mapped.parquet"
        delta_excel_path = f"{base_out}/delta_mapped.xlsx"

        delta_tabs = _split_tabs_from_autofixed(delta)
        delta_tabs = filter_tabs_by_workflow(delta_tabs, workflow)

        delta_buf = BytesIO()
        if not delta_tabs or all(df.empty for df in delta_tabs.values()):
            print("[DELTA] No data to write → skipping Excel generation")

            # OPTIONAL: write empty placeholder
            delta_tabs = {
                "No_Data": pd.DataFrame({"message": ["No delta rows found"]})
            }

        with pd.ExcelWriter(delta_buf, engine="openpyxl") as writer:
            written = False
            for sheet, df in delta_tabs.items():
                if df is None or df.empty:
                    continue

                df.to_excel(writer, sheet_name=sheet[:31], index=False)
                written = True

            if not written:
                pd.DataFrame({"message": ["No delta rows"]}).to_excel(
                    writer,
                    sheet_name="No_Data",
                    index=False
                )

        if local:
            write_local(delta_excel_path, delta_buf.getvalue())
            write_local(delta_path, df_to_bytes(delta))
        else:
            upload_blob(container, delta_excel_path, delta_buf.getvalue())
            delta = normalize_df(delta.copy())
            upload_blob(container, delta_path, df_to_bytes(delta))

    # -------------------------------------------------
    # ONLY REVIEW → queue + approved write
    # -------------------------------------------------
    if not meta["is_review"]:
        return

    delta_path = f"{base_out}/delta_mapped.parquet"

    delta_excel_path = f"{base_out}/delta_mapped.xlsx"

    # delta = delta.drop(columns=["_hash"], errors="ignore")

    delta_tabs = _split_tabs_from_autofixed(delta)
    delta_tabs = filter_tabs_by_workflow(delta_tabs, workflow)

    delta_buf = BytesIO()

    if not delta_tabs or all(df.empty for df in delta_tabs.values()):
        print("[DELTA] No data to write → skipping Excel generation")

        # OPTIONAL: write empty placeholder
        delta_tabs = {
            "No_Data": pd.DataFrame({"message": ["No delta rows found"]})
        }

    with pd.ExcelWriter(delta_buf, engine="openpyxl") as writer:
            written = False

            for sheet, df in delta_tabs.items():
                if df is None or df.empty:
                    continue

                df.to_excel(writer, sheet_name=sheet[:31], index=False)
                written = True

            if not written:
                pd.DataFrame({"message": ["No delta rows"]}).to_excel(
                    writer,
                    sheet_name="No_Data",
                    index=False
                )

    if local:
        write_local(delta_excel_path, delta_buf.getvalue())
    else:
        upload_blob(container, delta_excel_path, delta_buf.getvalue())

    if local:
        write_local(delta_path, df_to_bytes(delta))
    else:
        upload_blob(container, delta_path, df_to_bytes(delta))

    # =========================================================
    # GOLD SELECTED DELTA (FULL vs DELTA REVIEW)
    # =========================================================
    if meta["is_review"] and workflow == "products":

        print("[GOLD DELTA] Processing vs selected")

        gold_container = get_gold_container() if not local else None

        gold_tabs = load_gold_selected(gold_container, workflow, local, vendor)

        if gold_tabs:

            gold_flat = excel_tabs_to_flat(gold_tabs)

            # Align section columns
            def align(df):

                df = df.copy()
                if "__Section" in df.columns:
                    df["_sheet"] = df["__Section"]
                    # df.drop(columns=["__Section"], inplace=True)
                return df

            curr_flat = align(flat_df)
            gold_flat = align(gold_flat)

            # -------------------------------
            # FULL PRODUCT REVIEW
            # -------------------------------
            if not meta["is_delta"]:
                print("[GOLD DELTA] FULL → compute delta")

                gold_delta = compute_delta(
                    curr_flat.copy(),
                    gold_flat.copy(),
                    is_delta_review=False
                )

            # -------------------------------
            # DELTA PRODUCT REVIEW
            # -------------------------------
            else:
                print("[GOLD DELTA] DELTA → validate")

                expected_delta = compute_delta(
                    curr_flat.copy(),
                    gold_flat.copy(),
                    is_delta_review=False
                )

                curr_hash = set(curr_flat.get("_hash", pd.Series()).dropna())
                expected_hash = set(expected_delta.get("_hash", pd.Series()).dropna())

                missing = expected_hash - curr_hash
                extra = curr_hash - expected_hash

                print(f"[GOLD DELTA CHECK] Missing={len(missing)}, Extra={len(extra)}")

                # still pass vendor delta forward
                gold_delta = curr_flat.copy()

            # -------------------------------
            # SAVE
            # -------------------------------

                gold_delta_tabs = _split_tabs_from_autofixed(gold_delta)
                gold_delta_tabs = filter_tabs_by_workflow(gold_delta_tabs, workflow)

                buf_gold = BytesIO()
                with pd.ExcelWriter(buf_gold, engine="openpyxl") as writer:
                    written = False

                    for tab, df_tab in gold_delta_tabs.items():
                        if not df_tab.empty:
                            df_tab.to_excel(writer, sheet_name=tab[:31], index=False)
                            written = True

                    if not written:
                        pd.DataFrame({"info": ["No delta vs gold"]}).to_excel(
                            writer, sheet_name="Summary", index=False
                        )

                gold_out_path = (
                    os.path.join(
                        PROJECT_ROOT,
                        "silver",
                        "approved",
                        f"{workflow}_workflow",
                        f"delta_{workflow}_etl_mapped.xlsx"
                    )
                    if local else
                    f"approved/{workflow}_workflow/delta_{workflow}_etl_mapped.xlsx"
                )

                if local:
                    write_local(gold_out_path, buf_gold.getvalue())
                else:
                    container.upload_blob(gold_out_path, buf_gold.getvalue(), overwrite=True)

                print("[GOLD DELTA] Saved")
    # =========================================================
    # SILVER APPROVED WRITE (MINIMAL STRATEGY)
    # =========================================================
    if meta["is_review"]:

        approved_container = container

        approved_parquet_path = (
            f"approved/{workflow}_workflow/"
            f"vendor={vendor}/"
            f"{workflow}_etl_mapped.parquet"
        )

        approved_excel_path = (
            f"approved/{workflow}_workflow/"
            f"vendor={vendor}/"
            f"{workflow}_etl_mapped.xlsx"
        )

        # =========================================================
        # SAVE PRODUCT DELTA TO APPROVED
        # =========================================================
        approved_delta_path = (
            f"approved/{workflow}_workflow/vendor={vendor}/{workflow}_delta.parquet"
        )

        approved_delta_excel_path = (
            f"approved/{workflow}_workflow/vendor={vendor}/{workflow}_delta.xlsx"
        )

        approved_delta_bytes = df_to_bytes(delta)

        approved_delta_excel_bytes = delta_buf.getvalue()

        if local:
            write_local(
                os.path.join(
                    PROJECT_ROOT,
                    "silver",
                    "approved",
                    f"{workflow}_workflow",
                    f"vendor={vendor}",
                    f"{workflow}_delta.parquet"
                ),
                approved_delta_bytes
            )

            write_local(
                os.path.join(
                    PROJECT_ROOT,
                    "silver",
                    "approved",
                    f"{workflow}_workflow",
                    f"vendor={vendor}",
                    f"{workflow}_delta.xlsx"
                ),
                approved_delta_excel_bytes
            )
        else:
            approved_container.upload_blob(
                approved_delta_path,
                approved_delta_bytes,
                overwrite=True
            )

            # ✅ ADD THIS
            approved_container.upload_blob(
                approved_delta_excel_path,
                approved_delta_excel_bytes,
                overwrite=True
            )

        print("[APPROVED] Product delta saved")

        # -----------------------------------------
        # FULL REVIEW → overwrite
        # -----------------------------------------
        if not meta["is_delta"]:

            print(f"[APPROVED] Full review → overwrite {workflow} for {vendor}")

            parquet_data = df_to_bytes(flat_df)

            if local:
                ready_excel_bytes = buf.getvalue()
                write_local(approved_parquet_path, parquet_data)
                write_local(approved_excel_path, ready_excel_bytes)
            else:
                ready_excel_bytes = buf.getvalue()
                approved_container.upload_blob(approved_parquet_path, parquet_data, overwrite=True)
                approved_container.upload_blob(approved_excel_path, ready_excel_bytes, overwrite=True)

        # -----------------------------------------
        # DELTA REVIEW → merge
        # -----------------------------------------
        else:

            print(f"[APPROVED] Delta review → merge {workflow} for {vendor}")

            try:
                existing_bytes = approved_container.get_blob_client(approved_parquet_path).download_blob().readall()
                existing_df = df_from_bytes(existing_bytes)
            except:
                existing_df = pd.DataFrame()

            if existing_df.empty:
                merged_df = flat_df.copy()
            else:
                merged_df = pd.concat([existing_df, flat_df], ignore_index=True)

                merged_df = merged_df.copy()
                for col in merged_df.columns:
                    if merged_df[col].dtype == "object":
                        merged_df[col] = merged_df[col].astype(str).str.strip()

                merged_df = merged_df.drop_duplicates()

            parquet_data = df_to_bytes(merged_df)

            merged_tabs = _split_tabs_from_autofixed(merged_df)
            merged_tabs = filter_tabs_by_workflow(merged_tabs, workflow)

            merged_excel_buf = BytesIO()
            with pd.ExcelWriter(merged_excel_buf, engine="openpyxl") as writer:
                for tab, df_tab in merged_tabs.items():
                    df_tab.to_excel(writer, sheet_name=tab[:31], index=False)

            merged_excel_bytes = merged_excel_buf.getvalue()

            if local:
                write_local(approved_parquet_path, parquet_data)
                write_local(approved_excel_path, merged_excel_bytes)
            else:
                approved_container.upload_blob(approved_parquet_path, parquet_data, overwrite=True)
                approved_container.upload_blob(approved_excel_path, merged_excel_bytes, overwrite=True)

        # -------------------------------------------------
        # REBUILD UNIFIED CATEGORY QUEUE FROM APPROVED DELTAS
        # -------------------------------------------------
        build_unified_approved_state(
            container=approved_container,
            vendor=vendor,
            local=local
        )


        can_publish = run_unified_integrity(container, vendor)

        if not can_publish:
            print("Blocking issues found — skipping category queue build")
            return

        build_unified_category_queue(
            container=approved_container,
            vendor=vendor,
            local=local
        )

       

# =========================================================
# CLI
# =========================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-type", required=True)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--workflow", required=False, choices=["products", "pricing"])

    args = parser.parse_args()

    container = None if args.local else get_container()

    build_etl_mapped_for_vendor(
        container=container,
        vendor=args.vendor,
        submission_type=args.submission_type,
        submission_id=args.submission_id,
        local=args.local,
        workflow_override=args.workflow
    )