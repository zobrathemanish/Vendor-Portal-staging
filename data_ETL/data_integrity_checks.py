"""
data_integrity_checks.py

Purpose:
---------
Perform workflow-aware integrity checks AFTER canonicalization.

Supports:
  • Azure mode (default)
  • Local mode (--local)

Reads (depends on workflow):
  workflow=products
    - item_master_canonical.parquet
    - media_canonical.parquet (optional)

  workflow=pricing
    - pricing_canonical.parquet

Writes:
  integrity_issues.parquet
  integrity_summary.json
  integrity_report.xlsx

Notes:
------
- This stage is intentionally workflow-scoped.
- Cross-workflow checks like item_without_pricing or pricing_without_item
  are NOT performed here anymore.
- Merge/reconciliation can happen later downstream.
"""

import os
import json
from io import BytesIO
from datetime import datetime
from typing import List, Dict, Optional

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

WORKFLOW_PRODUCTS = "products"
WORKFLOW_PRICING = "pricing"
SUPPORTED_WORKFLOWS = {WORKFLOW_PRODUCTS, WORKFLOW_PRICING}

RUN_TS = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")

def parse_submission_type(submission_type: str):
    return {
        "is_review": "review" in submission_type,
        "is_delta": "delta" in submission_type,
        "workflow": "pricing" if "pricing" in submission_type else "product"
    }


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


def list_vendors(
    local: bool,
    container=None,
    workflow: str = None,
    submission_type: str = None,
    submission_id: str = None,
) -> List[str]:

    ensure_supported_workflow(workflow)

    meta = parse_submission_type(submission_type)
    root = PRICING_REVIEW if meta["is_review"] else IN_REVIEW

    expected_file = (
        "item_master_canonical.parquet"
        if workflow == WORKFLOW_PRODUCTS
        else "pricing_canonical.parquet"
    )

    vendors = set()

    # -------------------------
    # LOCAL MODE
    # -------------------------
    if local:
        base = os.path.join(
            "silver",
            root,
            f"{workflow}_workflow"
        )

        if not os.path.exists(base):
            return []

        for d in os.listdir(base):
            if not d.startswith("vendor="):
                continue

            check_path = os.path.join(
                base,
                d,
                f"submission_type={submission_type}",
                f"submission={submission_id}",
                CANONICAL_DIR,
                expected_file
            )

            if os.path.exists(check_path):
                vendor = d.split("vendor=", 1)[1]
                vendors.add(vendor)

    # -------------------------
    # AZURE MODE
    # -------------------------
    else:
        prefix = f"{root}/{workflow}_workflow/"

        for blob in container.list_blobs(name_starts_with=prefix):
            name = blob.name

            if (
                f"/submission_type={submission_type}/" in name
                and f"/submission={submission_id}/" in name
                and name.endswith(f"{CANONICAL_DIR}/{expected_file}")
            ):
                try:
                    vendor = name.split("vendor=", 1)[1].split("/", 1)[0]
                    vendors.add(vendor)
                except Exception:
                    continue

    return sorted(vendors)

# =========================================================
# HELPERS
# =========================================================
def resolve_join_key(df: pd.DataFrame) -> Optional[str]:
    """Pick best join key."""
    for col in ["_entity_id", "SKU", "Part Number", "PartNumber", "Part_Number"]:
        if col in df.columns:
            return col
    return None


def blank_or_null(series: pd.Series) -> pd.Series:
    mask = series.isna()
    if series.dtype == object or str(series.dtype).startswith("string"):
        mask = mask | (series.astype(str).str.strip() == "")
    return mask


def safe_read_optional_df(container, path: str, local: bool) -> pd.DataFrame:
    try:
        return read_df(container, path, local)
    except Exception:
        return pd.DataFrame()


