"""
data_autofix.py  
Autofix Stage (Final Version)
---------------------------------------------------------

Fixes performed:
✓ Preserves canonical Part Number via _entity_key
✓ Inserts _entity_id (hash stable across runs)
✓ Adds _autofix_run_id + _autofix_timestamp
✓ UOM conversions based on YAML (KG, CM)
✓ Dimension conversions for all length/width/height/depth columns
✓ String normalization, numeric cleaning
✓ Produces resolved + remaining issue reports
✓ Azure + Local mode supported
python data_health_check.py --vendor "Grote Lighting" --local
python data_autofix.py --vendor "Grote Lighting" --local
python data_canonicalize.py --vendor "Grote Lighting" --local
python data_integrity_checks.py --vendor "Grote Lighting" --local
python build_etl_mapped.py --vendor "Grote Lighting" --local
"""

import os
import json
import yaml
import hashlib
from io import BytesIO
from datetime import datetime
from typing import Dict, List

import pandas as pd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from azure.storage.blob import BlobServiceClient


# =========================================================
# CONFIG
# =========================================================
AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = os.getenv("SILVER_CONTAINER", "silver")

IN_REVIEW = "in_review"
PRICING_REVIEW = "post_pricing_review"

MAPPED_DIRNAME = "mapped"
PROFILE_DIRNAME = "profiling"
OUT_DIRNAME = "autofix"

MAPPED_FILENAME = "mapped.xlsx"
ISSUES_FILENAME = "health_issues.parquet"

UOM_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__),
    "autofix_config",
    "uom_config.yaml"
)


PROJECT_ROOT = os.path.abspath(
    os.path.dirname(__file__)
)



# Helpers

def local_vendor_root(vendor: str, mode: str) -> str:
    root = PRICING_REVIEW if mode == "post_review" else IN_REVIEW

    return os.path.join(
        PROJECT_ROOT,
        "silver",
        root,
        f"vendor={vendor}"
    )


def list_vendors_local() -> List[str]:
    root = os.path.join(PROJECT_ROOT, "silver", IN_REVIEW)
    if not os.path.exists(root):
        return []
    vendors = []
    for d in os.listdir(root):
        if d.startswith("vendor="):
            vendors.append(d.split("vendor=", 1)[1])
    return sorted(vendors)


def list_vendors_azure(container) -> List[str]:
    prefix = f"{IN_REVIEW}/vendor="
    vendors = set()
    for blob in container.list_blobs(name_starts_with=prefix):
        if f"/{PROFILE_DIRNAME}/{ISSUES_FILENAME}" in blob.name:
            v = blob.name.split("vendor=", 1)[1].split("/", 1)[0]
            vendors.add(v)
    return sorted(vendors)




# =========================================================
# Load YAML UOM config
# =========================================================
def load_uom_config():
    with open(UOM_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

UOM_RULES = load_uom_config()


# =========================================================
# Azure Helpers
# =========================================================
def get_container():
    service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
    return service.get_container_client(SILVER_CONTAINER)


def download(container, path: str) -> bytes:
    return container.get_blob_client(path).download_blob().readall()


def upload(container, path: str, data: bytes):
    container.upload_blob(path, data, overwrite=True)


# =========================================================
# DataFrame Helpers
# =========================================================
def df_from_parquet_bytes(b: bytes) -> pd.DataFrame:
    return pq.read_table(BytesIO(b)).to_pandas()


def parquet_bytes(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf)
    return buf.getvalue()


def excel_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name[:31], index=False)
    return buf.getvalue()

