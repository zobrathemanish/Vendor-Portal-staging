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

def get_gold_container():
    svc = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
    return svc.get_container_client("gold")

def load_gold_selected(container, workflow, local,vendor):
    path = (
        os.path.join(
            PROJECT_ROOT,
            "gold",
            "selected",
            f"{workflow}_workflow",
            f"{workflow}_etl_mapped.xlsx"
        )
        if local else
        f"selected/{workflow}_workflow/vendor={vendor}/{workflow}_etl_mapped.xlsx"
    )

    try:
        if local:
            return pd.read_excel(path, sheet_name=None)
        else:
            blob = container.get_blob_client(path).download_blob().readall()
            return pd.read_excel(BytesIO(blob), sheet_name=None)
    except:
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

    # handle numeric values properly
    if isinstance(v, (int, float)):
        # if float but whole number → convert to int
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)

    s = str(v).strip()

    # normalize numeric-like strings
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
        return str(f)
    except:
        return s.upper()


def hash_row(row, fields):
    return hashlib.sha256("|".join([norm(row.get(f)) for f in fields]).encode()).hexdigest()


# =========================================================
# BASELINE
# =========================================================
def load_baseline(container, vendor, workflow, local):
    path = (
        os.path.join(
            PROJECT_ROOT,
            "silver",
            "approved",
            f"{workflow}_workflow",
            f"{workflow}_etl_mapped.parquet"
        )
        if local else
        f"approved/{workflow}_workflow/{workflow}_etl_mapped.parquet"
    )

    try:
        df = read_parquet_local(path) if local else df_from_bytes(download_blob(container, path))
        return df
    except:
        return pd.DataFrame()


# =========================================================
# DELTA ENGINE
# =========================================================
def normalize_df(df):
    df = df.copy()

    for col in df.columns:

        # Force everything to string safely
        df[col] = df[col].astype(str)

        # Clean values
        df[col] = df[col].str.strip()

        # Normalize null-like values
        df[col] = df[col].replace({
            "nan": None,
            "None": None,
            "": None
        })

    return df

def compute_delta(curr, base, is_delta_review):

    for df in [curr, base]:
        if "__Section" not in df.columns and "_sheet" in df.columns:
            df["__Section"] = df["_sheet"]

    curr = normalize_df(curr)
    base = normalize_df(base)

    if base.empty:
        curr["_delta_type"] = "insert"
        return curr

    # Align columns between current and baseline
    all_cols = sorted(set(curr.columns) | set(base.columns))

    for col in all_cols:
        if col not in curr.columns:
            curr[col] = pd.NA
        if col not in base.columns:
            base[col] = pd.NA

    curr = curr[all_cols]
    base = base[all_cols]

    for df in [curr, base]:
        if "__Section" in df.columns:
            df["_sheet"] = df["__Section"]
            # df.drop(columns=["__Section"], inplace=True)

    # Exclude technical delta fields from hashing
    EXCLUDE_COLS = {
        "_delta_type", "_merge", "__Section", "_sheet",
        "_entity_key", "_entity_id", "_row_id",
        "_vendor", "_autofix_run_id", "_autofix_timestamp",
        "_transformation_applied"
    }

    hash_cols = [c for c in all_cols if c not in EXCLUDE_COLS]

    curr["_hash"] = curr.apply(lambda r: hash_row(r, hash_cols), axis=1)
    base["_hash"] = base.apply(lambda r: hash_row(r, hash_cols), axis=1)

    # print("\n---- HASH INPUT SAMPLE ----")
    # print(curr[hash_cols].head(3).T)
    # print(base[hash_cols].head(3).T)

    merged = curr.merge(base[["_hash"]], on="_hash", how="left", indicator=True)

    if is_delta_review:
        merged["_delta_type"] = merged["_merge"].map({
            "left_only": "insert",
            "both": None
        })
        return merged[merged["_delta_type"].notna()].drop(columns=["_merge"], errors="ignore")

    # full submission/review: inserts + deletes relative to approved
    merged["_delta_type"] = merged["_merge"].map({
        "left_only": "insert",
        "both": None
    })
    inserts = merged[merged["_delta_type"].notna()].drop(columns=["_merge"], errors="ignore")

    deletes = base.merge(curr[["_hash"]], on="_hash", how="left", indicator=True)
    deletes = deletes[deletes["_merge"] == "left_only"].copy()
    deletes["_delta_type"] = "delete"
    deletes = deletes.drop(columns=["_merge"], errors="ignore")

    delta_df = pd.concat([inserts, deletes], ignore_index=True)

    # ----------------------------------------
    # Convert insert+delete pairs → update
    # ----------------------------------------
    if not delta_df.empty and "Part Number" in delta_df.columns:

        key_cols = ["Part Number", "_sheet"]

        inserts_df = delta_df[delta_df["_delta_type"] == "insert"]
        deletes_df = delta_df[delta_df["_delta_type"] == "delete"]

        common_keys = pd.merge(
            inserts_df[key_cols],
            deletes_df[key_cols],
            on=key_cols
        )

        if not common_keys.empty:
            common_keys["_marker"] = 1

            delta_df = delta_df.merge(common_keys, on=key_cols, how="left")

            # insert → update
            delta_df.loc[
                (delta_df["_delta_type"] == "insert") & (delta_df["_marker"] == 1),
                "_delta_type"
            ] = "update"

            # remove corresponding deletes
            delta_df = delta_df[
                ~(
                    (delta_df["_delta_type"] == "delete") &
                    (delta_df["_marker"] == 1)
                )
            ]

            delta_df = delta_df.drop(columns=["_marker"], errors="ignore")

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
            return read_parquet_local(path)
        return df_from_bytes(download_blob(container, path))
    except:
        return pd.DataFrame()


