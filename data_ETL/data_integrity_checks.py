"""
data_integrity_checks.py

Purpose:
---------
Perform cross-entity integrity checks AFTER canonicalization.

Supports:
  • Azure mode (default)
  • Local mode (--local)

Reads:
  item_master_canonical.parquet
  pricing_canonical.parquet
  media_canonical.parquet (optional)

Writes:
  integrity_issues.parquet
  integrity_summary.json
  integrity_report.xlsx
"""

import os
import json
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
AZURE_CONN = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = "silver"

IN_REVIEW = "in_review"
PRICING_REVIEW = "post_pricing_review"

CANONICAL_DIR = "canonical"
INTEGRITY_DIR = "integrity"

BLOCKING = "blocking"
WARNING = "warning"

RUN_TS = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")


# =========================================================
# MODE HELPERS
# =========================================================
def get_container():
    if not AZURE_CONN:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set")
    service = BlobServiceClient.from_connection_string(AZURE_CONN)
    return service.get_container_client(SILVER_CONTAINER)

def read_df(container, path: str, local: bool) -> pd.DataFrame:
    """Read parquet from local disk or Azure."""
    if local:
        return pd.read_parquet(path)

    data = container.get_blob_client(path).download_blob().readall()
    return pq.read_table(BytesIO(data)).to_pandas()


def write_df(container, path: str, df: pd.DataFrame, local: bool):
    """Write parquet to local or Azure."""
    if local:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        df.to_parquet(path, index=False)
        return

    buf = BytesIO()
    pq.write_table(pa.Table.from_pandas(df), buf)
    container.upload_blob(path, buf.getvalue(), overwrite=True)


def write_bytes(container, path: str, data: bytes, local: bool):
    """Write bytes to local or Azure."""
    if local:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    else:
        container.upload_blob(path, data, overwrite=True)


def list_vendors(local: bool, container=None, mode="full") -> List[str]:
    """Detect vendors having canonical outputs."""
    vendors = set()

    if local:
        root = PRICING_REVIEW if mode == "post_review" else IN_REVIEW
        base = os.path.join("silver", root)

        if not os.path.exists(base):
            return []
        for d in os.listdir(base):
            if d.startswith("vendor="):
                v = d.split("vendor=", 1)[1]
                if os.path.exists(os.path.join(base, d, CANONICAL_DIR, "item_master_canonical.parquet")):
                    vendors.add(v)
    else:
        root = PRICING_REVIEW if mode == "post_review" else IN_REVIEW
        prefix = f"{root}/vendor="

        for b in container.list_blobs(name_starts_with=prefix):
            if f"/{CANONICAL_DIR}/item_master_canonical.parquet" in b.name:
                v = b.name.split("vendor=", 1)[1].split("/", 1)[0]
                vendors.add(v)

    return sorted(vendors)


# =========================================================
# INTEGRITY CHECK HELPERS
# =========================================================
def resolve_join_key(df: pd.DataFrame) -> str | None:
    """Pick best join key."""
    for col in ["_entity_id", "SKU", "Part Number", "PartNumber", "Part_Number"]:
        if col in df.columns:
            return col
    return None


def check_missing_required_fields(item: pd.DataFrame) -> List[Dict]:
    issues = []
    REQUIRED = ["Category"]

    for field in REQUIRED:
        if field not in item.columns:
            issues.append({
                "issue_type": "required_field_missing_column",
                "severity": BLOCKING,
                "field": field,
                "details": f"Required field '{field}' column missing"
            })
            continue

        for _, r in item[item[field].isna()].iterrows():
            issues.append({
                "issue_type": "required_field_missing_value",
                "severity": BLOCKING,
                "_entity_id": r["_entity_id"],
                "field": field,
                "join_key": "Part Number",
                "join_value": r.get("Part Number"),
                "details": f"Required field '{field}' is missing"
            })

    return issues


def check_pricing_without_item(item, pricing):
    issues = []
    item_ids = set(item["_entity_id"])

    for _, r in pricing.iterrows():
        if r["_entity_id"] not in item_ids:
            issues.append({
                "issue_type": "pricing_without_item",
                "severity": BLOCKING,
                "_entity_id": r["_entity_id"],
                "join_key": "Part Number",
                "join_value": r.get("Part Number"),
                "field": "Pricing",
                "details": "Pricing exists without item"
            })
    return issues