def enforce_identifier_types(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure identifier columns are always strings.
    Prevents Arrow from inferring numeric types.
    """
    df = df.copy()

    IDENTIFIER_COLS = {
        "Part Number",
        "SKU",
        "_entity_key",
        "_entity_id",
        "_vendor",
        "_sheet",
        "__Section",
    }

    for col in IDENTIFIER_COLS:
        if col in df.columns:
            df[col] = df[col].astype("string")

    return df



# =========================================================
# Common Helpers
# =========================================================
def norm_name(x):
    return "".join(ch.lower() for ch in str(x) if ch.isalnum())


def find_part_col(df: pd.DataFrame):
    for c in df.columns:
        if norm_name(c) in {"partnumber", "partno", "partnum"}:
            return c
    return None


# =========================================================
# UOM Conversion Engine
# =========================================================
def convert_weight_to_kg(value: float, uom: str) -> float:
    table = UOM_RULES.get("weight_uom_to_kg", {})
    u = str(uom).upper().strip()
    if u not in table:
        return None
    return float(value) * float(table[u])


def convert_dimension_to_cm(value: float, uom: str) -> float:
    table = UOM_RULES.get("dimension_uom_to_cm", {})
    u = str(uom).upper().strip()
    if u not in table:
        return None
    return float(value) * float(table[u])


def is_dimension_column(col: str) -> bool:
    s = col.lower()
    return any(k in s for k in ["length", "width", "height", "depth"])


# =========================================================
# FIX FUNCTIONS
# =========================================================
def fix_trim(value):
    if pd.isna(value):
        return value
    return str(value).strip()


def fix_numeric(value):
    if pd.isna(value):
        return value
    s = str(value).strip().replace("$", "")
    try:
        return float(s)
    except Exception:
        return value


# ---------------------------------------------------------
# APPLY FIX
# ---------------------------------------------------------
def apply_fix(df, idx, field, old_value, issue):
    issue_type = issue["_issue_type"]
    target = str(issue["_expected_or_hint"]).upper().strip()

    # -----------------------------
    # 1. string normalization
    # -----------------------------
    if issue_type == "string_normalization_candidate":
        new_val = fix_trim(old_value)
        if new_val != old_value:
            return new_val, f"trim({old_value!r} → {new_val!r})"
        return old_value, None

    # -----------------------------
    # 2. numeric cleanup
    # -----------------------------
    if issue_type == "datatype_mismatch":
        new_val = fix_numeric(old_value)
        if new_val != old_value:
            return new_val, f"numeric({old_value} → {new_val})"
        return old_value, None

    # -----------------------------
    # 3. UOM conversion
    # -----------------------------
    if issue_type == "conversion_candidate":
        source = str(old_value).upper().strip()
        # 🚫 Idempotency guard: already canonical
        if source == target:
            return old_value, None

        valid_weight = source in UOM_RULES.get("weight_uom_to_kg", {})
        valid_dim = source in UOM_RULES.get("dimension_uom_to_cm", {})

        if not valid_weight and not valid_dim:
            return old_value, None  # skip EA, PG, PK

        # Change UOM
        df.loc[idx, field] = target
        numeric_field = field.replace(" UOM", "").strip()

        transformations = []

        # --- Weight conversion ---
        if valid_weight and target == "KG":
            if numeric_field in df.columns:
                num_old = df.loc[idx, numeric_field].values[0]
                num_new = convert_weight_to_kg(num_old, source)
                if num_new is not None and num_new != num_old:
                    df.loc[idx, numeric_field] = num_new
                    transformations.append(
                        f"{numeric_field}: {num_old} {source} → {num_new} KG"
                    )


        # --- Dimension conversion ---
        if valid_dim and target == "CM":
            dims = [c for c in df.columns if is_dimension_column(c)]
            for col in dims:
                old_dim = df.loc[idx, col].values[0]
                if pd.isna(old_dim):
                    continue
                new_dim = convert_dimension_to_cm(old_dim, source)
                if new_dim is not None and new_dim != old_dim:
                    df.loc[idx, col] = new_dim
                    transformations.append(
                        f"{col}: {old_dim} {source} → {new_dim} CM"
                    )

        if transformations:
            return target, " | ".join(transformations)

        return old_value, None

    return old_value, None


# =========================================================
# APPLY FIXES PER SHEET
# =========================================================
def apply_sheet_fixes(df: pd.DataFrame, issues: pd.DataFrame):
    df = df.copy()
    part_col = find_part_col(df)

    if not part_col:
        print("⚠ No Part Number column found; skipping.")
        return df, [], []

    df[part_col] = df[part_col].astype("string").fillna("").str.strip()

    if "_transformation_applied" not in df.columns:
        df["_transformation_applied"] = ""

    resolved = []
    remaining = []

    # Track resolved per row instead of per entity
    resolved_row_field = set()  # (row_index, field)

    for _, issue in issues.iterrows():
        entity = str(issue["_entity_key"]).strip()
        field = issue["_field"]
        issue_type = issue["_issue_type"]

        if field not in df.columns:
            remaining.append(issue.to_dict())
            continue

        matching_rows = df.index[df[part_col] == entity]

        if len(matching_rows) == 0:
            remaining.append(issue.to_dict())
            continue

        applied_any = False

        for row_idx in matching_rows:

            if (row_idx, field) in resolved_row_field:
                continue

            old_val = df.loc[row_idx, field]

            new_val, trans = apply_fix(
                df=df,
                idx=[row_idx],   # pass as list to match your existing logic
                field=field,
                old_value=old_val,
                issue=issue
            )

            if trans is None:
                continue

            # Append transformation log
            existing = df.loc[row_idx, "_transformation_applied"]
            if trans not in existing:
                df.loc[row_idx, "_transformation_applied"] = (
                    (existing + " | " + trans).strip(" |")
                )

            resolved.append({
                "_tab": issue["_tab"],
                "_entity_key": entity,
                "_field": field,
                "_issue_type": issue_type,
                "_old_value": old_val,
                "_new_value": new_val,
                "_transformation": trans,
            })

            resolved_row_field.add((row_idx, field))
            applied_any = True

        if not applied_any:
            remaining.append(issue.to_dict())

    return df, resolved, remaining

# =========================================================
# IDENTITY BUILDER
# =========================================================
def add_entity_identity(flat: pd.DataFrame, vendor: str):
    """Adds _entity_key, _entity_id, _vendor, and autofix lineage."""

    # detect part number column
    part_col = None
    for c in flat.columns:
        if norm_name(c) in {"partnumber", "partno", "partnum"}:
            part_col = c
            break

    if not part_col:
        raise RuntimeError(" Part Number column not found in autofix output.")

    def _normalize(x):
        s = str(x).strip()
        return s if s else None

    # canonical part key
    flat["_entity_key"] = flat[part_col].apply(_normalize)

    # stable hashed identity
    def make_id(key):
        if key is None:
            return None
        raw = f"{vendor}|{key}"
        return hashlib.sha256(raw.encode()).hexdigest()

    flat["_entity_id"] = flat["_entity_key"].apply(make_id)

    if flat["_entity_id"].isna().any():
        bad = flat[flat["_entity_id"].isna()].head()
        raise RuntimeError(f" Could not assign _entity_id for rows:\n{bad}")

    # REQUIRED for canonicalization
    flat["_vendor"] = vendor

    # Optional but useful later
    flat["_row_id"] = flat.index.astype(int)

    # Autofix lineage
    rid = hashlib.sha256(f"{vendor}|{datetime.utcnow()}".encode()).hexdigest()[:16]
    flat["_autofix_run_id"] = rid
    flat["_autofix_timestamp"] = datetime.utcnow().isoformat()

    return flat



# =========================================================
# LOCAL MODE
# =========================================================
def run_vendor_local(vendor: str, local_mapped: str, local_issues: str):
    print(f"\n🛠 Autofix (Local Mode) for vendor: {vendor}")

    sheets = pd.read_excel(
        local_mapped,
        sheet_name=None,
        dtype=str,
        converters={
            "Part Number": str,
            "SKU": str,
        }
    )

    issues_df = pd.read_parquet(local_issues)

    fixable = issues_df[issues_df["_fixable_by_code"] == True]
    non_fixable = issues_df[issues_df["_fixable_by_code"] == False].to_dict("records")

    grouped = fixable.groupby("_tab")

    resolved_all = []
    remaining_all = []
    fixed_sheets = {}
    tabs_modified = set()

    for sheet_name, df in sheets.items():
        if sheet_name in grouped.groups:
            new_df, resolved, remaining = apply_sheet_fixes(df, grouped.get_group(sheet_name))
            fixed_sheets[sheet_name] = new_df
            resolved_all.extend(resolved)
            remaining_all.extend(remaining)
            if resolved:
                tabs_modified.add(sheet_name)
        else:
            fixed_sheets[sheet_name] = df

    # Combine into flat table
    flat = pd.concat([df.assign(_sheet=name) for name, df in fixed_sheets.items()], ignore_index=True)

    # ---------- ADD ENTITY IDENTITY ----------
    flat = add_entity_identity(flat, vendor)
    flat = enforce_identifier_types(flat)

    vendor_root = local_vendor_root(vendor, mode)
    out_dir = os.path.join(vendor_root, OUT_DIRNAME)
    os.makedirs(out_dir, exist_ok=True)

    flat.to_parquet(
        os.path.join(out_dir, "data_autofixed.parquet"),
        index=False
    )


    with pd.ExcelWriter(
        os.path.join(out_dir, "data_autofixed.xlsx"),
        engine="xlsxwriter"
    ) as writer:
        for name, df in fixed_sheets.items():
            df.to_excel(writer, sheet_name=name[:31], index=False)
    
    # ✅ Add non-fixable issues only once, at the end
    remaining_all.extend(non_fixable)
    # ✅ Final safety cleanup: drop remaining issues already resolved at entity+field level
    if resolved_all and remaining_all:
        resolved_keys = {
            (r["_entity_key"], r["_field"]) for r in resolved_all
        }
        remaining_all = [
            r for r in remaining_all
            if (r["_entity_key"], r["_field"]) not in resolved_keys
        ]


    report_sheets = {
        "issues_resolved": pd.DataFrame(resolved_all),
        "issues_remaining": pd.DataFrame(remaining_all),
    }

    with pd.ExcelWriter(
        os.path.join(out_dir, "autofix_report.xlsx"),
        engine="openpyxl"
    ) as writer:
        for name, df in report_sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)

    print(f"✅ Local autofix complete | resolved={len(resolved_all)}, remaining={len(remaining_all)}")


# =========================================================
# AZURE MODE
# =========================================================
def run_vendor_azure(container, vendor: str, submission_id: str, mode: str):
    print(f"\n🛠 Autofix (Azure Mode) for vendor: {vendor}")

    root = PRICING_REVIEW if mode == "post_review" else IN_REVIEW

    base = (
        f"{root}/vendor={vendor}/"
        f"submission={submission_id}"
    )

    mapped_path = f"{base}/{MAPPED_DIRNAME}/{MAPPED_FILENAME}"
    issues_path = f"{base}/{PROFILE_DIRNAME}/{ISSUES_FILENAME}"

    issues_df = df_from_parquet_bytes(download(container, issues_path))
    fixable = issues_df[issues_df["_fixable_by_code"] == True]
    non_fixable = issues_df[issues_df["_fixable_by_code"] == False].to_dict('records')

    grouped = fixable.groupby("_tab")

    # load workbook
    wb = pd.read_excel(BytesIO(download(container, mapped_path)), sheet_name=None, dtype=str)
    for sheet, df in wb.items():
        if "Part Number" in df.columns:
            vals = (
                df["Part Number"]
                .dropna()
                .astype(str)
                .unique()
                .tolist()
            )
            print(f"\n[{sheet}] Part Numbers:")
            print(sorted(vals))

    resolved_all = []
    remaining_all = []
    fixed_sheets = {}

    for sheet_name, df in wb.items():
        if sheet_name in grouped.groups:
            new_df, resolved, remaining = apply_sheet_fixes(df, grouped.get_group(sheet_name))
            fixed_sheets[sheet_name] = new_df
            resolved_all.extend(resolved)
            remaining_all.extend(remaining)
        else:
            fixed_sheets[sheet_name] = df

    flat = pd.concat([df.assign(_sheet=name) for name, df in fixed_sheets.items()], ignore_index=True)

    # ---------- ADD ENTITY IDENTITY ----------
    flat = add_entity_identity(flat, vendor)
    flat = enforce_identifier_types(flat)


    out_base = f"{base}/{OUT_DIRNAME}"
    upload(container, f"{out_base}/data_autofixed.parquet", parquet_bytes(flat))
    upload(container, f"{out_base}/data_autofixed.xlsx", excel_bytes(fixed_sheets))

    # ✅ Add non-fixable issues only once, at the end
    remaining_all.extend(non_fixable)

    # ✅ Final safety cleanup: drop remaining issues already resolved at entity+field level
    if resolved_all and remaining_all:
        resolved_keys = {
            (r["_entity_key"], r["_field"]) for r in resolved_all
        }
        remaining_all = [
            r for r in remaining_all
            if (r["_entity_key"], r["_field"]) not in resolved_keys
        ]


    report_sheets = {
        "issues_resolved": pd.DataFrame(resolved_all),
        "issues_remaining": pd.DataFrame(remaining_all),
    }

    upload(container, f"{out_base}/autofix_report.xlsx", excel_bytes(report_sheets))
    upload(container, f"{out_base}/autofix_report.parquet", parquet_bytes(pd.concat(report_sheets.values())))

    print(f"✅ Azure autofix complete | resolved={len(resolved_all)}, remaining={len(remaining_all)}")


# =========================================================
# External Pipeline Entry Point
# =========================================================

def run_autofix(vendor: str, submission_id: str, source: str = "full") -> None:
    """
    Entry point for other pipelines (e.g. post-review pipeline).
    source:
        "full"      → in_review
        "reviewed"  → post_pricing_review
    """

    container = get_container()

    if source == "reviewed":
        mode = "post_review"
    else:
        mode = "full"

    run_vendor_azure(
        container=container,
        vendor=vendor,
        submission_id=submission_id,
        mode=mode
    )

# =========================================================
# CLI
# =========================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", type=str, help="Vendor name")
    parser.add_argument("--submission-id", type=str, required=False)
    parser.add_argument("--all", action="store_true", help="Run autofix for all vendors")
    parser.add_argument("--local", action="store_true", help="Run in local mode")
    parser.add_argument("--local_mapped", help="Local mapped.xlsx (single-vendor only)")
    parser.add_argument("--local_issues", help="Local health_issues.parquet (single-vendor only)")
    parser.add_argument("--mode", default="full")


    args = parser.parse_args()
    submission_id = args.submission_id
    mode = args.mode

    # -------------------------
    # LOCAL MODE
    # -------------------------
    if args.local:
        if args.all:
            vendors = list_vendors_local()
            if not vendors:
                raise SystemExit(" No local vendors found under silver/in_review/")
            print(f"🔎 Found {len(vendors)} local vendors")
            for v in vendors:
                vendor_root = local_vendor_root(v, mode)
                mapped = os.path.join(vendor_root, MAPPED_DIRNAME, MAPPED_FILENAME)
                issues = os.path.join(vendor_root, PROFILE_DIRNAME, ISSUES_FILENAME)

                if not os.path.exists(mapped) or not os.path.exists(issues):
                    print(f"⚠️ Skipping {v}: missing mapped or health_issues")
                    continue

                run_vendor_local(v, mapped, issues)
        else:
            if not args.vendor or not submission_id:
                raise SystemExit(" Provide --vendor and --submission-id")

            vendor_root = local_vendor_root(args.vendor, mode)

            mapped = args.local_mapped or os.path.join(
                vendor_root, MAPPED_DIRNAME, MAPPED_FILENAME
            )
            issues = args.local_issues or os.path.join(
                vendor_root, PROFILE_DIRNAME, ISSUES_FILENAME
            )

            if not os.path.exists(mapped):
                raise SystemExit(f" mapped.xlsx not found at: {mapped}")

            if not os.path.exists(issues):
                raise SystemExit(f" health_issues.parquet not found at: {issues}")

            run_vendor_local(args.vendor, mapped, issues)


    # -------------------------
    # AZURE MODE
    # -------------------------
    else:
        container = get_container()

        if args.all:
            vendors = list_vendors_azure(container)
            if not vendors:
                raise SystemExit(" No vendors found with profiling outputs")
            print(f"🔎 Found {len(vendors)} Azure vendors")
            for v in vendors:
                run_vendor_azure(container, v, submission_id, mode)
        else:
            if not args.vendor:
                raise SystemExit("Provide --vendor or use --all")
            run_vendor_azure(container, args.vendor, submission_id, mode)