def build_unified_category_queue(container, vendor, local):
    product_delta = load_approved_workflow_delta(container, vendor, "products", local)
    pricing_delta = load_approved_workflow_delta(container, vendor, "pricing", local)

    if not product_delta.empty:
        product_delta = product_delta.copy()
        product_delta["_domain"] = "product"

    if not pricing_delta.empty:
        pricing_delta = pricing_delta.copy()
        pricing_delta["_domain"] = "pricing"

    frames = [df for df in [product_delta, pricing_delta] if not df.empty]

    if not frames:
        print("[QUEUE] No approved deltas found for products or pricing")
        return

    unified_delta = pd.concat(frames, ignore_index=True)

    unified_delta["_review_decision"] = pd.NA
    unified_delta["_review_comment"] = pd.NA

    path = (
        os.path.join(
            PROJECT_ROOT,
            "silver",
            CATEGORY_QUEUE_ROOT,
            f"vendor={vendor}",
            CATEGORY_ACTIVE_DIR,
            "delta_mapped.parquet"
        )
        if local else
        f"{CATEGORY_QUEUE_ROOT}/vendor={vendor}/{CATEGORY_ACTIVE_DIR}/delta_mapped.parquet"
    )

    if "__Section" not in unified_delta.columns and "_sheet" in unified_delta.columns:
        unified_delta["__Section"] = unified_delta["_sheet"]

    data = df_to_bytes(unified_delta)

    if local:
        write_local(path, data)
    else:
        upload_blob(container, path, data)
    
    # =========================================================
    # SAVE EXCEL (NEW)
    # =========================================================
    excel_path = path.replace(".parquet", ".xlsx")

    # ensure __Section exists
    if "__Section" not in unified_delta.columns and "_sheet" in unified_delta.columns:
        unified_delta["__Section"] = unified_delta["_sheet"]

    buf = BytesIO()

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:

        written = False

        for section, df_sec in unified_delta.groupby("__Section"):

            if df_sec.empty:
                continue

            df_sec.to_excel(writer, sheet_name=section[:31], index=False)
            written = True

        # fallback if empty
        if not written:
            pd.DataFrame({"info": ["No delta changes"]}).to_excel(
                writer, sheet_name="Summary", index=False
            )

    if local:
        write_local(excel_path, buf.getvalue())
    else:
        upload_blob(container, excel_path, buf.getvalue())

    print(f"[QUEUE] Excel written → {excel_path}")

    print(f"[QUEUE] Unified queue written for vendor={vendor} rows={len(unified_delta)}")


# # =========================================================
# # CATEGORY QUEUE
# # =========================================================
# def publish_category_queue(container, vendor, workflow, submission_type, submission_id, df, local):

#     if df.empty:
#         return

#     df["_review_decision"] = pd.NA
#     df["_review_comment"] = pd.NA

#     path = (
#         os.path.join(PROJECT_ROOT, "silver", CATEGORY_QUEUE_ROOT,
#                      f"{workflow}_workflow", f"vendor={vendor}", CATEGORY_ACTIVE_DIR, "queue.parquet")
#         if local else
#         f"{CATEGORY_QUEUE_ROOT}/{workflow}_workflow/vendor={vendor}/{CATEGORY_ACTIVE_DIR}/queue.parquet"
#     )