def check_item_without_pricing(item, pricing):
    issues = []
    priced = set(pricing[pricing["_pricing_source"] == "vendor"]["_entity_id"])

    for _, r in item.iterrows():
        if r["_entity_id"] not in priced:
            issues.append({
                "issue_type": "item_without_pricing",
                "severity": BLOCKING,
                "_entity_id": r["_entity_id"],
                "join_key": "Part Number",
                "join_value": r.get("Part Number"),
                "field": "Pricing",
                "details": "Item exists without pricing"
            })
    return issues


def check_pricing_contradictions(pricing):
    issues = []
    PRICE_COLS = ["List Price", "Jobber Price", "Dealer Price", "Net Price", "Discount %"]
    cols = [c for c in PRICE_COLS if c in pricing.columns]

    for eid, g in pricing.groupby("_entity_id"):
        part = g["Part Number"].iloc[0] if "Part Number" in g.columns else None
        for c in cols:
            if g[c].dropna().nunique() > 1:
                issues.append({
                    "issue_type": "pricing_contradiction",
                    "severity": BLOCKING,
                    "_entity_id": eid,
                    "join_key": "Part Number",
                    "join_value": part,
                    "field": c,
                    "details": f"Multiple values found for {c}"
                })
    return issues


def check_duplicate_upc(item):
    issues = []
    if "UPC" not in item.columns:
        return issues

    dup = item[item["UPC"].notna()].groupby("UPC").filter(lambda x: len(x) > 1)
    for upc, g in dup.groupby("UPC"):
        issues.append({
            "issue_type": "duplicate_upc",
            "severity": BLOCKING,
            "value": upc,
            "entity_ids": g["_entity_id"].tolist(),
            "details": "Same UPC mapped to multiple SKUs"
        })
    return issues


def check_invalid_upc(item):
    issues = []
    if "UPC" not in item.columns:
        return issues

    for _, r in item[item["UPC"].notna()].iterrows():
        upc = str(r["UPC"]).strip()
        if not upc.isdigit() or len(upc) not in (8, 12, 13, 14):
            issues.append({
                "issue_type": "invalid_upc",
                "severity": BLOCKING,
                "_entity_id": r["_entity_id"],
                "value": upc,
                "details": "UPC/GTIN must be numeric and valid length"
            })
    return issues


def check_assets(item, media):
    issues = []
    if media.empty:
        return issues

    item_key = resolve_join_key(item)
    media_key = resolve_join_key(media)
    if not item_key or not media_key:
        return [{
            "issue_type": "asset_item_join_unavailable",
            "severity": WARNING,
            "details": "Cannot join item and media"
        }]

    item_vals = set(item[item_key].dropna())
    media_vals = set(media[media_key].dropna())

    for val in media_vals - item_vals:
        issues.append({
            "issue_type": "asset_without_item",
            "severity": WARNING,
            "join_key": media_key,
            "join_value": val,
            "details": "Asset exists but no item matches"
        })

    for val in item_vals - media_vals:
        issues.append({
            "issue_type": "item_without_assets",
            "severity": WARNING,
            "join_key": item_key,
            "join_value": val,
            "details": "Item exists without any assets"
        })

    return issues


# =========================================================
# SNAPSHOT in_review → logs (append-only)
# =========================================================
def snapshot_in_review_to_logs(container, vendor: str, submission_id: str, local: bool, mode: str):
    """
    Copy selected in_review folders into silver/logs with a timestamp.
    Append-only. Never overwritten.
    """

    if local:
        # Local mode: optional, skip or implement later
        print("ℹ️  Local mode: skipping logs snapshot")
        return

    root = PRICING_REVIEW if mode == "post_review" else IN_REVIEW
    SRC_BASE = f"{root}/vendor={vendor}/submission={submission_id}"

    DEST_BASE = f"logs/vendor={vendor}/submission={submission_id}/run_ts={RUN_TS}"

    SNAPSHOT_DIRS = ["profiling", "autofix", "canonical", "integrity"]

    for d in SNAPSHOT_DIRS:
        src_prefix = f"{SRC_BASE}/{d}/"
        dest_prefix = f"{DEST_BASE}/{d}/"

        for blob in container.list_blobs(name_starts_with=src_prefix):
            data = container.get_blob_client(blob.name).download_blob().readall()

            # keep relative path inside folder
            rel_path = blob.name.replace(src_prefix, "")
            dest_path = f"{dest_prefix}{rel_path}"

            container.upload_blob(dest_path, data, overwrite=False)

    print(f"📦 Snapshot saved → silver/{DEST_BASE}")



