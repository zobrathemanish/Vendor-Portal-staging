"""
data_health_check.py (CLEAN FINAL — entity-first health diagnostics)

What this does
--------------
Runs a vendor "data health check" on mapped.xlsx (multi-tab) using ENTITY-FIRST semantics.

ENTITY-FIRST rule (most important):
- Missing required fields are evaluated PER Part Number (entity), not per row.
  -> A field is "missing" only if ALL rows for that Part Number in that tab are blank/null.

This script detects:
- Entity-level completeness (missing required fields per tab)
- "At least one ..." rules per entity across tabs
- Cross-reference issues (items↔pricing↔assets)
- Row/column missingness ratios (informational)
- Format/type issues (dates, numeric coercion)
- Anomalies (negative values, suspicious zeros, duplicates)
- Outliers (IQR) including Shipping Volume (Ship L*W*H)
- Conversion candidates (Currency CAD, Weight UOM KG, Dimension UOM CM)
- String normalization candidates (trim whitespace)

Reads (Azure)
-------------
silver/in_review/vendor=<vendor>/mapped/mapped.xlsx   (preferred)
Fallback: mapped.parquet (single table) scanned as one logical sheet "mapped"

Writes (Azure)
--------------
silver/in_review/vendor=<vendor>/profiling/
  - health_issues.parquet
  - health_issues.xlsx
  - health_summary.json
  - row_missingness.parquet
  - column_missingness.parquet
  - entity_completeness.parquet
  - statistics.json

Run
---
python data_health_check.py --vendor "Grote Lighting"
python data_health_check.py --all

Optional local debug:
python data_health_check.py --local_xlsx "C:/path/mapped.xlsx" --vendor "Local Vendor"

Notes
-----
- Informational only: NO rejection, NO feedback, NO promotion.
- Decisions stay in data_integrity_checks.py / approval layer.
"""

import os
import json
import uuid
import hashlib
from io import BytesIO
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from azure.storage.blob import BlobServiceClient


# =========================================================
# CONFIG
# =========================================================
AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = os.getenv("SILVER_CONTAINER", "silver")

IN_REVIEW_ROOT = "in_review"
PRICING_REVIEW_ROOT = "post_pricing_review"

MAPPED_DIRNAME = "mapped"
OUT_DIRNAME = "profiling"

# Expected tabs
TAB_ITEM_MASTER = "Item_Master"
TAB_DESCRIPTIONS = "Descriptions"
TAB_EXTENDED_INFO = "Extended_Info"
TAB_ATTRIBUTES = "Attributes"
TAB_PACKAGES = "Packages"
TAB_DIGITAL_ASSETS = "Digital_Assets"
TAB_PRICING = "Pricing"

ALLOWED_SHEETS = {
    TAB_ITEM_MASTER,
    TAB_DESCRIPTIONS,
    TAB_EXTENDED_INFO,
    TAB_ATTRIBUTES,
    TAB_PACKAGES,
    TAB_DIGITAL_ASSETS,
    TAB_PRICING,
}

PART_NUMBER_LOGICAL = "Part Number"

PROJECT_ROOT = os.path.abspath(
    os.path.dirname(__file__)   # ETL/data_ETL
)

def local_vendor_root(vendor: str) -> str:
    return os.path.join(
        PROJECT_ROOT,
        "silver",
        IN_REVIEW_ROOT,
        f"vendor={vendor}"
    )


def parse_submission_type(submission_type: str):
    return {
        "is_review": "review" in submission_type,
        "is_delta": "delta" in submission_type,
        "workflow": "pricing" if "pricing" in submission_type else "product"
    }

# -----------------------------
# ENTITY-level required fields (per tab)
# -----------------------------
ENTITY_REQUIRED_FIELDS: Dict[str, Dict[str, str]] = {
    TAB_ITEM_MASTER: {
        "Brand Label": "high",
        "Part Number": "high",
        "Category": "high",
        "Product Status": "high",
        "Minimum Order Quantity UOM": "medium",
        "Minimum Order Quantity": "medium",
        "HazmatFlag": "medium",  # NOTE: mapped has HazmatFlag (no space)
        "Barcode Type": "medium",
        "Barcode Number": "medium",
    },
    TAB_PRICING: {
        "Net Price": "high",
        "Effective Date": "low",
    },
}

# -----------------------------
# "At least one ..." rules (entity-level)
# -----------------------------
REQUIRED_DESCRIPTION_CODES_HIGH = {"SHO", "DES", "ASC"}  # high
REQUIRED_DESCRIPTION_CODES_MED = {"MKT", "FAB", "EXT"}  # medium
REQUIRED_EXTINFO_CODES_MED = {"CTO", "HSB"}             # medium
REQUIRED_ASSET_TYPES = {"P04", "P01", "LIN"}            # medium
# supported canonicalization UOM rules (same as YAML)
KNOWN_WEIGHT_UOMS = {"LB", "LBS", "OZ", "G", "GRAM", "KG"}
KNOWN_DIM_UOMS = {"CM", "MM", "IN"}
KNOWN_CURRENCY = {"USD", "CAD"}


PACKAGE_REQUIRED_FIELDS = [
    "Package UOM",
    "Package Quantity of Eaches",
    "Weight",
    "Weight UOM",
    "Dimension UOM",
    "Ship Length",
    "Ship Height",
    "Ship Width",
]

# -----------------------------
# Outliers (row-level, informational)
# -----------------------------
OUTLIER_FIELDS = {
    "Net Price": {"severity": "low"},
    "Weight": {"severity": "low"},
    "Minimum Order Quantity": {"severity": "low"},
    "Shipping Volume": {"severity": "medium"},  # computed if L/W/H exist
}

# -----------------------------
# Conversions (row-level candidates)
# -----------------------------
CONVERSION_FIELDS = {
    "Currency": {"target": "CAD"},
    "Weight UOM": {"target": "KG"},
    "Dimension UOM": {"target": "CM"},
}

# -----------------------------
# String normalization candidates (row-level)
# (we only flag whitespace-trim mismatch — safe & deterministic)
# -----------------------------
STRING_NORMALIZATION_FIELDS = [
    "Brand Label",
    "Category",
    "Product Status",
    "Description Value",
    "Attribute Name",
    "Attribute Value",
    "FileName",
]

DATE_FIELDS = ["Effective Date", "Start Date", "End Date"]


# =========================================================
# Azure helpers
# =========================================================
def get_container():
    if not AZURE_CONN_STR:
        raise RuntimeError("Missing env var: AZURE_STORAGE_CONNECTION_STRING")
    service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
    return service.get_container_client(SILVER_CONTAINER)


def download_blob_bytes(container, blob_path: str) -> bytes:
    return container.get_blob_client(blob_path).download_blob().readall()


def upload_blob_bytes(container, blob_path: str, data: bytes) -> None:
    container.upload_blob(blob_path, data, overwrite=True)


def list_vendors_with_mapped(container) -> List[str]:
    """
    Finds vendors that have mapped/mapped.xlsx OR mapped/mapped.parquet under:
      in_review/vendor=<vendor>/mapped/
    """
    prefix = f"{IN_REVIEW_ROOT}/vendor="
    vendors = set()

    for blob in container.list_blobs(name_starts_with=prefix):
        name = blob.name
        if f"/{MAPPED_DIRNAME}/mapped.xlsx" in name or f"/{MAPPED_DIRNAME}/mapped.parquet" in name:
            try:
                after = name.split("vendor=", 1)[1]
                vendor = after.split(f"/{MAPPED_DIRNAME}/", 1)[0]
                vendors.add(vendor)
            except Exception:
                continue

    return sorted(vendors)