def ensure_supported_workflow(workflow: str) -> None:
    if workflow not in SUPPORTED_WORKFLOWS:
        raise ValueError(
            f"Unsupported workflow: {workflow}. "
            f"Supported workflows: {sorted(SUPPORTED_WORKFLOWS)}"
        )


def build_base_path(
    vendor: str,
    workflow: str,
    submission_type: str,
    submission_id: str,
    local: bool,
) -> str:
    meta = parse_submission_type(submission_type)
    root = PRICING_REVIEW if meta["is_review"] else IN_REVIEW

    if local:
        return os.path.join(
            "silver",
            root,
            f"{workflow}_workflow",
            f"vendor={vendor}",
            f"submission_type={submission_type}",
            f"submission={submission_id}",
        )

    return (
        f"{root}/{workflow}_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}"
    )


# =========================================================
# INTEGRITY CHECK HELPERS
# =========================================================
def check_missing_required_fields(item: pd.DataFrame) -> List[Dict]:
    issues = []
    required_fields = [
        "Brand Label",
        "Part Number",
        "PartTerminologyID",   # ✅ NEW (core classification)
        "Product Status"
    ]

    for field in required_fields:
        if field not in item.columns:
            issues.append({
                "issue_type": "required_field_missing_column",
                "severity": BLOCKING,
                "field": field,
                "details": f"Required field '{field}' column missing",
            })
            continue

        missing_mask = blank_or_null(item[field])
        for _, row in item[missing_mask].iterrows():
            issues.append({
                "issue_type": "required_field_missing_value",
                "severity": BLOCKING,
                "_entity_id": row.get("_entity_id"),
                "field": field,
                "join_key": "Part Number",
                "join_value": row.get("Part Number"),
                "details": f"Required field '{field}' is missing",
            })

    return issues


def check_pricing_contradictions(pricing: pd.DataFrame) -> List[Dict]:
    issues = []
    if pricing.empty:
        return issues

    if "_entity_id" not in pricing.columns:
        return [{
            "issue_type": "pricing_entity_id_missing",
            "severity": BLOCKING,
            "field": "_entity_id",
            "details": "Pricing canonical is missing _entity_id; cannot perform contradiction checks",
        }]

    price_cols = ["List Price", "Jobber Price", "Dealer Price", "Net Price", "Discount %"]
    cols = [c for c in price_cols if c in pricing.columns]

    for entity_id, group in pricing.groupby("_entity_id", dropna=False):
        part = group["Part Number"].iloc[0] if "Part Number" in group.columns and not group.empty else None
        for col in cols:
            non_null = group[col].dropna()
            if non_null.nunique() > 1:
                issues.append({
                    "issue_type": "pricing_contradiction",
                    "severity": BLOCKING,
                    "_entity_id": entity_id,
                    "join_key": "Part Number",
                    "join_value": part,
                    "field": col,
                    "details": f"Multiple values found for {col}",
                })

    return issues


def check_duplicate_upc(item: pd.DataFrame) -> List[Dict]:
    issues = []
    if item.empty or "UPC" not in item.columns:
        return issues

    dup = item[item["UPC"].notna()].groupby("UPC").filter(lambda x: len(x) > 1)
    for upc, group in dup.groupby("UPC"):
        issues.append({
            "issue_type": "duplicate_upc",
            "severity": BLOCKING,
            "value": upc,
            "entity_ids": group["_entity_id"].tolist() if "_entity_id" in group.columns else [],
            "details": "Same UPC mapped to multiple SKUs",
        })
    return issues


def check_invalid_upc(item: pd.DataFrame) -> List[Dict]:
    issues = []
    if item.empty or "UPC" not in item.columns:
        return issues

    for _, row in item[item["UPC"].notna()].iterrows():
        upc = str(row["UPC"]).strip()
        if not upc.isdigit() or len(upc) not in (8, 12, 13, 14):
            issues.append({
                "issue_type": "invalid_upc",
                "severity": BLOCKING,
                "_entity_id": row.get("_entity_id"),
                "value": upc,
                "details": "UPC/GTIN must be numeric and valid length",
            })

    return issues


