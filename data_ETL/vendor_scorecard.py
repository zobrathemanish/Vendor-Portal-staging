"""
vendor_scorecard.py

Purpose:
--------
Consumes vendor_profile artifacts and produces weighted vendor scorecards
for category and leadership decision-making.

Supports:
  • Azure mode (default)
  • Local mode (--local)

READ-ONLY. Business-weight driven.
Append-only, vendor-scoped outputs.
"""

# =========================================================
# IMPORTS
# =========================================================
import os
from io import BytesIO
from datetime import datetime
from typing import Dict, List

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from azure.storage.blob import BlobServiceClient
from data_ETL.admin_snapshot import update_admin_summary

# =========================================================
# CONFIG
# =========================================================
AZURE_CONN = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = "silver"

PROFILE_BASE_DIR = "analytics/vendor_profiling"
OUT_BASE_DIR = "analytics/vendor_scorecard"

IN_REVIEW = "in_review"

WEIGHTS = {
    "data_quality": 0.35,
    "automation": 0.25,
    "assets": 0.25,
    "operations": 0.15,
}

# =========================================================
# PIPELINE CONTEXT (NEW)
# =========================================================
class PipelineContext:
    def __init__(self, submission_type: str):
        self.submission_type = submission_type

#Adding Base Resolver
def submission_base_path(vendor: str, workflow:str, submission_id: str, local: bool, ctx: PipelineContext) -> str:
    root = "in_review"

    if local:
        return os.path.join(
            "silver",
            root,
            f"{workflow}_workflow",
            f"vendor={vendor}",
            f"submission_type={ctx.submission_type}",
            f"submission={submission_id}",
        )

    return (
        f"{root}/"
        f"{workflow}_workflow/"
        f"vendor={vendor}/"
        f"submission_type={ctx.submission_type}/"
        f"submission={submission_id}"
    )

# =========================================================
# STORAGE HELPERS
# =========================================================
def get_container():
    if not AZURE_CONN:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set")
    return BlobServiceClient.from_connection_string(
        AZURE_CONN
    ).get_container_client(SILVER_CONTAINER)


def read_profile(container, path: str, local: bool) -> pd.DataFrame:
    """
    Read vendor_profile from Azure Blob or local filesystem.
    Supports both Parquet and Excel.
    """
    if local:
        if path.endswith(".parquet"):
            return pd.read_parquet(path)
        if path.endswith(".xlsx"):
            return pd.read_excel(path)
        raise ValueError(f"Unsupported profile format: {path}")

    # Azure mode
    data = container.get_blob_client(path).download_blob().readall()

    if path.endswith(".parquet"):
        return pq.read_table(BytesIO(data)).to_pandas()

    if path.endswith(".xlsx"):
        return pd.read_excel(BytesIO(data))

    raise ValueError(f"Unsupported profile format: {path}")


def write_outputs(container, vendor, workflow, submission_id, df, local, ctx):
    base = submission_base_path(vendor, workflow, submission_id, local, ctx)

    if local:
        out_dir = os.path.join(base, "analytics", "vendor_scorecard")
        os.makedirs(out_dir, exist_ok=True)

        df.to_parquet(os.path.join(out_dir, f"vendor_scorecard_{submission_id}.parquet"), index=False)
        df.to_excel(os.path.join(out_dir, f"vendor_scorecard_{submission_id}.xlsx"), index=False)
        return

    out_dir = f"{base}/analytics/vendor_scorecard"

    buf = BytesIO()
    pq.write_table(pa.Table.from_pandas(df), buf)
    container.upload_blob(f"{out_dir}/vendor_scorecard_{submission_id}.parquet", buf.getvalue(), overwrite=True)

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    container.upload_blob(f"{out_dir}/vendor_scorecard_{submission_id}.xlsx", buf.getvalue(), overwrite=True)


# =========================================================
# DISCOVERY
# =========================================================
def list_vendors(local: bool, container=None) -> List[str]:
    vendors = set()

    if local:
        base = os.path.join("silver", PROFILE_BASE_DIR)
        if not os.path.exists(base):
            return []
        for d in os.listdir(base):
            if d.startswith("vendor="):
                vendors.add(d.split("vendor=", 1)[1])
    else:
        prefix = f"{PROFILE_BASE_DIR}/vendor="
        for b in container.list_blobs(name_starts_with=prefix):
            v = b.name.split("vendor=", 1)[1].split("/", 1)[0]
            vendors.add(v)

    return sorted(vendors)


def get_profile_path(vendor: str, workflow:str, submission_id: str, local: bool, ctx: PipelineContext) -> str:
    base = submission_base_path(vendor, workflow, submission_id, local, ctx)

    if local:
        return os.path.join(
            base,
            "analytics",
            "vendor_profiling",
            f"vendor_profile_{submission_id}.parquet",
        )

    return f"{base}/analytics/vendor_profiling/vendor_profile_{submission_id}.parquet"


# =========================================================
# SCORING FUNCTIONS
# =========================================================
def score_data_quality(row: pd.Series) -> float:
    score = 100.0
    score -= min(row.get("health_issue_count", 0), 40)
    score -= row.get("entity_scoped_issues", 0) * 2
    score -= row.get("row_missingness_avg_pct", 0)
    return round(max(score, 0.0), 2)


def score_automation(row: pd.Series) -> float:
    return round(float(row.get("autofix_resolution_rate", 0.0)), 2)