# =========================================================
# Bytes helpers
# =========================================================
def df_from_parquet_bytes(data: bytes) -> pd.DataFrame:
    buf = BytesIO(data)
    table = pq.read_table(buf)
    return table.to_pandas()


def parquet_bytes_from_df(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, buf)
    return buf.getvalue()


def excel_bytes_from_sheets(sheets: Dict[str, pd.DataFrame]) -> bytes:
    """
    Writes multiple sheets into a single xlsx bytes.
    """
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets.items():
            safe_name = name[:31]  # Excel sheet name limit
            df.to_excel(writer, sheet_name=safe_name, index=False)
    return buf.getvalue()


# =========================================================
# ID + column matching helpers
# =========================================================
def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def norm_name(x: str) -> str:
    return "".join(ch.lower() for ch in str(x).strip() if ch.isalnum())


def find_col(df: pd.DataFrame, preferred_names: List[str]) -> Optional[str]:
    """
    Find a column in df that matches any of preferred_names by normalized name.
    """
    if df is None or df.empty:
        return None
    norm_map = {norm_name(c): c for c in df.columns}
    for name in preferred_names:
        key = norm_name(name)
        if key in norm_map:
            return norm_map[key]
    return None


def ensure_col(df: pd.DataFrame, logical_name: str) -> Tuple[bool, Optional[str]]:
    actual = find_col(df, [logical_name])
    return (actual is not None, actual)


def normalize_part_number(x) -> Optional[str]:
    if pd.isna(x):
        return None
    s = str(x).strip()
    if s == "":
        return None
    if s.isdigit():
        s = s.zfill(5)
    return s


def add_lineage_and_ids(df: pd.DataFrame, vendor: str, source_file: str, source_sheet: str) -> pd.DataFrame:
    df = df.copy()
    run_id = str(uuid.uuid4())
    ts = datetime.utcnow().isoformat()

    def set_if_missing(col, value):
        if col not in df.columns:
            df[col] = value

    set_if_missing("_vendor", vendor)
    set_if_missing("_source_file", source_file)
    set_if_missing("_source_sheet", source_sheet)
    set_if_missing("_ingestion_run_id", run_id)
    set_if_missing("_ingestion_timestamp", ts)

    if "_source_row_number" not in df.columns:
        df["_source_row_number"] = df.index + 2  # excel style

    if "_row_id" not in df.columns:
        df["_row_id"] = df.apply(
            lambda r: sha256(f"{vendor}|{source_file}|{source_sheet}|{int(r['_source_row_number'])}"),
            axis=1,
        )

    part_col = find_col(df, [PART_NUMBER_LOGICAL])
    if part_col:
        df["_entity_key"] = df[part_col].apply(normalize_part_number)
        df["_entity_id"] = df["_entity_key"].apply(lambda p: sha256(f"{vendor}|{p}") if p else None)
        #DEBUG
        print("DEBUG AFTER NORMALIZATION:")
        # print("Original:", df[part_col].head(5).tolist())
        # print("EntityKey:", df["_entity_key"].head(5).tolist())
        df[part_col] = df["_entity_key"]
        print("after doing whatsoever")
        # print("Original:", df[part_col].head(5).tolist())
        # print("EntityKey:", df["_entity_key"].head(5).tolist())
        df[part_col] = df["_entity_key"]

    else:
        df["_entity_key"] = None
        df["_entity_id"] = None

    # identity drift guard if part number exists (same part should not map to multiple entity_id)
    if part_col:
        drift = df.groupby(part_col)["_entity_id"].nunique(dropna=True)
        if (drift > 1).any():
            bad = drift[drift > 1]
            raise RuntimeError(f" Identity drift detected: Part Number maps to multiple _entity_id\n{bad}")

    return df


# =========================================================
# Issue schema + sanitizer
# =========================================================
ISSUE_COLUMNS = [
    "_vendor", "_tab", "_scope",
    "_row_id", "_entity_key", "_entity_id",
    "_field", "_issue_type", "_issue_subtype",
    "_observed_value", "_expected_or_hint",
    "_detection_method", "_severity",
    "_fixable_by_code", "_confidence",
    "_notes",
]


def record_issue(
    *,
    vendor: str,
    tab: str,
    scope: str,  # row | entity | dataset
    issue_type: str,
    issue_subtype: str,
    severity: str,
    detection_method: str,
    entity_key: Optional[str] = None,
    entity_id: Optional[str] = None,
    row_id: Optional[str] = None,
    field: Optional[str] = None,
    observed_value: Any = None,
    expected_or_hint: Any = None,
    fixable_by_code: bool = False,
    confidence: float = 1.0,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "_vendor": vendor,
        "_tab": tab,
        "_scope": scope,
        "_row_id": row_id,
        "_entity_key": entity_key,
        "_entity_id": entity_id,
        "_field": field,
        "_issue_type": issue_type,
        "_issue_subtype": issue_subtype,
        "_observed_value": observed_value,
        "_expected_or_hint": expected_or_hint,
        "_detection_method": detection_method,
        "_severity": severity,
        "_fixable_by_code": bool(fixable_by_code),
        "_confidence": float(confidence),
        "_notes": notes,
    }


def sanitize_issue_df_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    """
    Make diagnostics table Parquet-safe by coercing heterogeneous value columns to string.
    """
    df = df.copy()
    if df.empty:
        return pd.DataFrame(columns=ISSUE_COLUMNS)

    # Ensure all expected columns exist
    for c in ISSUE_COLUMNS:
        if c not in df.columns:
            df[c] = None
    df = df[ISSUE_COLUMNS]

    force_str = [
        "_tab", "_scope", "_field", "_issue_type", "_issue_subtype",
        "_observed_value", "_expected_or_hint", "_detection_method", "_severity", "_notes"
    ]
    for c in force_str:
        df[c] = df[c].astype("string")

    # ids as string
    for c in ["_vendor", "_row_id", "_entity_key", "_entity_id"]:
        df[c] = df[c].astype("string")

    # flags/numerics
    df["_fixable_by_code"] = df["_fixable_by_code"].astype(bool)
    df["_confidence"] = pd.to_numeric(df["_confidence"], errors="coerce").fillna(1.0).astype(float)

    return df


# =========================================================
# General helpers
# =========================================================
def blank_or_null(s: pd.Series) -> pd.Series:
    m = s.isna()
    if s.dtype == object or str(s.dtype).startswith("string"):
        m = m | (s.astype(str).str.strip() == "")
    return m


def parts_set(df: Optional[pd.DataFrame]) -> set:
    if df is None or df.empty:
        return set()
    pc = find_col(df, [PART_NUMBER_LOGICAL])
    if not pc:
        return set()
    return set(df[pc].apply(normalize_part_number).dropna().unique())

def observed_entity(row):
    """
    Always use normalized entity key for observed values tied to identity.
    Prevents leading-zero loss.
    """
    return row.get("_entity_key")

def find_local_mapped_xlsx(vendor: str) -> Optional[str]:
    path = os.path.join(
        local_vendor_root(vendor),
        MAPPED_DIRNAME,
        "mapped.xlsx"
    )
    return path if os.path.exists(path) else None