def check_assets(item: pd.DataFrame, media: pd.DataFrame) -> List[Dict]:
    issues = []
    if item.empty or media.empty:
        return issues

    item_key = resolve_join_key(item)
    media_key = resolve_join_key(media)

    if not item_key or not media_key:
        return [{
            "issue_type": "asset_item_join_unavailable",
            "severity": WARNING,
            "details": "Cannot join item and media",
        }]

    item_vals = set(item[item_key].dropna())
    media_vals = set(media[media_key].dropna())

    for val in media_vals - item_vals:
        issues.append({
            "issue_type": "asset_without_item",
            "severity": WARNING,
            "join_key": media_key,
            "join_value": val,
            "details": "Asset exists but no item matches",
        })

    for val in item_vals - media_vals:
        issues.append({
            "issue_type": "item_without_assets",
            "severity": WARNING,
            "join_key": item_key,
            "join_value": val,
            "details": "Item exists without any assets",
        })

    return issues


# =========================================================
# WORKFLOW LOADERS
# =========================================================
def load_workflow_data(base: str, workflow: str, container, local: bool) -> Dict[str, pd.DataFrame]:
    """
    Load only the canonical entities needed for the current workflow.
    """
    ensure_supported_workflow(workflow)

    data = {
        "item": pd.DataFrame(),
        "pricing": pd.DataFrame(),
        "media": pd.DataFrame(),
    }

    if workflow == WORKFLOW_PRODUCTS:
        data["item"] = read_df(container, f"{base}/{CANONICAL_DIR}/item_master_canonical.parquet", local)
        data["media"] = safe_read_optional_df(container, f"{base}/{CANONICAL_DIR}/media_canonical.parquet", local)

    elif workflow == WORKFLOW_PRICING:
        data["pricing"] = read_df(container, f"{base}/{CANONICAL_DIR}/pricing_canonical.parquet", local)

    return data


def run_workflow_checks(workflow: str, data: Dict[str, pd.DataFrame]) -> List[Dict]:
    """
    Run only the integrity checks relevant to the current workflow.
    """
    ensure_supported_workflow(workflow)

    issues: List[Dict] = []

    if workflow == WORKFLOW_PRODUCTS:
        item = data["item"]
        media = data["media"]

        issues += check_missing_required_fields(item)
        issues += check_duplicate_upc(item)
        issues += check_invalid_upc(item)

        if not media.empty:
            issues += check_assets(item, media)

    elif workflow == WORKFLOW_PRICING:
        pricing = data["pricing"]
        issues += check_pricing_contradictions(pricing)

    return issues


# =========================================================
# SNAPSHOT in_review → logs (append-only)
# =========================================================
def snapshot_in_review_to_logs(
        container,
        vendor: str,
        workflow: str,
        submission_type: str,
        submission_id: str,
        local: bool,
    ):
    """
    Copy selected in_review folders into silver/logs with a timestamp.
    Append-only. Never overwritten.
    """
    if local:
        print("ℹ️  Local mode: skipping logs snapshot")
        return

    meta = parse_submission_type(submission_type)
    root = PRICING_REVIEW if meta["is_review"] else IN_REVIEW

    src_base = (
        f"{root}/{workflow}_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}"
    )

    dest_base = (
        f"logs/vendor={vendor}/"
        f"submission={submission_id}/"
        f"workflow={workflow}/"
        f"run_ts={RUN_TS}"
    )

    snapshot_dirs = ["profiling", "autofix", "canonical", "integrity"]

    for dirname in snapshot_dirs:
        src_prefix = f"{src_base}/{dirname}/"
        dest_prefix = f"{dest_base}/{dirname}/"

        for blob in container.list_blobs(name_starts_with=src_prefix):
            data = container.get_blob_client(blob.name).download_blob().readall()
            rel_path = blob.name.replace(src_prefix, "")
            dest_path = f"{dest_prefix}{rel_path}"
            container.upload_blob(dest_path, data, overwrite=False)

    print(f"📦 Snapshot saved → silver/{dest_base}")


