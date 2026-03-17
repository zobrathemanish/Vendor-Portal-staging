"""
admin_snapshot.py

Purpose:
--------
Incrementally update admin/vendor_summary.parquet
for a single vendor submission.

Azure-only.
"""

import os
from io import BytesIO
from datetime import datetime
import pandas as pd
from azure.storage.blob import BlobServiceClient

AZURE_CONN = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = "silver"
ADMIN_PATH = "admin/vendor_summary.parquet"


def get_container():
    if not AZURE_CONN:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set")
    return BlobServiceClient.from_connection_string(
        AZURE_CONN
    ).get_container_client(SILVER_CONTAINER)


def safe_get(df: pd.DataFrame, column: str):
    if column in df.columns and not df.empty:
        return df[column].iloc[0]
    return None


def update_admin_summary(
    vendor: str,
    submission_id: str,
    profile_df: pd.DataFrame,
    scorecard_df: pd.DataFrame,
):
    container = get_container()

    # ---------- Derive metrics ----------
    completeness = None
    missing_pct = safe_get(profile_df, "row_missingness_avg_pct")
    if missing_pct is not None:
        completeness = round(100 - missing_pct, 2)

    asset_coverage = None
    asset_missing = safe_get(profile_df, "asset_missing_pct")
    if asset_missing is not None:
        asset_coverage = round(100 - asset_missing, 2)

    row = {
        "vendor": vendor,
        "submission_id": submission_id,
        "overall_score": safe_get(scorecard_df, "overall_score"),
        "completeness_pct": completeness,
        "asset_coverage_pct": asset_coverage,
        "autofix_success_pct": safe_get(profile_df, "autofix_resolution_rate"),
        "integrity_blocking_issues": safe_get(profile_df, "integrity_blocking_issues"),
        "can_promote": safe_get(profile_df, "can_promote"),
        "last_updated_utc": datetime.utcnow().isoformat(),
    }

    new_row_df = pd.DataFrame([row])

    # ---------- Load existing admin file ----------
    try:
        blob = container.get_blob_client(ADMIN_PATH)
        existing_data = blob.download_blob().readall()
        admin_df = pd.read_parquet(BytesIO(existing_data))
    except Exception:
        admin_df = pd.DataFrame()

    # ---------- Replace vendor row ----------
    if admin_df.empty:
        updated_df = new_row_df
    else:
        admin_df = admin_df[admin_df["vendor"] != vendor]
        updated_df = pd.concat(
            [admin_df.astype(object), new_row_df.astype(object)],
            ignore_index=True
        )

    # ---------- Write back ----------
    buf = BytesIO()
    updated_df.to_parquet(buf, index=False)
    buf.seek(0)

    container.upload_blob(ADMIN_PATH, buf, overwrite=True)

    print(f"📊 Admin summary updated | vendor={vendor}")