def score_assets(row: pd.Series) -> float:
    score = 100.0
    score -= row.get("asset_missing_pct", 0)

    if not row.get("media_ready", False):
        score -= 40

    size_quality = row.get("asset_image_size_compliance_pct", 0.0)
    score -= max(0, 100 - size_quality) * 0.3

    return round(max(score, 0.0), 2)


def score_operations(row: pd.Series) -> float:
    score = 100.0

    if not row.get("can_promote", True):
        score -= 50

    score -= row.get("integrity_blocking_issues", 0) * 10
    return round(max(score, 0.0), 2)

def infer_vendor_from_submission(
    submission_id: str,
    container,
    local: bool,
) -> str | None:
    """
    Find vendor by locating:
      analytics/vendor_profiling/vendor=<vendor>/submission=<submission_id>/
    """

    if local:
        base = os.path.join("silver", PROFILE_BASE_DIR)
        if not os.path.exists(base):
            return None

        for d in os.listdir(base):
            if not d.startswith("vendor="):
                continue

            candidate = os.path.join(
                base,
                d,
                f"submission={submission_id}",
                f"vendor_profile_{submission_id}.parquet",
            )

            if os.path.exists(candidate):
                return d.split("vendor=", 1)[1]

        return None

    # ---------- AZURE ----------
    prefix = f"{PROFILE_BASE_DIR}/vendor="
    blobs = container.list_blobs(name_starts_with=prefix)

    for b in blobs:
        if f"submission={submission_id}/vendor_profile_{submission_id}.parquet" in b.name:
            return b.name.split("vendor=", 1)[1].split("/", 1)[0]

    return None

# =========================================================
# PROGRAMMATIC ENTRYPOINT
# =========================================================
def run_vendor_scorecard(
    vendor: str,
    workflow: str,
    submission_id: str,
    submission_type: str = "product_submission",
    local: bool = False,
    
):
    # 🔥 Normalize workflow input
    workflow = workflow.lower().strip()

    print(f"DEBUG workflow={workflow} | submission_type={submission_type}")

    # handle common mismatch
    if workflow == "products":
        workflow = "product"
        
    if "delta" in submission_type.lower():
        print(f"⏭ Skipping scorecard for delta submission | {submission_id}")
        return
    container = None if local else get_container()

    print("▶️ Vendor Scorecard")
    print("Vendor:", vendor)
    print("Submission Type ", submission_type)

    ctx = PipelineContext(submission_type=submission_type)

    profile_path = get_profile_path(vendor, workflow, submission_id, local, ctx)
    if local and not os.path.exists(profile_path):
        print(f"⚠️ No profiling found | vendor={vendor} | submission={submission_id}")
        return

    if not local:
        try:
            container.get_blob_client(profile_path).get_blob_properties()
        except Exception:
            print(f"⚠️ No profiling found | vendor={vendor} | submission={submission_id}")
            return

    df = read_profile(container, profile_path, local)

    if df.empty:
        print(f"⚠️ Empty profile | vendor={vendor} | submission={submission_id}")
        return

    DEFAULTS: Dict[str, object] = {
        "health_issue_count": 0,
        "entity_scoped_issues": 0,
        "row_missingness_avg_pct": 0.0,
        "autofix_resolution_rate": 0.0,
        "asset_missing_pct": 0.0,
        "media_ready": False,
        "media_compliance_pct": 0.0,
        "asset_image_size_compliance_pct": 0.0,
        "can_promote": True,
        "submission_count": 1,
        "integrity_blocking_issues": 0,
    }

    for col, default in DEFAULTS.items():
        if col not in df.columns:
            df[col] = default

    df["data_quality_score"] = df.apply(score_data_quality, axis=1)
    df["automation_score"] = df.apply(score_automation, axis=1)
    df["asset_score"] = df.apply(score_assets, axis=1)
    df["operations_score"] = df.apply(score_operations, axis=1)

    df["overall_score"] = (
        df["data_quality_score"] * WEIGHTS["data_quality"]
        + df["automation_score"] * WEIGHTS["automation"]
        + df["asset_score"] * WEIGHTS["assets"]
        + df["operations_score"] * WEIGHTS["operations"]
    ).round(2)

    df["scorecard_generated_ts"] = datetime.utcnow().isoformat()

    write_outputs(container, vendor, workflow, submission_id, df, local, ctx)

    print(f"✅ Vendor scorecard generated | vendor={vendor} | submission={submission_id}")

    # -------------------------------------------------
    # ADMIN UPDATE (ONLY AFTER PRICING REVIEW)
    # -------------------------------------------------
    if not local:
        can_promote = bool(df.get("can_promote", [True])[0])

        if can_promote:
            update_admin_summary(
                vendor=vendor,
                submission_id=submission_id,
                profile_df=df,
                scorecard_df=df,
            )
            print(f"📊 Admin summary updated | vendor={vendor}")
        else:
            print(f"⏭ Admin not updated (blocking issues present) | vendor={vendor}")

# =========================================================
# MAIN
# =========================================================
# =========================================================
# CLI ENTRYPOINT
# =========================================================
def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--submission-type", default="product_submission")
    parser.add_argument("--workflow", default="product")
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()

    run_vendor_scorecard(
        vendor=args.vendor,
        workflow=args.workflow,
        submission_id=args.submission_id,
        submission_type=args.submission_type,
        local=args.local,
       
    )

if __name__ == "__main__":
    main()
