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
# SUBMISSION TYPE
# =========================================================
def parse_submission_type(submission_type: str, workflow_override: str = None):
    derived = "pricing" if "pricing" in submission_type else "product"

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
    return pd.read_parquet(path)


def write_local(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def df_from_bytes(b):
    return pq.read_table(BytesIO(b)).to_pandas()


def df_to_bytes(df):
    buf = BytesIO()
    pq.write_table(pa.Table.from_pandas(df), buf)
    return buf.getvalue()


# =========================================================
# HASH
# =========================================================
def norm(v):
    if pd.isna(v): return ""
    return str(v).strip().upper()


def hash_row(row, fields):
    return hashlib.sha256("|".join([norm(row.get(f)) for f in fields]).encode()).hexdigest()


# =========================================================
# BASELINE
# =========================================================
def load_baseline(container, vendor, local):
    path = (
        os.path.join(PROJECT_ROOT, "silver", "approved", "current_state",
                     f"vendor={vendor}", "etl_mapped.parquet")
        if local else
        f"approved/current_state/vendor={vendor}/etl_mapped.parquet"
    )

    try:
        df = read_parquet_local(path) if local else df_from_bytes(download_blob(container, path))
        return df
    except:
        return pd.DataFrame()


# =========================================================
# DELTA ENGINE
# =========================================================
def compute_delta(curr, base, is_delta_review):

    if base.empty:
        curr["_delta_type"] = "insert"
        return curr

    curr["_hash"] = curr.apply(lambda r: hash_row(r, curr.columns), axis=1)
    base["_hash"] = base.apply(lambda r: hash_row(r, base.columns), axis=1)

    merged = curr.merge(base[["_hash"]], on="_hash", how="left", indicator=True)

    if is_delta_review:
        merged["_delta_type"] = merged["_merge"].map({
            "left_only": "insert",
            "both": None
        })
        return merged[merged["_delta_type"].notna()]

    # full review
    merged["_delta_type"] = merged["_merge"].map({
        "left_only": "insert",
        "both": None
    })

    deletes = base.merge(curr[["_hash"]], on="_hash", how="left", indicator=True)
    deletes = deletes[deletes["_merge"] == "left_only"]
    deletes["_delta_type"] = "delete"

    return pd.concat([merged, deletes])


# =========================================================
# CATEGORY QUEUE
# =========================================================
def publish_category_queue(container, vendor, workflow, submission_type, submission_id, df, local):

    if df.empty:
        return

    df["_review_decision"] = pd.NA
    df["_review_comment"] = pd.NA

    path = (
        os.path.join(PROJECT_ROOT, "silver", CATEGORY_QUEUE_ROOT,
                     f"{workflow}_workflow", f"vendor={vendor}", CATEGORY_ACTIVE_DIR, "queue.parquet")
        if local else
        f"{CATEGORY_QUEUE_ROOT}/{workflow}_workflow/vendor={vendor}/{CATEGORY_ACTIVE_DIR}/queue.parquet"
    )

    data = df_to_bytes(df)

    if local:
        write_local(path, data)
    else:
        upload_blob(container, path, data)

##HELPERS
def split_tabs(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:

    tabs = {}

    # -------------------------------------------------
    # CASE 1: Section column exists
    # -------------------------------------------------
    if "__Section" in df.columns or "_sheet" in df.columns:
        section_col = "__Section" if "__Section" in df.columns else "_sheet"

        for tab, cols in SCHEMA_TABS.items():
            chunk = df[df[section_col] == tab].copy()
            chunk = chunk.drop(columns=[section_col], errors="ignore")

            for c in cols:
                if c not in chunk.columns:
                    chunk[c] = pd.NA

            tabs[tab] = chunk[cols].reset_index(drop=True)

        return tabs

def filter_tabs_by_workflow(tabs: Dict[str, pd.DataFrame], workflow: str) -> Dict[str, pd.DataFrame]:

    if workflow == "product":
        # exclude pricing
        return {k: v for k, v in tabs.items() if k != "Pricing"}

    if workflow == "pricing":
        # only item master + pricing
        return {k: v for k, v in tabs.items() if k in ["Item_Master", "Pricing"]}

    return tabs

    # -------------------------------------------------
    # CASE 2: NO SECTION COLUMN → infer from columns
    # -------------------------------------------------
    print("⚠️ No __Section column → inferring tabs from schema")

    for tab, cols in SCHEMA_TABS.items():
        available_cols = [c for c in cols if c in df.columns]

        if not available_cols:
            tabs[tab] = pd.DataFrame(columns=cols)
            continue

        chunk = df[available_cols].copy()

        # ensure all schema columns exist
        for c in cols:
            if c not in chunk.columns:
                chunk[c] = pd.NA

        tabs[tab] = chunk[cols].dropna(how="all").reset_index(drop=True)

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
            # 🔥 Skip empty pricing tab entirely
            if tab_name == "Pricing":
                continue

            out[tab_name] = pd.DataFrame(columns=schema_cols)
            continue

        # remove internal cols
        drop_cols = [c for c in ["__Section", "_sheet"] if c in chunk.columns]
        chunk = chunk.drop(columns=drop_cols, errors="ignore")

        # -----------------------------------------
        # SPECIAL CASE: PRICING (flexible schema)
        # -----------------------------------------
        if tab_name == "Pricing":
            # keep everything except internal columns
            out[tab_name] = chunk.reset_index(drop=True)
            continue

        # ensure schema
        for c in schema_cols:
            if c not in chunk.columns:
                chunk[c] = pd.NA

        # drop empty rows
        chunk = chunk.dropna(how="all", subset=schema_cols)

        out[tab_name] = chunk[schema_cols].reset_index(drop=True)

    return out
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

    # skip if submission
    if not meta["is_review"]:
        return

    # baseline
    base = load_baseline(container, vendor, local)

    # delta
    delta = compute_delta(df.copy(), base.copy(), meta["is_delta"])

    if delta.empty:
        return

    delta_path = f"{base_out}/delta_mapped.parquet"

    delta_excel_path = f"{base_out}/delta_mapped.xlsx"

    delta_tabs = _split_tabs_from_autofixed(delta)
    delta_tabs = filter_tabs_by_workflow(delta_tabs, workflow)

    delta_buf = BytesIO()
    with pd.ExcelWriter(delta_buf, engine="openpyxl") as writer:
        for tab, df_tab in delta_tabs.items():
            if not df_tab.empty:
                df_tab.to_excel(writer, sheet_name=tab[:31], index=False)

    if local:
        write_local(delta_excel_path, delta_buf.getvalue())
    else:
        upload_blob(container, delta_excel_path, delta_buf.getvalue())

    if local:
        write_local(delta_path, df_to_bytes(delta))
    else:
        upload_blob(container, delta_path, df_to_bytes(delta))

    # queue
    publish_category_queue(
        container,
        vendor,
        workflow,
        submission_type,
        submission_id,
        delta,
        local
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