# =========================================================
# MAIN RUNNER
# =========================================================
def run_vendor(vendor: str, submission_id: str, container, local: bool, mode: str):
    
    root = PRICING_REVIEW if mode == "post_review" else IN_REVIEW
    base = (
        os.path.join(
            "silver",
            root,
            f"vendor={vendor}",
            f"submission={submission_id}",
        )
        if local else
        f"{root}/vendor={vendor}/submission={submission_id}"
    )

    item = read_df(container, f"{base}/{CANONICAL_DIR}/item_master_canonical.parquet", local)
    pricing = read_df(container, f"{base}/{CANONICAL_DIR}/pricing_canonical.parquet", local)

    try:
        media = read_df(container, f"{base}/{CANONICAL_DIR}/media_canonical.parquet", local)
    except Exception:
        media = pd.DataFrame()

    issues = []
    issues += check_pricing_without_item(item, pricing)
    issues += check_item_without_pricing(item, pricing)
    issues += check_missing_required_fields(item)
    issues += check_duplicate_upc(item)
    issues += check_invalid_upc(item)
    issues += check_pricing_contradictions(pricing)
    issues += check_assets(item, media)

    issues_df = pd.DataFrame(issues).drop_duplicates()

    summary = {
        "vendor": vendor,
        "checked_at": datetime.utcnow().isoformat(),
        "total_issues": len(issues_df),
        "blocking": int((issues_df["severity"] == BLOCKING).sum()) if not issues_df.empty else 0,
        "warnings": int((issues_df["severity"] == WARNING).sum()) if not issues_df.empty else 0,
        "can_promote": (
            issues_df.empty or not (issues_df["severity"] == BLOCKING).any()
        ),
    }

    out_base = f"{base}/{INTEGRITY_DIR}"

    write_df(container, f"{out_base}/integrity_issues.parquet", issues_df, local)
    write_bytes(container, f"{out_base}/integrity_summary.json", json.dumps(summary, indent=2).encode(), local)

    # Excel report
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        issues_df.to_excel(writer, index=False, sheet_name="issues")
        pd.DataFrame([summary]).to_excel(writer, index=False, sheet_name="summary")

    write_bytes(container, f"{out_base}/integrity_report.xlsx", buf.getvalue(), local)

    print(f"✅ Integrity checks complete: {vendor} | issues={len(issues_df)}")

    # -------------------------------------------------
    # SNAPSHOT CURRENT in_review STATE (ALWAYS)
    # -------------------------------------------------
    snapshot_in_review_to_logs(container, vendor, submission_id, local, mode)

# =========================================================
# External Pipeline Entry Point
# =========================================================

def run_integrity_checks(vendor: str, submission_id: str, source: str = "full") -> bool:
    """
    Entry point for other pipelines (e.g. post-review pipeline).

    Returns:
        bool → True if can promote, False if blocking issues exist
    """

    if source == "post_review":
        mode = "post_review"
    else:
        mode = "full"

    container = get_container()

    # Run integrity
    run_vendor(
        vendor=vendor,
        submission_id=submission_id,
        container=container,
        local=False,
        mode=mode
    )

    # After run, read summary to determine promotion decision
    root = PRICING_REVIEW if mode == "post_review" else IN_REVIEW

    summary_path = (
        f"{root}/vendor={vendor}/"
        f"submission={submission_id}/"
        f"{INTEGRITY_DIR}/integrity_summary.json"
    )

    summary_bytes = container.get_blob_client(summary_path).download_blob().readall()
    summary = json.loads(summary_bytes)

    return bool(summary.get("can_promote", False))

# =========================================================
# CLI
# =========================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", type=str)
    parser.add_argument("--submission-id", type=str)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--mode", default="full")

    args = parser.parse_args()

    submission_id = args.submission_id
    mode = args.mode

    container = None if args.local else get_container()

    if args.all:
        raise SystemExit("--all is not supported; use orchestrator")
    else:
        if not args.vendor:
            raise SystemExit(" Provide --vendor and --submission-id")
        run_vendor(vendor=args.vendor,submission_id=submission_id, container=container, local=args.local, mode=mode)
