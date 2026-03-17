"""
promote_data_to_ready.py (Azure-native)

Purpose:
---------
Promote canonical data from silver/in_review → silver/ready
ONLY if integrity checks allow it.

Reads (Azure):
  silver/in_review/vendor=<vendor>/canonical/
  silver/in_review/vendor=<vendor>/integrity/integrity_summary.json

Writes (Azure):
  silver/ready/vendor=<vendor>/data/
    - item_master.parquet
    - pricing.parquet
    - attributes.parquet
    - data_snapshot.xlsx
    - promotion_summary.json

Rules:
------
-  If blocking issues > 0 → do NOT promote
- ⚠️ Warnings are allowed
- No data mutation
- Promotion is a copy + freeze

Run:
  python promote_data_to_ready.py --vendor "Grote Lighting"
  python promote_data_to_ready.py --all
"""

import os
import json
from io import BytesIO
from datetime import datetime
from typing import List

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from azure.storage.blob import BlobServiceClient


# =========================================================
# CONFIG
# =========================================================
AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = "silver"

IN_REVIEW_ROOT = "in_review"
APPROVED_ROOT = "approved"

CANONICAL_DIR = "canonical"
INTEGRITY_DIR = "integrity"
APPROVED_DATA_DIR = "data"

# =========================================================
# Azure helpers
# =========================================================
def get_container():
    if not AZURE_CONN_STR:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set")
    return BlobServiceClient.from_connection_string(AZURE_CONN_STR)\
        .get_container_client(SILVER_CONTAINER)


def download_bytes(container, blob_path: str) -> bytes:
    return container.get_blob_client(blob_path).download_blob().readall()


def upload_bytes(container, blob_path: str, data: bytes):
    container.upload_blob(blob_path, data, overwrite=True)


def download_df(container, blob_path: str) -> pd.DataFrame:
    data = download_bytes(container, blob_path)
    table = pq.read_table(BytesIO(data))
    return table.to_pandas()


def upload_df(container, blob_path: str, df: pd.DataFrame):
    buf = BytesIO()
    pq.write_table(pa.Table.from_pandas(df), buf)
    upload_bytes(container, blob_path, buf.getvalue())


def list_vendors_ready(container) -> List[str]:
    vendors = set()
    prefix = f"{IN_REVIEW_ROOT}/vendor="
    for b in container.list_blobs(name_starts_with=prefix):
        if f"/{INTEGRITY_DIR}/integrity_summary.json" in b.name:
            vendor = b.name.split("vendor=", 1)[1].split("/", 1)[0]
            vendors.add(vendor)
    return sorted(vendors)


# =========================================================
# Promotion logic
# =========================================================
def load_integrity_summary(container, vendor: str) -> dict:
    path = f"{IN_REVIEW_ROOT}/vendor={vendor}/{INTEGRITY_DIR}/integrity_summary.json"
    data = download_bytes(container, path)
    return json.loads(data)


def promote_vendor(container, vendor: str):
    print(f"\n🚀 Promotion check: {vendor}")

    integrity = load_integrity_summary(container, vendor)

    blocking = integrity.get("blocking", 0)
    warnings = integrity.get("warnings", 0)
    can_promote = integrity.get("can_promote", False)

    if not can_promote:
        print(f"⛔ BLOCKED: {blocking} blocking integrity issues")
        promotion_summary = {
            "vendor": vendor,
            "promoted": False,
            "blocked": True,
            "blocking_issues": blocking,
            "warnings": warnings,
            "checked_at": datetime.utcnow().isoformat(),
            "reason": "Blocking integrity issues present",
        }
        upload_bytes(
            container,
            f"{IN_REVIEW_ROOT}/vendor={vendor}/{INTEGRITY_DIR}/promotion_failed.json",
            json.dumps(promotion_summary, indent=2).encode(),
        )
        return

    # -----------------------------------------------------
    # Load canonical data
    # -----------------------------------------------------
    base_in = f"{IN_REVIEW_ROOT}/vendor={vendor}/{CANONICAL_DIR}"

    item = download_df(container, f"{base_in}/item_master_canonical.parquet")
    pricing = download_df(container, f"{base_in}/pricing_canonical.parquet")
    attrs = download_df(container, f"{base_in}/attributes_canonical.parquet")

    # -----------------------------------------------------
    # Write approved data
    # -----------------------------------------------------
    base_out = f"{APPROVED_ROOT}/vendor={vendor}/{APPROVED_DATA_DIR}"

    # Replay safety: do not overwrite approved data
    existing = list(container.list_blobs(name_starts_with=base_out))
    if existing:
        print(f"⚠️ SKIPPED: {vendor} already promoted")
        return

    upload_df(container, f"{base_out}/item_master.parquet", item)
    upload_df(container, f"{base_out}/pricing.parquet", pricing)
    upload_df(container, f"{base_out}/attributes.parquet", attrs)

    # Excel snapshot
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        item.to_excel(writer, sheet_name="item_master", index=False)
        pricing.to_excel(writer, sheet_name="pricing", index=False)
        attrs.to_excel(writer, sheet_name="attributes", index=False)
    upload_bytes(container, f"{base_out}/data_snapshot.xlsx", buf.getvalue())

    # Promotion summary (THIS is the data equivalent of media_canonical)
    promotion_summary = {
        "vendor": vendor,
        "promoted": True,
        "blocked": False,
        "blocking_issues": blocking,
        "warnings": warnings,
        "promoted_at": datetime.utcnow().isoformat(),
        "integrity_snapshot": integrity,
        "canonical_hashes": integrity.get("canonical_hashes"),
        "notes": "Data promoted from in_review canonical to approved without mutation.",
    }

    upload_bytes(
        container,
        f"{base_out}/promotion_summary.json",
        json.dumps(promotion_summary, indent=2).encode(),
    )

    print(f"✅ PROMOTED: {vendor} | warnings={warnings}")


# =========================================================
# CLI
# =========================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", type=str)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    container = get_container()

    if args.all:
        vendors = list_vendors_ready(container)
        if not vendors:
            raise SystemExit("No vendors found with integrity summaries.")
        for v in vendors:
            promote_vendor(container, v)
    else:
        if not args.vendor:
            raise SystemExit("Provide --vendor or --all")
        promote_vendor(container, args.vendor)