#     data = df_to_bytes(df)

#     if local:
#         write_local(path, data)
#     else:
#         upload_blob(container, path, data)

# ##HELPERS
# def split_tabs(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:

#     tabs = {}

#     # -------------------------------------------------
#     # CASE 1: Section column exists
#     # -------------------------------------------------
#     if "__Section" in df.columns or "_sheet" in df.columns:
#         section_col = "__Section" if "__Section" in df.columns else "_sheet"

#         for tab, cols in SCHEMA_TABS.items():
#             chunk = df[df[section_col] == tab].copy()
#             chunk = chunk.drop(columns=[section_col], errors="ignore")

#             for c in cols:
#                 if c not in chunk.columns:
#                     chunk[c] = pd.NA

#             tabs[tab] = chunk[cols].reset_index(drop=True)

#         return tabs

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

    gold_container = get_gold_container() if not local else None
    gold_tabs = load_gold_selected(gold_container, workflow, local, vendor)

    gold_flat = excel_tabs_to_flat(gold_tabs) if gold_tabs else pd.DataFrame()

    # align section column
    def align(df):
        df = df.copy()
        if "__Section" in df.columns:
            df["_sheet"] = df["__Section"]
            # df.drop(columns=["__Section"], inplace=True)
        return df

    curr_flat = align(flat_df)
    gold_flat = align(gold_flat)

    delta = compute_delta(curr_flat.copy(), gold_flat.copy(), meta["is_delta"])

    delta = compute_delta(curr_flat.copy(), gold_flat.copy(), meta["is_delta"])

    # 🔍 DEBUG HERE
    print("\n===== DEBUG DELTA FOR 00211 =====")
    print(delta[delta["Part Number"] == "00211"].T)
    print("=================================\n")

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
        with pd.ExcelWriter(delta_buf, engine="openpyxl") as writer:
            for tab, df_tab in delta_tabs.items():
                if not df_tab.empty:
                    df_tab.to_excel(writer, sheet_name=tab[:31], index=False)

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

    delta = delta.drop(columns=["_hash"], errors="ignore")

    delta_tabs = _split_tabs_from_autofixed(delta)
    delta_tabs = filter_tabs_by_workflow(delta_tabs, workflow)

    delta_buf = BytesIO()
    with pd.ExcelWriter(delta_buf, engine="openpyxl") as writer:
                    written = False

                    for tab, df_tab in delta_tabs.items():
                        if not df_tab.empty:
                            df_tab.to_excel(writer, sheet_name=tab[:31], index=False)
                            written = True

                    if not written:
                        pd.DataFrame({"info": ["No delta changes"]}).to_excel(
                            writer, sheet_name="Summary", index=False
                         )

    if local:
        write_local(delta_excel_path, delta_buf.getvalue())
    else:
        upload_blob(container, delta_excel_path, delta_buf.getvalue())

    if local:
        write_local(delta_path, df_to_bytes(delta))
    else:
        upload_blob(container, delta_path, df_to_bytes(delta))

    # if meta["is_review"]:

    #     print("[QUEUE] Publishing reviewed delta to category queue")

    #     # ------------------------------------------
    #     # Read already generated delta from ready
    #     # ------------------------------------------
    #     delta_path_ready = (
    #         f"{READY_ROOT}/{workflow}_workflow/vendor={vendor}/"
    #         f"submission_type={submission_type}/submission={submission_id}/review/delta_mapped.parquet"
    #     )

    #     print("[QUEUE LOAD PATH]", delta_path_ready)

    #     try:
    #         delta_bytes = container.get_blob_client(delta_path_ready).download_blob().readall()
    #         delta_df = pq.read_table(BytesIO(delta_bytes)).to_pandas()
    #         print("[QUEUE LOAD SIZE]", len(delta_bytes))
    #     except Exception as e:
    #         print("[QUEUE] Failed to load delta:", e)
    #         delta_df = pd.DataFrame()

    #     # ------------------------------------------
    #     # Publish to category queue
    #     # ------------------------------------------
    #     publish_category_queue(
    #         container,
    #         vendor,
    #         workflow,
    #         submission_type,
    #         submission_id,
    #         delta_df,
    #         local
    #     )

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