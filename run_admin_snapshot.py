"""
run_admin_snapshot.py

Azure-only Admin Snapshot Aggregator

Builds:
--------
silver/admin/vendor_summary.parquet

One row per vendor (latest submission only)
"""

import os
from datetime import datetime
from typing import List, Optional

import pandas as pd
from azure.storage.blob import BlobServiceClient


# =========================================================
# CONFIG
# =========================================================

AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = os.getenv("SILVER_CONTAINER", "silver")

ANALYTICS_ROOT = "analytics"
POST_REVIEW_ROOT = "post_pricing_review"
ADMIN_ROOT = "admin"

if not AZURE_CONN_STR:
    raise ValueError("AZURE_STORAGE_CONNECTION_STRING not set")


# =========================================================
# AZURE CLIENT
# =========================================================

blob_service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
container = blob_service.get_container_client(SILVER_CONTAINER)


# =========================================================
# HELPERS
# =========================================================

def list_vendors() -> List[str]:
    vendors = set()
    prefix = f"{ANALYTICS_ROOT}/vendor_scorecard/"
    for blob in container.list_blobs(name_starts_with=prefix):
        parts = blob.name.split("/")
        for p in parts:
            if p.startswith("vendor="):
                vendors.add(p.split("=")[1])
    return sorted(vendors)


def get_latest_submission(vendor: str) -> Optional[str]:
    submissions = set()
    prefix = f"{ANALYTICS_ROOT}/vendor_scorecard/vendor={vendor}/"
    for blob in container.list_blobs(name_starts_with=prefix):
        parts = blob.name.split("/")
        for p in parts:
            if p.startswith("submission="):
                submissions.add(p.split("=")[1])

    if not submissions:
        return None

    return sorted(submissions)[-1]


def read_parquet(blob_path: str) -> pd.DataFrame:
    blob = container.get_blob_client(blob_path)
    stream = blob.download_blob()
    return pd.read_parquet(stream)


def safe_get(df: pd.DataFrame, column: str):
    if column in df.columns and not df.empty:
        return df[column].iloc[0]
    return None


# =========================================================
# CORE AGGREGATION
# =========================================================

def build_vendor_summary() -> pd.DataFrame:
    vendors = list_vendors()
    rows = []

    for vendor in vendors:
        latest_submission = get_latest_submission(vendor)

        if not latest_submission:
            continue

        try:
            profile_path = (
                f"{ANALYTICS_ROOT}/vendor_profiling/"
                f"vendor={vendor}/vendor_profile_{latest_submission}.parquet"
            )

            scorecard_path = (
                f"{ANALYTICS_ROOT}/vendor_scorecard/"
                f"vendor={vendor}/submission={latest_submission}/"
                f"vendor_scorecard_{latest_submission}.parquet"
            )

            review_changes_path = (
                f"{POST_REVIEW_ROOT}/vendor={vendor}/"
                f"submission={latest_submission}/review_changes.parquet"
            )

            integrity_path = (
                f"{POST_REVIEW_ROOT}/vendor={vendor}/"
                f"submission={latest_submission}/integrity.parquet"
            )

            profile_df = read_parquet(profile_path)
            scorecard_df = read_parquet(scorecard_path)

            # Optional layers
            delta_count = 0
            error_count = 0

            try:
                review_df = read_parquet(review_changes_path)
                delta_count = len(review_df)
            except:
                pass

            try:
                integrity_df = read_parquet(integrity_path)
                error_count = len(integrity_df[integrity_df["status"] == "ERROR"])
            except:
                pass

            row = {
                "vendor": vendor,
                "submission_id": latest_submission,
                "overall_score": safe_get(scorecard_df, "overall_score"),
                "completeness_pct": safe_get(profile_df, "completeness_pct"),
                "asset_coverage_pct": safe_get(profile_df, "asset_coverage_pct"),
                "autofix_success_pct": safe_get(scorecard_df, "autofix_success_pct"),
                "approval_rate_pct": safe_get(scorecard_df, "approval_rate_pct"),
                "rejection_rate_pct": safe_get(scorecard_df, "rejection_rate_pct"),
                "delta_count": delta_count,
                "error_count": error_count,
                "last_updated_utc": datetime.utcnow(),
            }

            rows.append(row)

        except Exception as e:
            print(f"⚠️ Failed processing vendor {vendor}: {e}")

    return pd.DataFrame(rows)


# =========================================================
# SAVE SNAPSHOT
# =========================================================

def save_snapshot(df: pd.DataFrame):
    output_path = f"{ADMIN_ROOT}/vendor_summary.parquet"
    blob = container.get_blob_client(output_path)
    blob.upload_blob(df.to_parquet(index=False), overwrite=True)
    print("✅ Admin snapshot written to silver/admin/vendor_summary.parquet")


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    print("📊 Building Azure Admin Snapshot...")
    summary_df = build_vendor_summary()

    if summary_df.empty:
        print("⚠️ No vendors found.")
    else:
        save_snapshot(summary_df)

    print("🚀 Admin snapshot complete.")