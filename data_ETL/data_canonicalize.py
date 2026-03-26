"""
data_canonicalize.py

Purpose:
---------
Canonicalizes vendor product data into ERP/PIM-ready structures while
enforcing product identity and logging all business-impacting changes
for downstream human review.

Supports:
---------
- Azure mode (default)
- Local mode (--local)

Canonical output contract (BOTH modes):
--------------------------------------
silver/in_review/vendor=<vendor>/submission=<submission_id>/canonical/
"""

import os
import json
import hashlib
from io import BytesIO
from datetime import datetime
from typing import List, Dict

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

AUTOFIX_DIRNAME = "autofix"
CANONICAL_DIRNAME = "canonical"
REVIEW_DIRNAME = "review"

SYSTEM_COL_PREFIX = "_"

# LOCAL PROJECT ROOT = ETL/data_etl
PROJECT_ROOT = os.path.abspath(
    os.path.dirname(__file__)
)

def parse_submission_type(submission_type: str):
    return {
        "is_review": "review" in submission_type,
        "is_delta": "delta" in submission_type,
        "workflow": "pricing" if "pricing" in submission_type else "product"
    }

#shared logic
def enforce_identifier_types(df: pd.DataFrame) -> pd.DataFrame:
    """
    Canonical safety net:
    Identifiers must ALWAYS be strings.
    """
    df = df.copy()

    IDENTIFIER_COLS = {
        "Part Number",
        "SKU",
        "UPC",
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
# IO RESOLUTION
# =========================================================
def local_vendor_root(vendor: str, workflow: str, submission_type: str, submission_id: str) -> str:
    meta = parse_submission_type(submission_type)
    root = IN_REVIEW_ROOT

    return os.path.join(
        PROJECT_ROOT,
        "silver",
        root,
        f"{workflow}_workflow",
        f"vendor={vendor}",
        f"submission_type={submission_type}",
        f"submission={submission_id}",
    )


def get_container():
    if not AZURE_CONN_STR:
        raise RuntimeError("Missing AZURE_STORAGE_CONNECTION_STRING")
    return BlobServiceClient.from_connection_string(
        AZURE_CONN_STR
    ).get_container_client(SILVER_CONTAINER)


# =========================================================
# IO HELPERS
# =========================================================
def read_parquet_bytes(data: bytes) -> pd.DataFrame:
    return pq.read_table(BytesIO(data)).to_pandas()


def parquet_bytes(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    pq.write_table(pa.Table.from_pandas(df), buf)
    return buf.getvalue()


def write_local_parquet(path: str, df: pd.DataFrame):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_parquet(path, index=False)


def write_local_bytes(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


#Vendor Discovery Helpers

def list_vendors_local(workflow: str, submission_type: str, submission_id: str) -> List[str]:
    meta = parse_submission_type(submission_type)
    root_name = IN_REVIEW_ROOT

    root = os.path.join(
        PROJECT_ROOT,
        "silver",
        root_name,
        f"{workflow}_workflow",
    )

    if not os.path.exists(root):
        return []

    vendors = []
    for d in os.listdir(root):
        if d.startswith("vendor="):
            check_path = os.path.join(
                root,
                d,
                f"submission_type={submission_type}",
                f"submission={submission_id}",
                AUTOFIX_DIRNAME,
                "data_autofixed.parquet"
            )
            if os.path.exists(check_path):
                vendors.append(d.split("vendor=", 1)[1])

    return sorted(vendors)


def list_vendors_azure(container, workflow: str, submission_type: str, submission_id: str) -> List[str]:
    meta = parse_submission_type(submission_type)
    root = IN_REVIEW_ROOT

    prefix = f"{root}/{workflow}_workflow/"

    vendors = set()
    for blob in container.list_blobs(name_starts_with=prefix):
        expected = (
            f"/submission_type={submission_type}/"
            f"submission={submission_id}/"
            f"{AUTOFIX_DIRNAME}/data_autofixed.parquet"
        )
        if expected in blob.name and "vendor=" in blob.name:
            v = blob.name.split("vendor=", 1)[1].split("/", 1)[0]
            vendors.add(v)

    return sorted(vendors)



# =========================================================
# IDENTITY & UTILS
# =========================================================
IDENTITY_LINEAGE_COLS = [
    "_vendor", "_row_id", "_entity_id",
    "_source_file", "_source_sheet", "_source_row_number",
    "_ingestion_run_id", "_ingestion_timestamp",
    "_autofix_run_id", "_autofix_timestamp",
]


def identity_cols(df: pd.DataFrame) -> List[str]:
    return [c for c in IDENTITY_LINEAGE_COLS if c in df.columns]


def df_hash(df: pd.DataFrame) -> str:
    return hashlib.sha256(
        pd.util.hash_pandas_object(df, index=True).values.tobytes()
    ).hexdigest()


def assert_valid_identity(df: pd.DataFrame, vendor: str):
    if "_entity_id" not in df.columns:
        raise RuntimeError(f"_entity_id missing for vendor {vendor}")
    if df["_entity_id"].isna().any():
        raise RuntimeError(f"_entity_id contains NULLs for vendor {vendor}")
    if df["_entity_id"].nunique() == 0:
        raise RuntimeError(f"No valid _entity_id values for vendor {vendor}")


# =========================================================
# CANONICAL BUILDERS
# =========================================================
ITEM_MASTER_CANDIDATES = [
    "Part Number",
    "SKU",
    "Brand Label",
    "Description",
    "PartTerminologyID",
    "HazmatFlag",
    "Barcode Type",
    "UNSPSC",
    "Barcode Number",
    "Barcode UOM",
    "Quantity UOM",
    "Quantity Size",
    "Minimum Order Quantity UOM",
    "Minimum Order Quantity",
    "VMRS Code",
    "Product Status",
    "Category"
]


def build_item_master(df: pd.DataFrame) -> pd.DataFrame:
    cols = list(dict.fromkeys(identity_cols(df) + ITEM_MASTER_CANDIDATES))
    item = df[[c for c in cols if c in df.columns]].copy()
    return item.drop_duplicates(subset=["_entity_id"])


def build_pricing(df: pd.DataFrame) -> pd.DataFrame:
    if "Net Price" not in df.columns:
        return df[identity_cols(df)].iloc[0:0].copy()

    pricing = df[identity_cols(df) + ["Net Price"]].copy()
    pricing["_pricing_source"] = pricing["Net Price"].notna().map(
        lambda x: "vendor" if x else "missing"
    )
    return pricing[pricing["_pricing_source"] == "vendor"].drop_duplicates()


def build_attributes_longform(
    df: pd.DataFrame,
    item_cols: List[str],
    pricing_cols: List[str]
) -> pd.DataFrame:

    exclude = set(identity_cols(df)) | set(item_cols) | set(pricing_cols)
    exclude |= {c for c in df.columns if c.startswith(SYSTEM_COL_PREFIX)}

    attr_cols = [c for c in df.columns if c not in exclude]

    if not attr_cols:
        return pd.DataFrame(
            columns=identity_cols(df) + ["attribute_name", "attribute_value"]
        )

    melted = df[identity_cols(df) + attr_cols].melt(
        id_vars=identity_cols(df),
        value_vars=attr_cols,
        var_name="attribute_name",
        value_name="attribute_value",
    )

    melted = melted.dropna()
    melted["attribute_value"] = melted["attribute_value"].astype(str)
    return melted[melted["attribute_value"].str.strip() != ""].drop_duplicates()


# =========================================================
# SERIALIZATION
# =========================================================
def excel_bytes(item, pricing, attrs, summary) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        item.to_excel(writer, index=False, sheet_name="item_master")
        pricing.to_excel(writer, index=False, sheet_name="pricing")
        attrs.to_excel(writer, index=False, sheet_name="attributes")
        pd.DataFrame([summary]).to_excel(writer, index=False, sheet_name="summary")
    return buf.getvalue()


# =========================================================
# RUNNER
# =========================================================
def canonicalize_vendor(
        vendor: str,
        workflow: str,
        submission_type: str,
        submission_id: str,
        local: bool,
    ):
    print(f"\n🧱 Canonicalizing vendor: {vendor}")
    if local:
        print("📁 Mode      : LOCAL")
        print(f"📁 PROJECT_ROOT = {PROJECT_ROOT}")
        print(f"📁 Vendor root  = {local_vendor_root(vendor, workflow, submission_type, submission_id)}")
    else:
        print("☁️ Mode      : AZURE")

    if local:
        root = local_vendor_root(vendor, workflow, submission_type, submission_id)
        in_path = os.path.join(root, AUTOFIX_DIRNAME, "data_autofixed.parquet")
        df = pd.read_parquet(in_path)
    else:
        container = get_container()
        meta = parse_submission_type(submission_type)
        root = IN_REVIEW_ROOT

        blob_path = (
            f"{root}/{workflow}_workflow/"
            f"vendor={vendor}/"
            f"submission_type={submission_type}/"
            f"submission={submission_id}/"
            f"{AUTOFIX_DIRNAME}/data_autofixed.parquet"
        )

        df = read_parquet_bytes(
            container.get_blob_client(blob_path).download_blob().readall()
        )

    df = enforce_identifier_types(df)
    assert_valid_identity(df, vendor)

    item = build_item_master(df)
    if workflow == "products":
        print("[CANONICAL] Skipping pricing (product workflow)")
        pricing = pd.DataFrame(columns=identity_cols(df) + ["Net Price"])
    else:
        pricing = build_pricing(df)
    if workflow == "products":
        attrs = build_attributes_longform(df, list(item.columns), [])
    else:
        attrs = build_attributes_longform(df, list(item.columns), list(pricing.columns))

    summary = {
        "vendor": vendor,
        "canonicalized_at": datetime.utcnow().isoformat(),
        "input_rows": len(df),
        "unique_products": df["_entity_id"].nunique(),
        "item_master_rows": len(item),
        "pricing_rows": len(pricing) if workflow != "products" else 0,
        "attributes_rows": len(attrs),
        "hashes": {
            "item_master": df_hash(item),
            "pricing": df_hash(pricing),
            "attributes": df_hash(attrs),
        },
    }

    if local:
        root_path = local_vendor_root(vendor, workflow, submission_type, submission_id)

        base = os.path.join(root_path, CANONICAL_DIRNAME)
        review = os.path.join(root_path, REVIEW_DIRNAME)


        write_local_parquet(f"{base}/item_master_canonical.parquet", item)
        write_local_parquet(f"{base}/pricing_canonical.parquet", pricing)
        write_local_parquet(f"{base}/attributes_canonical.parquet", attrs)
        write_local_bytes(f"{base}/data_canonical.xlsx",
                          excel_bytes(item, pricing, attrs, summary))
        write_local_bytes(f"{base}/canonical_summary.json",
                          json.dumps(summary, indent=2).encode())
        write_local_parquet(f"{review}/review_changes.parquet", pd.DataFrame())
    else:
        root = IN_REVIEW_ROOT

        base = (
            f"{root}/{workflow}_workflow/"
            f"vendor={vendor}/"
            f"submission_type={submission_type}/"
            f"submission={submission_id}/"
            f"{CANONICAL_DIRNAME}"
        )

        review = (
            f"{root}/{workflow}_workflow/"
            f"vendor={vendor}/"
            f"submission_type={submission_type}/"
            f"submission={submission_id}/"
            f"{REVIEW_DIRNAME}"
        )

        container = get_container()
        container.upload_blob(f"{base}/item_master_canonical.parquet",
                              parquet_bytes(item), overwrite=True)
        container.upload_blob(f"{base}/pricing_canonical.parquet",
                              parquet_bytes(pricing), overwrite=True)
        container.upload_blob(f"{base}/attributes_canonical.parquet",
                              parquet_bytes(attrs), overwrite=True)
        container.upload_blob(f"{base}/data_canonical.xlsx",
                              excel_bytes(item, pricing, attrs, summary), overwrite=True)
        container.upload_blob(f"{base}/canonical_summary.json",
                              json.dumps(summary, indent=2).encode(), overwrite=True)
        container.upload_blob(f"{review}/review_changes.parquet",
                              parquet_bytes(pd.DataFrame()), overwrite=True)

    print(f"✅ Done: {vendor} | items={len(item)} pricing={len(pricing)} attrs={len(attrs)}")


# =========================================================
# External Pipeline Entry Point
# =========================================================

def run_data_canonicalize(
        vendor: str,
        workflow: str,
        submission_type: str,
        submission_id: str
    ) -> None:
    """
    Entry point for other pipelines (e.g. post-review pipeline).
    source:
        "full"      → in_review
        "reviewed"  → post_pricing_review
    """

    canonicalize_vendor(
        vendor=vendor,
        workflow=workflow,
        submission_type=submission_type,
        submission_id=submission_id,
        local=False,
    )


# =========================================================
# CLI
# =========================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", type=str, help="Vendor name")
    parser.add_argument("--submission-id", type=str, required=False)
    parser.add_argument("--all", action="store_true", help="Run for all vendors")
    parser.add_argument("--local", action="store_true", help="Run in local mode")
    parser.add_argument("--mode", default="full")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--submission-type", dest="submission_type", required=True)

    args = parser.parse_args()
    submission_id = args.submission_id
    submission_type = args.submission_type

    if not submission_id:
        raise SystemExit("Provide --submission-id")

    if not args.workflow:
        raise SystemExit("Provide --workflow")

    if not submission_type:
        raise SystemExit("Provide --submission-type")
  
    # -------------------------
    # LOCAL MODE
    # -------------------------
    if args.local:
        if args.all:
            raise SystemExit("--all is not supported; use orchestrator")
        else:
            if not args.vendor:
                raise SystemExit(" Provide --vendor and --submission-id")

        canonicalize_vendor(
            args.vendor,
            args.workflow,
            args.submission_type,
            submission_id,
            local=True,
        )
    # -------------------------
    # AZURE MODE
    # -------------------------
    else:
        container = get_container()

        if args.all:
            vendors = list_vendors_azure(
                container,
                args.workflow,
                args.submission_type,
                submission_id
            )
            if not vendors:
                raise SystemExit(" No Azure vendors found with autofix outputs")
            print(f"🔎 Found {len(vendors)} Azure vendors")
            for v in vendors:
                canonicalize_vendor(
                    v,
                    args.workflow,
                    args.submission_type,
                    submission_id,
                    local=False,
                )
        else:
            if not args.vendor:
                raise SystemExit("Provide --vendor and --submission_id")
            canonicalize_vendor(
                args.vendor,
                args.workflow,
                args.submission_type,
                submission_id,
                local=False,
            )