# =========================================================
# ENTITY-FIRST missing checks (per tab)
# =========================================================
def entity_required_field_checks(vendor: str, tab: str, df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    For each required field in a tab, for each Part Number:
      issue if ALL rows for that Part Number have that field blank/null.
    """
    issues: List[Dict[str, Any]] = []

    rules = ENTITY_REQUIRED_FIELDS.get(tab, {})
    if not rules:
        return issues

    part_col = find_col(df, [PART_NUMBER_LOGICAL])
    if not part_col:
        issues.append(record_issue(
            vendor=vendor,
            tab=tab,
            scope="dataset",
            issue_type="missing_column",
            issue_subtype="part_number_missing",
            severity="high",
            detection_method="schema_check",
            field=PART_NUMBER_LOGICAL,
            expected_or_hint="required for entity-level checks",
        ))
        return issues

    df = df.copy()
    df["_pn"] = df[part_col].apply(normalize_part_number)

    for field, severity in rules.items():
        exists, actual = ensure_col(df, field)
        if not exists:
            issues.append(record_issue(
                vendor=vendor,
                tab=tab,
                scope="dataset",
                issue_type="missing_column",
                issue_subtype="required_column_absent",
                severity="high",
                detection_method="schema_check",
                field=field,
                expected_or_hint="column must exist",
            ))
            continue

        for pn, g in df.groupby("_pn", dropna=True):
            if pn is None:
                continue
            if blank_or_null(g[actual]).all():
                issues.append(record_issue(
                    vendor=vendor,
                    tab=tab,
                    scope="entity",
                    issue_type="missing_required_field",
                    issue_subtype="entity_level_missing",
                    severity=severity,
                    detection_method="entity_coverage",
                    entity_key=pn,
                    entity_id=sha256(f"{vendor}|{pn}"),
                    field=field,
                    expected_or_hint="present for at least one row of this part",
                ))
    return issues


# =========================================================
# "At least one ..." entity rules
# =========================================================
def descriptions_code_coverage(vendor: str, df_desc: pd.DataFrame) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    tab = TAB_DESCRIPTIONS

    part_col = find_col(df_desc, [PART_NUMBER_LOGICAL])
    code_col = find_col(df_desc, ["Description Code"])
    if not part_col or not code_col:
        if not part_col:
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="dataset",
                issue_type="missing_column", issue_subtype="part_number_missing",
                severity="high", detection_method="schema_check",
                field=PART_NUMBER_LOGICAL, expected_or_hint="required for coverage checks"
            ))
        if not code_col:
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="dataset",
                issue_type="missing_column", issue_subtype="description_code_missing",
                severity="high", detection_method="schema_check",
                field="Description Code", expected_or_hint="required for coverage checks"
            ))
        return issues

    df = df_desc.copy()
    df["_pn"] = df[part_col].apply(normalize_part_number)
    df["_code"] = df[code_col].astype(str).str.strip().str.upper()

    grouped = df.groupby("_pn", dropna=True)["_code"].apply(lambda x: set(x.dropna()))
    for pn, code_set in grouped.items():
        if pn is None:
            continue
        if not (code_set & REQUIRED_DESCRIPTION_CODES_HIGH):
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="entity",
                issue_type="missing_required_code",
                issue_subtype="descriptions_missing_high_set",
                severity="high", detection_method="group_coverage",
                entity_key=pn, entity_id=sha256(f"{vendor}|{pn}"),
                field="Description Code",
                observed_value=sorted(code_set),
                expected_or_hint=sorted(REQUIRED_DESCRIPTION_CODES_HIGH),
            ))
        if not (code_set & REQUIRED_DESCRIPTION_CODES_MED):
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="entity",
                issue_type="missing_required_code",
                issue_subtype="descriptions_missing_medium_set",
                severity="medium", detection_method="group_coverage",
                entity_key=pn, entity_id=sha256(f"{vendor}|{pn}"),
                field="Description Code",
                observed_value=sorted(code_set),
                expected_or_hint=sorted(REQUIRED_DESCRIPTION_CODES_MED),
            ))
    return issues


def extended_info_code_coverage(vendor: str, df_ext: pd.DataFrame) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    tab = TAB_EXTENDED_INFO

    part_col = find_col(df_ext, [PART_NUMBER_LOGICAL])
    code_col = find_col(df_ext, ["Extended Info Code"])
    if not part_col or not code_col:
        if not part_col:
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="dataset",
                issue_type="missing_column", issue_subtype="part_number_missing",
                severity="high", detection_method="schema_check",
                field=PART_NUMBER_LOGICAL, expected_or_hint="required for coverage checks"
            ))
        if not code_col:
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="dataset",
                issue_type="missing_column", issue_subtype="extended_info_code_missing",
                severity="high", detection_method="schema_check",
                field="Extended Info Code", expected_or_hint="required for coverage checks"
            ))
        return issues

    df = df_ext.copy()
    df["_pn"] = df[part_col].apply(normalize_part_number)
    df["_code"] = df[code_col].astype(str).str.strip().str.upper()

    grouped = df.groupby("_pn", dropna=True)["_code"].apply(lambda x: set(x.dropna()))
    for pn, code_set in grouped.items():
        if pn is None:
            continue
        if not (code_set & REQUIRED_EXTINFO_CODES_MED):
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="entity",
                issue_type="missing_extended_info_code",
                issue_subtype="extended_info_missing_required_set",
                severity="medium", detection_method="group_coverage",
                entity_key=pn, entity_id=sha256(f"{vendor}|{pn}"),
                field="Extended Info Code",
                observed_value=sorted(code_set),
                expected_or_hint=sorted(REQUIRED_EXTINFO_CODES_MED),
            ))
    return issues


def attributes_min_one(vendor: str, df_attr: pd.DataFrame, universe_parts: set) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    tab = TAB_ATTRIBUTES

    part_col = find_col(df_attr, [PART_NUMBER_LOGICAL])
    name_col = find_col(df_attr, ["Attribute Name"])
    val_col = find_col(df_attr, ["Attribute Value"])

    if not part_col:
        issues.append(record_issue(
            vendor=vendor, tab=tab, scope="dataset",
            issue_type="missing_column", issue_subtype="part_number_missing",
            severity="high", detection_method="schema_check",
            field=PART_NUMBER_LOGICAL, expected_or_hint="required for attributes checks"
        ))
        return issues

    df = df_attr.copy()
    df["_pn"] = df[part_col].apply(normalize_part_number)

    if name_col and val_col:
        valid = (~blank_or_null(df[name_col])) & (~blank_or_null(df[val_col]))
        parts_with_attr = set(df.loc[valid, "_pn"].dropna().unique())
    else:
        # still compute presence by any row; also warn about schema
        parts_with_attr = set(df["_pn"].dropna().unique())
        if not name_col:
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="dataset",
                issue_type="missing_column", issue_subtype="attribute_name_missing",
                severity="medium", detection_method="schema_check",
                field="Attribute Name", expected_or_hint="recommended"
            ))
        if not val_col:
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="dataset",
                issue_type="missing_column", issue_subtype="attribute_value_missing",
                severity="medium", detection_method="schema_check",
                field="Attribute Value", expected_or_hint="recommended"
            ))

    missing = sorted([p for p in universe_parts if p not in parts_with_attr])
    for pn in missing:
        issues.append(record_issue(
            vendor=vendor, tab=tab, scope="entity",
            issue_type="missing_entity_relationship",
            issue_subtype="no_attributes",
            severity="low", detection_method="cross_reference",
            entity_key=pn, entity_id=sha256(f"{vendor}|{pn}"),
            expected_or_hint=">=1 attribute row",
            observed_value=0,
        ))
    return issues


def packages_min_one_and_fields(vendor: str, df_pkg: pd.DataFrame, universe_parts: set) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    tab = TAB_PACKAGES

    part_col = find_col(df_pkg, [PART_NUMBER_LOGICAL])
    if not part_col:
        issues.append(record_issue(
            vendor=vendor, tab=tab, scope="dataset",
            issue_type="missing_column", issue_subtype="part_number_missing",
            severity="high", detection_method="schema_check",
            field=PART_NUMBER_LOGICAL, expected_or_hint="required for packages checks"
        ))
        return issues

    df = df_pkg.copy()
    df["_pn"] = df[part_col].apply(normalize_part_number)

    parts_with_pkg = set(df["_pn"].dropna().unique())
    missing_pkg = sorted([p for p in universe_parts if p not in parts_with_pkg])
    for pn in missing_pkg:
        issues.append(record_issue(
            vendor=vendor, tab=tab, scope="entity",
            issue_type="missing_package_definition",
            issue_subtype="no_package_rows",
            severity="medium", detection_method="cross_reference",
            entity_key=pn, entity_id=sha256(f"{vendor}|{pn}"),
            expected_or_hint=">=1 package row",
            observed_value=0,
        ))

    # required package fields should be present in at least one row per part
    for req in PACKAGE_REQUIRED_FIELDS:
        exists, actual = ensure_col(df, req)
        if not exists:
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="dataset",
                issue_type="missing_column",
                issue_subtype="package_required_column_absent",
                severity="high", detection_method="schema_check",
                field=req, expected_or_hint="required for package completeness"
            ))
            continue

        for pn, g in df.groupby("_pn", dropna=True):
            if pn is None:
                continue
            if blank_or_null(g[actual]).all():
                issues.append(record_issue(
                    vendor=vendor, tab=tab, scope="entity",
                    issue_type="missing_package_definition",
                    issue_subtype="required_package_field_missing",
                    severity="medium", detection_method="group_coverage",
                    entity_key=pn, entity_id=sha256(f"{vendor}|{pn}"),
                    field=req, expected_or_hint="present in at least one package row",
                ))

    return issues


def assets_min_one_required_type(vendor: str, df_assets: pd.DataFrame, universe_parts: set) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    tab = TAB_DIGITAL_ASSETS

    part_col = find_col(df_assets, [PART_NUMBER_LOGICAL])
    if not part_col:
        issues.append(record_issue(
            vendor=vendor, tab=tab, scope="dataset",
            issue_type="missing_column", issue_subtype="part_number_missing",
            severity="high", detection_method="schema_check",
            field=PART_NUMBER_LOGICAL, expected_or_hint="required for assets checks"
        ))
        return issues

    # In your mapped.xlsx, Digital_Assets has "Representation" (great)
    type_col = find_col(df_assets, ["MediaType"])

    if not type_col:
        issues.append(record_issue(
            vendor=vendor, tab=tab, scope="dataset",
            issue_type="missing_column", issue_subtype="asset_type_missing",
            severity="medium", detection_method="schema_check",
            field="Representation", expected_or_hint="recommended to validate P04/P01/LIN"
        ))
        # fallback: treat any row as asset presence
        parts_with_assets = set(df_assets[part_col].apply(normalize_part_number).dropna().unique())
        missing = sorted([p for p in universe_parts if p not in parts_with_assets])
        for pn in missing:
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="entity",
                issue_type="missing_required_asset",
                issue_subtype="no_asset_rows",
                severity="medium", detection_method="cross_reference",
                entity_key=pn, entity_id=sha256(f"{vendor}|{pn}"),
                expected_or_hint=">=1 asset row",
                observed_value=0,
            ))
        return issues

    df = df_assets.copy()
    df["_pn"] = df[part_col].apply(normalize_part_number)
    df["_type"] = df[type_col].astype(str).str.strip().str.upper()

    image_mask = (
        df["_type"].isin(REQUIRED_ASSET_TYPES)
        & df["FileType"].astype(str).str.upper().isin({"JPG", "JPEG", "PNG", "GIF"})
    )

    parts_with_required = set(
        df.loc[image_mask, "_pn"].dropna().unique()
    )


    missing = sorted([p for p in universe_parts if p not in parts_with_required])
    for pn in missing:
        issues.append(record_issue(
            vendor=vendor, tab=tab, scope="entity",
            issue_type="missing_required_asset",
            issue_subtype="no_required_image_type",
            severity="medium", detection_method="cross_reference",
            entity_key=pn, entity_id=sha256(f"{vendor}|{pn}"),
            field="Representation",
            expected_or_hint=sorted(REQUIRED_ASSET_TYPES),
            observed_value=0,
        ))
    return issues


# =========================================================
# Cross-reference checks
# =========================================================
def cross_reference_checks(vendor: str, item_parts: set, pricing_parts: set, asset_parts: set) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    tab = "CROSS_REFERENCE"

    for pn in sorted(item_parts - pricing_parts):
        issues.append(record_issue(
            vendor=vendor, tab=tab, scope="entity",
            issue_type="orphan_entity", issue_subtype="item_without_pricing",
            severity="high", detection_method="set_diff",
            entity_key=pn, entity_id=sha256(f"{vendor}|{pn}"),
            expected_or_hint="present in Pricing", observed_value="missing"
        ))
    for pn in sorted(pricing_parts - item_parts):
        issues.append(record_issue(
            vendor=vendor, tab=tab, scope="entity",
            issue_type="orphan_entity", issue_subtype="pricing_without_item",
            severity="high", detection_method="set_diff",
            entity_key=pn, entity_id=sha256(f"{vendor}|{pn}"),
            expected_or_hint="present in Item_Master", observed_value="missing"
        ))
    for pn in sorted(item_parts - asset_parts):
        issues.append(record_issue(
            vendor=vendor, tab=tab, scope="entity",
            issue_type="orphan_entity", issue_subtype="item_without_assets",
            severity="medium", detection_method="set_diff",
            entity_key=pn, entity_id=sha256(f"{vendor}|{pn}"),
            expected_or_hint="present in Digital_Assets", observed_value="missing"
        ))
    for pn in sorted(asset_parts - item_parts):
        issues.append(record_issue(
            vendor=vendor, tab=tab, scope="entity",
            issue_type="orphan_entity", issue_subtype="assets_without_item",
            severity="medium", detection_method="set_diff",
            entity_key=pn, entity_id=sha256(f"{vendor}|{pn}"),
            expected_or_hint="present in Item_Master", observed_value="missing"
        ))

    return issues


# =========================================================
# Row/Column missingness (informational)
# =========================================================
def compute_missingness_by_row(vendor: str, tab: str, df: pd.DataFrame) -> pd.DataFrame:
    core = df.copy()
    cols = [c for c in core.columns if not str(c).startswith("_")]
    if not cols:
        ratio = pd.Series([0.0] * len(core))
    else:
        miss = core[cols].isna()
        for c in cols:
            if core[c].dtype == object or str(core[c].dtype).startswith("string"):
                miss[c] = miss[c] | (core[c].astype(str).str.strip() == "")
        ratio = miss.mean(axis=1)

    return pd.DataFrame({
        "_vendor": vendor,
        "_tab": tab,
        "_row_id": core.get("_row_id"),
        "_entity_key": core.get("_entity_key"),
        "_missing_ratio": ratio.astype(float),
    })


def compute_missingness_by_col(vendor: str, tab: str, df: pd.DataFrame) -> pd.DataFrame:
    core = df.copy()
    cols = [c for c in core.columns if not str(c).startswith("_")]
    rows = []
    if cols:
        miss = core[cols].isna()
        for c in cols:
            if core[c].dtype == object or str(core[c].dtype).startswith("string"):
                miss[c] = miss[c] | (core[c].astype(str).str.strip() == "")
        for c in cols:
            rows.append({
                "_vendor": vendor,
                "_tab": tab,
                "_column": c,
                "_missing_ratio": float(miss[c].mean()),
            })
    return pd.DataFrame(rows)


# =========================================================
# Row-level anomaly checks (kept informational)
# =========================================================
def conversion_candidate_checks(vendor: str, tab: str, df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []

    # Weight UOM ---------------------------------
    exists, col = ensure_col(df, "Weight UOM")
    if exists:
        s = df[col].astype(str).str.strip().str.upper()
        # Only flag if unit is in our known conversion set AND not already KG
        mask = s.isin(KNOWN_WEIGHT_UOMS) & (s != "KG")

        for _, row in df[mask].iterrows():
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="row",
                issue_type="conversion_candidate", issue_subtype="weight_unit",
                severity="medium", detection_method="direct_compare",
                row_id=row.get("_row_id"), entity_key=row.get("_entity_key"), entity_id=row.get("_entity_id"),
                field="Weight UOM", observed_value=row.get(col), expected_or_hint="KG",
                fixable_by_code=True, confidence=1.0
            ))

    # Dimension UOM -------------------------------
    exists, col = ensure_col(df, "Dimension UOM")
    if exists:
        s = df[col].astype(str).str.strip().str.upper()
        mask = s.isin(KNOWN_DIM_UOMS) & (s != "CM")

        for _, row in df[mask].iterrows():
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="row",
                issue_type="conversion_candidate", issue_subtype="dimension_unit",
                severity="medium", detection_method="direct_compare",
                row_id=row.get("_row_id"), entity_key=row.get("_entity_key"), entity_id=row.get("_entity_id"),
                field="Dimension UOM", observed_value=row.get(col), expected_or_hint="CM",
                fixable_by_code=True, confidence=1.0
            ))

    # Currency ------------------------------------
    exists, col = ensure_col(df, "Currency")
    if exists:
        s = df[col].astype(str).str.strip().str.upper()
        mask = s.isin(KNOWN_CURRENCY) & (s != "CAD")

        for _, row in df[mask].iterrows():
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="row",
                issue_type="conversion_candidate", issue_subtype="currency",
                severity="medium", detection_method="direct_compare",
                row_id=row.get("_row_id"), entity_key=row.get("_entity_key"), entity_id=row.get("_entity_id"),
                field="Currency", observed_value=row.get(col), expected_or_hint="CAD",
                fixable_by_code=True, confidence=1.0
            ))

    return issues



def string_normalization_checks(vendor: str, tab: str, df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    for field in STRING_NORMALIZATION_FIELDS:
        exists, actual = ensure_col(df, field)
        if not exists:
            continue
        s = df[actual]
        if not (s.dtype == object or str(s.dtype).startswith("string")):
            continue
        trimmed = s.astype(str).str.strip()
        mism = (~blank_or_null(s)) & (s.astype(str) != trimmed)
        for _, row in df[mism].iterrows():
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="row",
                issue_type="string_normalization_candidate", issue_subtype="whitespace_trim",
                severity="low", detection_method="string_heuristic",
                row_id=row.get("_row_id"), entity_key=row.get("_entity_key"), entity_id=row.get("_entity_id"),
                field=field, observed_value=row.get(actual), expected_or_hint=str(row.get(actual)).strip(),
                fixable_by_code=True, confidence=0.9
            ))
    return issues


def date_format_checks(vendor: str, tab: str, df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    for field in DATE_FIELDS:
        exists, actual = ensure_col(df, field)
        if not exists:
            continue

        raw = df[actual]
        parsed = pd.to_datetime(raw, errors="coerce")
        bad = (~blank_or_null(raw)) & (parsed.isna())
        for _, row in df[bad].iterrows():
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="row",
                issue_type="invalid_date_format", issue_subtype="unparseable_date",
                severity="medium", detection_method="to_datetime",
                row_id=row.get("_row_id"), entity_key=row.get("_entity_key"), entity_id=row.get("_entity_id"),
                field=field, observed_value=row.get(actual), expected_or_hint="parseable date",
                fixable_by_code=False, confidence=1.0
            ))

    # Start Date > End Date (if both exist on this tab)
    start_exists, start_col = ensure_col(df, "Start Date")
    end_exists, end_col = ensure_col(df, "End Date")
    if start_exists and end_exists:
        start_p = pd.to_datetime(df[start_col], errors="coerce")
        end_p = pd.to_datetime(df[end_col], errors="coerce")
        invalid = (~start_p.isna()) & (~end_p.isna()) & (start_p > end_p)
        for _, row in df[invalid].iterrows():
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="row",
                issue_type="start_date_after_end_date", issue_subtype="date_order_invalid",
                severity="medium", detection_method="date_compare",
                row_id=row.get("_row_id"), entity_key=row.get("_entity_key"), entity_id=row.get("_entity_id"),
                field="Start Date / End Date",
                observed_value=f"{row.get(start_col)} > {row.get(end_col)}",
                expected_or_hint="Start Date <= End Date",
                fixable_by_code=False, confidence=1.0
            ))
    return issues


def numeric_sanity_checks(vendor: str, tab: str, df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    numeric_fields = [
        "Net Price", "List Price", "Dealer Price", "Jobber Price",
        "Minimum Order Quantity", "MOQ", "Weight",
        "Package Quantity of Eaches", "Ship Length", "Ship Width", "Ship Height",
    ]
    for field in numeric_fields:
        exists, actual = ensure_col(df, field)
        if not exists:
            continue
        raw = df[actual]
        coerced = pd.to_numeric(raw, errors="coerce")

        mismatch = (~blank_or_null(raw)) & (coerced.isna())
        for _, row in df[mismatch].iterrows():
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="row",
                issue_type="datatype_mismatch", issue_subtype="expected_numeric",
                severity="medium", detection_method="to_numeric",
                row_id=row.get("_row_id"), entity_key=row.get("_entity_key"), entity_id=row.get("_entity_id"),
                field=field, observed_value=str(row.get(actual)), expected_or_hint="numeric",
                fixable_by_code=False, confidence=1.0
            ))

        neg = coerced < 0
        for _, row in df[neg.fillna(False)].iterrows():
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="row",
                issue_type="negative_value", issue_subtype="numeric_lt_zero",
                severity="medium", detection_method="numeric_rule",
                row_id=row.get("_row_id"), entity_key=row.get("_entity_key"), entity_id=row.get("_entity_id"),
                field=field, observed_value=str(row.get(actual)), expected_or_hint=">= 0",
                fixable_by_code=False, confidence=1.0
            ))

        if field in {"Net Price", "Weight", "Minimum Order Quantity", "MOQ", "Package Quantity of Eaches"}:
            zero = coerced == 0
            for _, row in df[zero.fillna(False)].iterrows():
                issues.append(record_issue(
                    vendor=vendor, tab=tab, scope="row",
                    issue_type="suspicious_zero_value", issue_subtype="numeric_eq_zero",
                    severity="low", detection_method="numeric_rule",
                    row_id=row.get("_row_id"), entity_key=row.get("_entity_key"), entity_id=row.get("_entity_id"),
                    field=field, observed_value=str(row.get(actual)), expected_or_hint="non-zero expected",
                    fixable_by_code=False, confidence=0.7
                ))
    return issues


def duplicate_checks(vendor: str, tab: str, df: pd.DataFrame) -> List[Dict[str, Any]]:
     # Duplicates only meaningful for master-like tabs
    if tab not in {TAB_ITEM_MASTER}:
        return []
    issues: List[Dict[str, Any]] = []
    part_col = find_col(df, [PART_NUMBER_LOGICAL])
    if not part_col:
        return issues

    parts = df[part_col].apply(normalize_part_number)
    dup = parts.duplicated(keep=False) & parts.notna()
    for _, row in df[dup].iterrows():
        issues.append(record_issue(
            vendor=vendor, tab=tab, scope="row",
            issue_type="duplicate_entity", issue_subtype="duplicate_part_number_in_tab",
            severity="low", detection_method="duplicated",
            row_id=row.get("_row_id"), entity_key=row.get("_entity_key"), entity_id=row.get("_entity_id"),
            field=PART_NUMBER_LOGICAL, observed_value=observed_entity(row),
            expected_or_hint="unique per tab (if intended)", fixable_by_code=False, confidence=0.6
        ))
    return issues


def outlier_iqr_checks(vendor: str, tab: str, df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []

    # compute Shipping Volume if dims exist
    Lc = find_col(df, ["Ship Length"])
    Wc = find_col(df, ["Ship Width"])
    Hc = find_col(df, ["Ship Height"])
    df2 = df
    if Lc and Wc and Hc:
        L = pd.to_numeric(df[Lc], errors="coerce")
        W = pd.to_numeric(df[Wc], errors="coerce")
        H = pd.to_numeric(df[Hc], errors="coerce")
        df2 = df.copy()
        df2["Shipping Volume"] = L * W * H

    for field, cfg in OUTLIER_FIELDS.items():
        exists, actual = ensure_col(df2, field)
        if not exists:
            continue
        s = pd.to_numeric(df2[actual], errors="coerce")
        if s.dropna().empty:
            continue
        q1, q3 = s.quantile([0.25, 0.75])
        iqr = q3 - q1
        if pd.isna(iqr) or iqr == 0:
            continue
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        out = (s < lower) | (s > upper)
        for _, row in df2[out.fillna(False)].iterrows():
            issues.append(record_issue(
                vendor=vendor, tab=tab, scope="row",
                issue_type="outlier_iqr", issue_subtype="iqr_outlier",
                severity=cfg["severity"], detection_method="iqr",
                row_id=row.get("_row_id"), entity_key=row.get("_entity_key"), entity_id=row.get("_entity_id"),
                field=field, observed_value=str(row.get(actual)),
                expected_or_hint=f"{lower:.4g} – {upper:.4g}",
                fixable_by_code=False, confidence=0.8
            ))
    return issues


# =========================================================
# Statistics (per tab)
# =========================================================
def compute_statistics(vendor: str, tab: str, df: pd.DataFrame) -> Dict[str, Any]:
    core = df.copy()
    cols = [c for c in core.columns if not str(c).startswith("_")]
    stats: Dict[str, Any] = {"vendor": vendor, "tab": tab, "generated_at": datetime.utcnow().isoformat(), "numeric": {}}
    for c in cols:
        s = pd.to_numeric(core[c], errors="coerce")
        if s.dropna().empty:
            continue
        skew = s.skew()
        stats["numeric"][c] = {
            "min": float(s.min()),
            "max": float(s.max()),
            "mean": float(s.mean()),
            "median": float(s.median()),
            "skewness": float(skew) if np.isfinite(skew) else None,
            "count": int(s.count()),
        }
    return stats


# =========================================================
# Load workbook
# =========================================================
def load_mapped_workbook_from_azure(container, vendor: str, workflow:str, submission_type: str, submission_id: str):
    meta = parse_submission_type(submission_type)

    if meta["is_review"]:
        root = PRICING_REVIEW_ROOT
    else:
        root = IN_REVIEW_ROOT

    base = (
        f"{root}/{workflow}_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}/"
        f"{MAPPED_DIRNAME}"
    )
    xlsx_blob = f"{base}/mapped.xlsx"
    pq_blob = f"{base}/mapped.parquet"

    # prefer xlsx
    try:
        raw = download_blob_bytes(container, xlsx_blob)
        wb_all = pd.read_excel(BytesIO(raw), sheet_name=None, dtype=str)
        
        meta = parse_submission_type(submission_type)

        if meta["is_review"]:
            wb = {
                name: df
                for name, df in wb_all.items()
                if name in ALLOWED_SHEETS
            }

            dropped = set(wb_all.keys()) - set(wb.keys())
            if dropped:
                print(f"🧹 Post-review mode: dropping non-business sheets: {sorted(dropped)}")
                #DEBUG
            print("DEBUG RAW PARTS BEFORE LINEAGE:")
            for name, df in wb_all.items():
                if "Part Number" in df.columns:
                    print(name, df["Part Number"].head(5).tolist())

        else:
            # Full mode (vendor raw upload)
            wb = wb_all

        return wb, "mapped.xlsx"
    except Exception:
        raw = download_blob_bytes(container, pq_blob)
        df = df_from_parquet_bytes(raw)
        return {"mapped": df}, "mapped.parquet"


def load_mapped_workbook_local(local_xlsx: str, mode: str) -> Tuple[Dict[str, pd.DataFrame], str]:
    wb_all = pd.read_excel(local_xlsx, sheet_name=None, dtype=str)
    meta = parse_submission_type(submission_type)
    if meta["is_review"]:
        wb = {
            name: df
            for name, df in wb_all.items()
            if name in ALLOWED_SHEETS
        }
    else:
        wb = wb_all

    return wb, os.path.basename(local_xlsx)


# =========================================================
# Vendor profiling
# =========================================================
def profile_vendor(vendor: str, sheets: Dict[str, pd.DataFrame], source_file: str) -> Tuple[
    pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, Any]
]:
    issues: List[Dict[str, Any]] = []
    row_missing_all: List[pd.DataFrame] = []
    col_missing_all: List[pd.DataFrame] = []
    stats_all: List[Dict[str, Any]] = []

    # Add lineage/ids
    profiled: Dict[str, pd.DataFrame] = {}
    for sheet_name, df in sheets.items():
        if df is None or df.empty:
            continue
        df2 = add_lineage_and_ids(df, vendor, source_file, sheet_name)
        profiled[sheet_name] = df2

        # missingness + stats
        row_missing_all.append(compute_missingness_by_row(vendor, sheet_name, df2))
        col_missing_all.append(compute_missingness_by_col(vendor, sheet_name, df2))
        stats_all.append(compute_statistics(vendor, sheet_name, df2))

        # ENTITY-FIRST required fields
        issues += entity_required_field_checks(vendor, sheet_name, df2)

        # row-level informational checks
        issues += conversion_candidate_checks(vendor, sheet_name, df2)
        issues += string_normalization_checks(vendor, sheet_name, df2)
        issues += date_format_checks(vendor, sheet_name, df2)
        issues += numeric_sanity_checks(vendor, sheet_name, df2)
        issues += duplicate_checks(vendor, sheet_name, df2)
        issues += outlier_iqr_checks(vendor, sheet_name, df2)

    # Cross-tab + completeness rules use Item_Master as universe if present
    item_df = profiled.get(TAB_ITEM_MASTER)
    pricing_df = profiled.get(TAB_PRICING)
    assets_df = profiled.get(TAB_DIGITAL_ASSETS)

    item_parts = parts_set(item_df)
    pricing_parts = parts_set(pricing_df)
    asset_parts = parts_set(assets_df)

    # Cross-reference checks (only if tabs present)
    if item_df is not None and pricing_df is not None and assets_df is not None:
        issues += cross_reference_checks(vendor, item_parts, pricing_parts, asset_parts)
    else:
        # record missing sheets as dataset issues (informational)
        if item_df is None:
            issues.append(record_issue(
                vendor=vendor, tab="CROSS_REFERENCE", scope="dataset",
                issue_type="missing_sheet", issue_subtype="item_master_missing",
                severity="high", detection_method="sheet_presence",
                expected_or_hint="Item_Master required for cross-reference checks"
            ))
        if pricing_df is None:
            issues.append(record_issue(
                vendor=vendor, tab="CROSS_REFERENCE", scope="dataset",
                issue_type="missing_sheet", issue_subtype="pricing_missing",
                severity="high", detection_method="sheet_presence",
                expected_or_hint="Pricing required for items↔pricing checks"
            ))
        if assets_df is None:
            issues.append(record_issue(
                vendor=vendor, tab="CROSS_REFERENCE", scope="dataset",
                issue_type="missing_sheet", issue_subtype="digital_assets_missing",
                severity="medium", detection_method="sheet_presence",
                expected_or_hint="Digital_Assets required for items↔assets checks"
            ))

    # "At least one ..." rules
    if profiled.get(TAB_DESCRIPTIONS) is not None and item_parts:
        issues += descriptions_code_coverage(vendor, profiled[TAB_DESCRIPTIONS])

    if profiled.get(TAB_EXTENDED_INFO) is not None and item_parts:
        issues += extended_info_code_coverage(vendor, profiled[TAB_EXTENDED_INFO])

    if profiled.get(TAB_ATTRIBUTES) is not None and item_parts:
        issues += attributes_min_one(vendor, profiled[TAB_ATTRIBUTES], item_parts)

    if profiled.get(TAB_PACKAGES) is not None and item_parts:
        issues += packages_min_one_and_fields(vendor, profiled[TAB_PACKAGES], item_parts)

    if profiled.get(TAB_DIGITAL_ASSETS) is not None and item_parts:
        issues += assets_min_one_required_type(vendor, profiled[TAB_DIGITAL_ASSETS], item_parts)

    # Build outputs
    issues_df = pd.DataFrame(issues)

    # 🔒 Ensure stable schema BEFORE any operations
    if issues_df.empty:
        issues_df = pd.DataFrame(columns=ISSUE_COLUMNS)
    else:
        for col in ISSUE_COLUMNS:
            if col not in issues_df.columns:
                issues_df[col] = None

    # Enforce severity downgrade for fixable issues (safe now)
    if "_fixable_by_code" in issues_df.columns:
        issues_df.loc[issues_df["_fixable_by_code"] == True, "_severity"] = "low"

    issues_df = sanitize_issue_df_for_parquet(issues_df)


    row_missing_df = pd.concat(row_missing_all, ignore_index=True) if row_missing_all else pd.DataFrame(
        columns=["_vendor", "_tab", "_row_id", "_entity_key", "_missing_ratio"]
    )
    col_missing_df = pd.concat(col_missing_all, ignore_index=True) if col_missing_all else pd.DataFrame(
        columns=["_vendor", "_tab", "_column", "_missing_ratio"]
    )

    entity_rows = [
        {"_vendor": vendor, "_metric": "item_master_parts", "_count": int(len(item_parts))},
        {"_vendor": vendor, "_metric": "pricing_parts", "_count": int(len(pricing_parts))},
        {"_vendor": vendor, "_metric": "digital_assets_parts", "_count": int(len(asset_parts))},
    ]
    entity_comp_df = pd.DataFrame(entity_rows)

    stats_payload = {
        "vendor": vendor,
        "generated_at": datetime.utcnow().isoformat(),
        "tabs": stats_all,
    }

    # Summary
    summary = {
        "vendor": vendor,
        "health_checked_at": datetime.utcnow().isoformat(),
        "tabs_scanned": sorted(list(profiled.keys())),
        "issues_total": int(len(issues_df)),
        "issues_by_type": issues_df["_issue_type"].value_counts(dropna=False).to_dict() if not issues_df.empty else {},
        "issues_by_severity": issues_df["_severity"].value_counts(dropna=False).to_dict() if not issues_df.empty else {},
        "issues_by_scope": issues_df["_scope"].value_counts(dropna=False).to_dict() if not issues_df.empty else {},
        "fixable_flagged": int((issues_df["_fixable_by_code"] == True).sum()) if "_fixable_by_code" in issues_df.columns else 0,  # noqa: E712
        "row_missingness_avg": float(row_missing_df["_missing_ratio"].mean()) if (not row_missing_df.empty and "_missing_ratio" in row_missing_df.columns) else 0.0,
        "notes": "Entity-first health check only; no rejection, vendor feedback, or promotion performed.",
    }

    out_payload = {"summary": summary, "statistics": stats_payload}
    return issues_df, row_missing_df, col_missing_df, entity_comp_df, out_payload


# =========================================================
# Writers
# =========================================================
def write_vendor_outputs(
    container,
    vendor: str,
    workflow:str,
    submission_type:str,
    submission_id: str,
    issues_df: pd.DataFrame,
    row_missing_df: pd.DataFrame,
    col_missing_df: pd.DataFrame,
    entity_comp_df: pd.DataFrame,
    payload: Dict[str, Any],
) -> None:
    
    meta = parse_submission_type(submission_type)

    if meta["is_review"]:
        root = PRICING_REVIEW_ROOT
    else:
        root = IN_REVIEW_ROOT

    out_base = (
        f"{root}/{workflow}_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}/"
        f"{OUT_DIRNAME}"
    )

    # Parquet outputs
    upload_blob_bytes(container, f"{out_base}/health_issues.parquet", parquet_bytes_from_df(issues_df))
    upload_blob_bytes(container, f"{out_base}/row_missingness.parquet", parquet_bytes_from_df(row_missing_df))
    upload_blob_bytes(container, f"{out_base}/column_missingness.parquet", parquet_bytes_from_df(col_missing_df))
    upload_blob_bytes(container, f"{out_base}/entity_completeness.parquet", parquet_bytes_from_df(entity_comp_df))

    # JSON outputs
    summary = payload["summary"]
    stats = payload["statistics"]
    upload_blob_bytes(container, f"{out_base}/health_summary.json", json.dumps(summary, indent=2).encode("utf-8"))
    upload_blob_bytes(container, f"{out_base}/statistics.json", json.dumps(stats, indent=2).encode("utf-8"))

    # Excel outputs (review-friendly)
    xlsx = excel_bytes_from_sheets({
        "health_issues": issues_df,
        "health_summary": pd.DataFrame([summary]),
        "row_missingness": row_missing_df,
        "column_missingness": col_missing_df,
        "entity_completeness": entity_comp_df,
    })
    upload_blob_bytes(container, f"{out_base}/health_issues.xlsx", xlsx)


# =========================================================
# Run modes
# =========================================================
def run_vendor_azure(container, vendor: str, workflow: str, submission_type: str, submission_id: str):
    print(f"\n🩺 Health check vendor: {vendor}")
    print(
        f"   ↳ input : {IN_REVIEW_ROOT}/{workflow}_workflow/"
        f"vendor={vendor}/submission_type={submission_type}/submission={submission_id}/mapped/mapped.xlsx"
    )

    sheets, source_file = load_mapped_workbook_from_azure(container, vendor, workflow, submission_type, submission_id)
    issues_df, row_missing_df, col_missing_df, entity_comp_df, payload = profile_vendor(vendor, sheets, source_file)
    write_vendor_outputs(container, vendor, workflow, submission_type, submission_id, issues_df, row_missing_df, col_missing_df, entity_comp_df, payload)

    print(f"✅ Done: {vendor} | issues={len(issues_df)} | tabs={len(sheets)}")


def run_vendor_local(local_xlsx: str, vendor: str) -> None:
    print(f"\n🩺 Local health check (pipeline layout): {local_xlsx}")

    # --------------------------------------------------
    # Load mapped workbook
    # --------------------------------------------------
    sheets, source_file = load_mapped_workbook_local(local_xlsx)

    issues_df, row_missing_df, col_missing_df, entity_comp_df, payload = profile_vendor(
        vendor, sheets, source_file
    )

    # --------------------------------------------------
    # Resolve pipeline-consistent output path
    # --------------------------------------------------
    vendor_root = local_vendor_root(vendor)
    out_dir = os.path.join(vendor_root, OUT_DIRNAME)
    os.makedirs(out_dir, exist_ok=True)

    # --------------------------------------------------
    # Parquet outputs (pipeline truth)
    # --------------------------------------------------
    issues_df.to_parquet(
        os.path.join(out_dir, "health_issues.parquet"),
        index=False
    )
    row_missing_df.to_parquet(
        os.path.join(out_dir, "row_missingness.parquet"),
        index=False
    )
    col_missing_df.to_parquet(
        os.path.join(out_dir, "column_missingness.parquet"),
        index=False
    )
    entity_comp_df.to_parquet(
        os.path.join(out_dir, "entity_completeness.parquet"),
        index=False
    )

    # --------------------------------------------------
    # JSON outputs
    # --------------------------------------------------
    with open(os.path.join(out_dir, "health_summary.json"), "w", encoding="utf-8") as f:
        json.dump(payload["summary"], f, indent=2)

    with open(os.path.join(out_dir, "statistics.json"), "w", encoding="utf-8") as f:
        json.dump(payload["statistics"], f, indent=2)

    # --------------------------------------------------
    # Excel output (review-friendly)
    # --------------------------------------------------
    xlsx = excel_bytes_from_sheets({
        "health_issues": issues_df,
        "health_summary": pd.DataFrame([payload["summary"]]),
        "row_missingness": row_missing_df,
        "column_missingness": col_missing_df,
        "entity_completeness": entity_comp_df,
    })

    with open(os.path.join(out_dir, "health_issues.xlsx"), "wb") as f:
        f.write(xlsx)

    print(
        f"✅ Local health check complete | vendor={vendor} "
        f"| issues={len(issues_df)} | tabs={len(sheets)}"
    )

# =========================================================
# External Pipeline Entry Point
# =========================================================

def run_health_check(vendor: str, workflow:str, submission_type: str, submission_id: str,  source: str = "full") -> None:
    """
    Entry point for other pipelines (e.g. post-review pipeline).
    source:
        "full"          → in_review
        "reviewed"      → post_pricing_review
    """

    # ✅ Define mode FIRST
    if source == "reviewed":
        mode = "post_review"
    else:
        mode = "full"

    print("MODE:", mode)
    meta = parse_submission_type(submission_type)

    print("ROOT:", PRICING_REVIEW_ROOT if meta["is_review"] else IN_REVIEW_ROOT)

    container = get_container()

    run_vendor_azure(
        container=container,
        vendor=vendor,
        workflow=workflow,
        submission_type = submission_type,
        submission_id=submission_id,
    )

# =========================================================
# CLI
# =========================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument("--vendor", type=str, help='Vendor name (required when using --local)')
    parser.add_argument("--submission-id", type=str, required=True)
    parser.add_argument("--all", action="store_true", help="Process all vendors with mapped outputs")
    parser.add_argument("--local", action="store_true", help="Run in local mode (auto-detect mapped.xlsx)")
    parser.add_argument("--local_xlsx", type=str, help="Explicit path to mapped.xlsx (local debug)")
    parser.add_argument("--submission-type", required=True)
    parser.add_argument("--workflow", required=True)

    args = parser.parse_args()
    submission_id = args.submission_id
    submission_type = args.submission_type

    print("🔥 USING data_health_check FROM:", __file__)


    # ---------------------------
    # LOCAL MODE (automatic path)
    # ---------------------------
    if args.local:
        if not args.vendor or not submission_id:
            raise SystemExit(" --vendor is required when using --local")

        # auto-detect mapped.xlsx
        local_path = find_local_mapped_xlsx(args.vendor)
        if not local_path:
            raise SystemExit(
                f" Auto-detected mapped.xlsx not found at:\n"
                f"   {local_vendor_root(args.vendor)}/mapped/mapped.xlsx\n"
                f"👉 If file is elsewhere, use: --local_xlsx <path>"
            )

        print(f"📄 Using local mapped workbook: {local_path}")
        print(f"📁 Writing profiling outputs to:\n   {local_vendor_root(args.vendor)}/profiling/")
        run_vendor_local(local_path, args.vendor)
        exit()

    # ---------------------------
    # LOCAL XLSX explicit path
    # ---------------------------
    if args.local_xlsx:
        if not args.vendor:
            raise SystemExit(" For --local_xlsx, also provide --vendor")
        print(f"📄 Using explicit local workbook: {args.local_xlsx}")
        run_vendor_local(args.local_xlsx, args.vendor)
        exit()

    # ---------------------------
    # AZURE MODE
    # ---------------------------
    container = get_container()

    if args.all:
        vendors = list_vendors_with_mapped(container)
        if not vendors:
            raise SystemExit("No vendors found with mapped outputs under in_review/")
        print(f"🔎 Running health check for {len(vendors)} vendors")
        for v in vendors:
            run_vendor_azure(container, v, args.workflow, submission_type, submission_id)
    else:
        if not args.vendor:
            raise SystemExit(" Provide --vendor and --submission_id")
        run_vendor_azure(container, args.vendor, args.workflow, submission_type, submission_id)