# =========================================================
# MAIN RUNNER
# =========================================================
def run_vendor(
        vendor: str,
        workflow: str,
        submission_type: str,
        submission_id: str,
        container,
        local: bool,
    ):
    ensure_supported_workflow(workflow)

    base = build_base_path(
        vendor=vendor,
        workflow=workflow,
        submission_type=submission_type,
        submission_id=submission_id,
        local=local,
    )

    data = load_workflow_data(base=base, workflow=workflow, container=container, local=local)
    issues = run_workflow_checks(workflow=workflow, data=data)

    for issue in issues:
        issue["workflow"] = workflow
        issue["vendor"] = vendor
        issue["submission_id"] = submission_id
        issue["submission_type"] = submission_type

    issues_df = pd.DataFrame(issues).drop_duplicates() if issues else pd.DataFrame()

    summary = {
        "vendor": vendor,
        "workflow": workflow,
        "submission_id": submission_id,
        "submission_type": submission_type,
        "checked_at": datetime.utcnow().isoformat(),
        "total_issues": len(issues_df),
        "blocking": int((issues_df["severity"] == BLOCKING).sum()) if not issues_df.empty and "severity" in issues_df.columns else 0,
        "warnings": int((issues_df["severity"] == WARNING).sum()) if not issues_df.empty and "severity" in issues_df.columns else 0,
        "can_promote": (
            issues_df.empty or not ((issues_df["severity"] == BLOCKING).any())
        ),
    }

    out_base = f"{base}/{INTEGRITY_DIR}"

    write_df(container, f"{out_base}/integrity_issues.parquet", issues_df, local)
    write_bytes(
        container,
        f"{out_base}/integrity_summary.json",
        json.dumps(summary, indent=2).encode(),
        local,
    )

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        issues_df.to_excel(writer, index=False, sheet_name="issues")
        pd.DataFrame([summary]).to_excel(writer, index=False, sheet_name="summary")

    write_bytes(container, f"{out_base}/integrity_report.xlsx", buf.getvalue(), local)

    print(
        f"✅ Integrity checks complete: "
        f"vendor={vendor} | workflow={workflow} | issues={len(issues_df)}"
    )

    snapshot_in_review_to_logs(
        container=container,
        vendor=vendor,
        submission_id=submission_id,
        submission_type=submission_type,
        workflow=workflow,
        local=local,
    )


# =========================================================
# External Pipeline Entry Point
# =========================================================
def run_integrity_checks(
        vendor: str,
        workflow: str,
        submission_type: str,
        submission_id: str,
    ) -> bool:
    """
    Entry point for other pipelines.

    Returns:
        bool → True if can promote, False if blocking issues exist
    """
    ensure_supported_workflow(workflow)

    container = get_container()

    run_vendor(
        vendor=vendor,
        submission_id=submission_id,
        submission_type=submission_type,
        workflow=workflow,
        container=container,
        local=False,
    )

    meta = parse_submission_type(submission_type)
    root = PRICING_REVIEW if meta["is_review"] else IN_REVIEW

    summary_path = (
        f"{root}/{workflow}_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
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
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--submission-type", dest="submission_type", required=True)

    args = parser.parse_args()

    ensure_supported_workflow(args.workflow)

    submission_id = args.submission_id
    mode = args.mode
    container = None if args.local else get_container()

    if args.all:
        raise SystemExit("--all is not supported; use orchestrator")

    if not args.vendor:
        raise SystemExit("Provide --vendor and --submission-id")

    run_vendor(
        vendor=args.vendor,
        submission_id=submission_id,
        submission_type=args.submission_type,
        workflow=args.workflow,
        container=container,
        local=args.local,
    )