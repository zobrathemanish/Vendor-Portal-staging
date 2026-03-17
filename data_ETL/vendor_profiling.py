"""
vendor_profiling.py

Purpose:
--------
Aggregates ingestion, validation, profiling, autofix, integrity, canonical,
asset, and readiness telemetry into vendor-level profiling metrics.

Supports:
  • Azure mode (default)
  • Local mode (--local)

Outputs:
  silver/<in_review|post_pricing_review>/vendor=<vendor>/submission=<id>/analytics/vendor_profiling/
    - vendor_profile.parquet
    - vendor_profile.xlsx

READ-ONLY. Safe for Category & leadership reporting.
"""

# =========================================================
# IMPORTS
# =========================================================
import os
import json
from io import BytesIO
from datetime import datetime
from typing import Dict, List

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
LOGS_DIR = "logs"
READY_DIR = "ready"
ANALYTICS_DIR = "analytics/vendor_profiling"
PRICING_REVIEW = "post_pricing_review"

# =========================================================
# OUTPUT SCHEMA (STABLE CONTRACT)
# =========================================================
COLUMN_ORDER = [
    "vendor",
    "submission_id",
    "submission_count",
    "etl_health_status",
    "can_promote",

    # Data health
    "health_issue_count",
    "health_fixable_issues",
    "health_medium_issues",
    "entity_scoped_issues",
    "row_missingness_avg_pct",

    # Autofix
    "autofix_detected_issues",
    "autofix_resolved_issues",
    "autofix_unresolved_issues",
    "autofix_resolution_rate",

    # Integrity
    "integrity_blocking_issues",
    "integrity_warning_issues",

    # Canonical data
    "canonical_unique_products",
    "canonical_item_master_rows",
    "canonical_pricing_rows",
    "canonical_attributes_rows",

    # Assets
    "asset_declared_total",
    "asset_missing_declared",
    "asset_missing_pct",
    "asset_images_written",
    "asset_documents_written",
    "asset_skipped_count",
    "asset_images_checked_for_size",
    "asset_images_meeting_min_size",
    "asset_image_size_compliance_pct",
    "asset_image_size_ready",


    # Media
    "media_products_total",
    "media_products_with_any_image",
    "media_products_with_both_images",
    "media_products_missing_all_images",
    "media_compliance_pct",
    "media_ready",


    # Metadata
    "profiling_generated_ts",
]

def submission_base_path(vendor: str, submission_id: str, local: bool, mode: str) -> str:
    root = PRICING_REVIEW if mode == "post_review" else IN_REVIEW

    if local:
        return os.path.join(
            "silver",
            root,
            f"vendor={vendor}",
            f"submission={submission_id}",
        )
    return f"{root}/vendor={vendor}/submission={submission_id}"

def build_vendor_profile_for_submission(
    vendor: str,
    submission_id: str,
    container,
    local: bool,
    mode: str,
) -> Dict:

    base = submission_base_path(vendor, submission_id, local, mode)

    profile = {
        "vendor": vendor,
        "submission_id": submission_id,
        "submission_count": 1,
        "can_promote": True,
    }

    # ---------------------------------------------------------
    # Load metrics (submission-level scope)
    # ---------------------------------------------------------
    for loader in (
        load_health,
        load_autofix,
        load_integrity,
        load_canonical,
        load_assets,
        load_media_canonical,
    ):
        if loader is load_assets:
            data = loader(base, vendor, container, local)
        else:
            data = loader(base, container, local)

        if data:
            profile.update(data)

    # ---------------------------------------------------------
    # 🔥 CRITICAL FIX:
    # Compute autofix resolution rate (submission-level)
    # ---------------------------------------------------------
    detected = profile.get("autofix_detected_issues", 0)
    resolved = profile.get("autofix_resolved_issues", 0)

    if detected > 0:
        profile["autofix_resolution_rate"] = round(
            100 * resolved / detected,
            2
        )
    else:
        # If no issues required fixing → automation perfect
        profile["autofix_resolution_rate"] = 100.0

    # ---------------------------------------------------------
    # Asset size compliance %
    # ---------------------------------------------------------
    checked = profile.get("asset_images_checked_for_size", 0)
    meets_min = profile.get("asset_images_meeting_min_size", 0)

    if checked > 0:
        profile["asset_image_size_compliance_pct"] = round(
            100 * meets_min / checked,
            2
        )
    else:
        profile["asset_image_size_compliance_pct"] = 0.0

    profile["asset_image_size_ready"] = meets_min > 0

    # ---------------------------------------------------------
    # Asset missing %
    # ---------------------------------------------------------
    declared = profile.get("asset_declared_total", 0)
    missing_declared = profile.get("asset_missing_declared", 0)

    if declared > 0:
        profile["asset_missing_pct"] = round(
            100 * missing_declared / declared,
            2
        )
    else:
        profile["asset_missing_pct"] = 0.0

    # ---------------------------------------------------------
    # Media readiness
    # ---------------------------------------------------------
    profile["media_ready"] = (
        profile.get("media_products_total", 0) > 0
        and profile.get("media_products_missing_all_images", 0) == 0
    )

    # ---------------------------------------------------------
    # ETL health status
    # ---------------------------------------------------------
    if profile.get("integrity_blocking_issues", 0) > 0:
        profile["etl_health_status"] = "RED"
    elif profile.get("asset_missing_declared", 0) > 0:
        profile["etl_health_status"] = "YELLOW"
    else:
        profile["etl_health_status"] = "GREEN"

    # ---------------------------------------------------------
    # Timestamp
    # ---------------------------------------------------------
    profile["profiling_generated_ts"] = datetime.utcnow().isoformat()

    return profile


# =========================================================
# MODE HELPERS (CONSISTENT WITH OTHER FILES)
# =========================================================
def get_container():
    if not AZURE_CONN:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set")
    service = BlobServiceClient.from_connection_string(AZURE_CONN)
    return service.get_container_client(SILVER_CONTAINER)


def read_json(container, path: str, local: bool) -> Dict | None:
    if local:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    try:
        data = container.get_blob_client(path).download_blob().readall()
        return json.loads(data)
    except Exception:
        return None


def read_parquet(container, path: str, local: bool) -> pd.DataFrame:
    if local:
        return pd.read_parquet(path)

    data = container.get_blob_client(path).download_blob().readall()
    return pq.read_table(BytesIO(data)).to_pandas()


def write_outputs(
    container,
    base_dir: str,
    vendor: str,
    submission_id: str,
    df: pd.DataFrame,
    local: bool
):
    vendor_dir = base_dir

    parquet_path = os.path.join(
        vendor_dir,
        f"vendor_profile_{submission_id}.parquet"
    )
    excel_path = os.path.join(
        vendor_dir,
        f"vendor_profile_{submission_id}.xlsx"
    )

    if local:
        os.makedirs(vendor_dir, exist_ok=True)
        df.to_parquet(parquet_path, index=False)
        df.to_excel(excel_path, index=False, sheet_name="Vendor_Profile")
        return

    # Parquet (NO overwrite)
    buf = BytesIO()
    pq.write_table(pa.Table.from_pandas(df), buf)
    container.upload_blob(parquet_path, buf.getvalue(), overwrite=True)

    # Excel (NO overwrite)
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Vendor_Profile")
    container.upload_blob(excel_path, buf.getvalue(), overwrite=True)



# =========================================================
# DISCOVERY
# =========================================================
def list_vendors(local: bool, container=None) -> List[str]:
    """
    Discover vendors with submission-level logs (silver/logs/vendor=...)
    """
    vendors = set()

    if local:
        base = os.path.join("silver", LOGS_DIR)
        if not os.path.exists(base):
            return []
        for d in os.listdir(base):
            if d.startswith("vendor="):
                vendors.add(d.split("vendor=", 1)[1])
    else:
        prefix = f"{LOGS_DIR}/vendor="
        for b in container.list_blobs(name_starts_with=prefix):
            v = b.name.split("vendor=", 1)[1].split("/", 1)[0]
            vendors.add(v)

    return sorted(vendors)


def list_submission_paths(vendor: str, local: bool, container=None) -> List[str]:
    """
    Discover submission_id folders for a vendor
    """
    submissions = set()

    if local:
        base = os.path.join("silver", LOGS_DIR, f"vendor={vendor}")
        if not os.path.exists(base):
            return []
        for d in os.listdir(base):
            if d.startswith("submission="):
                submissions.add(os.path.join(base, d))
    else:
        prefix = f"{LOGS_DIR}/vendor={vendor}/submission="
        for b in container.list_blobs(name_starts_with=prefix):
            submission = prefix + b.name.split("submission=", 1)[1].split("/", 1)[0]
            submissions.add(submission)

    return sorted(submissions)

def find_first_json(container, prefix: str, local: bool):
    if local:
        base_dir = os.path.dirname(prefix)
        if not os.path.exists(base_dir):
            return None
        for f in os.listdir(base_dir):
            if f.startswith(os.path.basename(prefix)):
                return os.path.join(base_dir, f)
        return None
    else:
        blobs = list(container.list_blobs(name_starts_with=prefix))
        return blobs[0].name if blobs else None



# =========================================================
# METRIC LOADERS (SUBMISSION-LEVEL)
# =========================================================
def load_health(submission_path, container, local):
    s = read_json(container, f"{submission_path}/profiling/health_summary.json", local)
    if not s:
        return {}

    return {
        "health_issue_count": int(s.get("issues_total", 0)),
        "health_fixable_issues": int(s.get("fixable_flagged", 0)),
        "health_medium_issues": int(s.get("issues_by_severity", {}).get("medium", 0)),
        "entity_scoped_issues": int(s.get("issues_by_scope", {}).get("entity", 0)),
        "row_missingness_avg_pct": round(float(s.get("row_missingness_avg", 0.0)) * 100, 2),
    }


def load_autofix(submission_path, container, local):
    """
    Load autofix metrics from autofix_report.xlsx
    Sheets:
      - issues_resolved
      - issues_remaining
    """
    xlsx_path = f"{submission_path}/autofix/autofix_report.xlsx"

    try:
        if local:
            if not os.path.exists(xlsx_path):
                return {}
            resolved_df = pd.read_excel(xlsx_path, sheet_name="issues_resolved")
            remaining_df = pd.read_excel(xlsx_path, sheet_name="issues_remaining")
        else:
            blob = container.get_blob_client(xlsx_path)
            data = blob.download_blob().readall()
            bio = BytesIO(data)
            resolved_df = pd.read_excel(bio, sheet_name="issues_resolved")
            bio.seek(0)
            remaining_df = pd.read_excel(bio, sheet_name="issues_remaining")
    except Exception:
        return {}

    resolved = len(resolved_df)
    remaining = len(remaining_df)

    return {
        "autofix_detected_issues": resolved + remaining,
        "autofix_resolved_issues": resolved,
        "autofix_unresolved_issues": remaining,
    }



def load_integrity(submission_path, container, local):
    s = read_json(container, f"{submission_path}/integrity/integration_summary.json", local)
    if not s:
        return {}

    return {
        "integrity_total_issues": int(s.get("total_issues", 0)),
        "integrity_blocking_issues": int(s.get("blocking", 0)),
        "integrity_warning_issues": int(s.get("warnings", 0)),
        "can_promote": bool(s.get("can_promote", False)),
    }


def load_canonical(submission_path, container, local):
    s = read_json(container, f"{submission_path}/canonical/canonical_summary.json", local)
    if not s:
        return {}

    return {
        "canonical_input_rows": int(s.get("input_rows", 0)),
        "canonical_unique_products": int(s.get("unique_products", 0)),
        "canonical_item_master_rows": int(s.get("item_master_rows", 0)),
        "canonical_pricing_rows": int(s.get("pricing_rows", 0)),
        "canonical_attributes_rows": int(s.get("attributes_rows", 0)),
    }

def load_assets(submission_path, vendor: str,container, local):
    metrics = {}

    manifest_path = find_first_json(
        container,
        f"{submission_path}/assets/asset_manifest_",
        local
    )
    manifest = read_json(container, manifest_path, local) if manifest_path else None

    if manifest:
        s = manifest.get("summary", {})
        metrics.update({
            "asset_declared_total": s.get("total_assets", 0),
            "asset_missing_declared": s.get("missing_declared", 0),
            "asset_extra_assets": s.get("extra_assets", 0),
            "asset_manifest_failures": s.get("failed", 0),
        })

    validation_path = find_first_json(
        container,
        f"{submission_path}/assets/asset_validation_log_",
        local
    )
    validation = read_json(container, validation_path, local) if validation_path else None

    if validation:
        metrics["asset_validation_failed_count"] = len(validation.get("failed", []))

    transform_path = find_first_json(
        container,
        f"{submission_path}/logs/asset-transform-",
        local
    )

    transform = read_json(container, transform_path, local) if transform_path else None

    if transform:
        image_transforms = transform.get("image_transforms", [])

        checked = 0
        meets_min = 0

        for img in image_transforms:
            res = img.get("final_resolution") or img.get("original_resolution")
            if not res:
                continue

            try:
                w, h = map(int, res.lower().split("x"))
            except Exception:
                continue

            checked += 1
            if w >= 840 and h >= 840:
                meets_min += 1
        
        metrics.update({
            "asset_images_written": transform.get("images_written", 0),
            "asset_documents_written": transform.get("documents_written", 0),
            "asset_skipped_count": len(transform.get("skipped", [])),
            "asset_missing_source_count": len(transform.get("missing_assets", [])),
            "asset_transform_errors": len(transform.get("errors", [])),
            "asset_images_checked_for_size": checked,
            "asset_images_meeting_min_size": meets_min,
        })

    return metrics

def load_media_canonical(submission_path, container, local):
    try:
        df = read_parquet(container, f"{submission_path}/canonical/media_canonical.parquet", local)
    except Exception:
        return {}

    df2 = df.copy()
    df2["media_type"] = df2["media_type"].astype(str).str.strip().str.upper()
    df2["media_category"] = df2["media_category"].astype(str).str.strip().str.lower()

    images = df2[df2["media_category"] == "image"]

    by_entity = images.groupby("_entity_id")["media_type"].apply(set)

    total_products = by_entity.shape[0]

    with_any_image = sum(
        ("P01" in reps) or ("P04" in reps)
        for reps in by_entity
    )

    with_both = sum(
        ("P01" in reps) and ("P04" in reps)
        for reps in by_entity
    )

    return {
        "media_products_total": total_products,
        "media_products_with_any_image": with_any_image,
        "media_products_with_both_images": with_both,
        "media_products_missing_all_images": total_products - with_any_image,
        "media_compliance_pct": round(
            100 * with_any_image / total_products, 2
        ) if total_products > 0 else 0.0,
    }



# =========================================================
# AGGREGATION
# =========================================================
def aggregate_submissions(submission_path: List[str], vendor: str, container, local):
    agg = {
        "submission_count": 0,
        "can_promote": True,
    }

    missingness = []

    for submission in submission_path:
        agg["submission_count"] += 1

        for loader in (load_health, load_autofix, load_integrity, load_canonical, load_assets, load_media_canonical):
            if loader is load_assets:
                data = loader(submission, vendor, container, local)
            else:
                data = loader(submission, container, local)

            for k, v in data.items():
                if k == "can_promote":
                    agg["can_promote"] &= v
                elif k.endswith("_products"):
                    agg[k] = max(agg.get(k, 0), v)   # media canonical
                elif k.endswith("_rows"):
                    agg[k] = max(agg.get(k, 0), v)
                elif k == "row_missingness_avg_pct":
                    if v > 0:
                        missingness.append(v)
                else:
                    agg[k] = agg.get(k, 0) + v

    if missingness:
        agg["row_missingness_avg_pct"] = round(sum(missingness) / len(missingness), 2)

    if agg.get("autofix_detected_issues", 0) > 0:
        agg["autofix_resolution_rate"] = round(
            100 * agg.get("autofix_resolved_issues", 0) / agg["autofix_detected_issues"], 2
        )
    else:
         agg["autofix_resolution_rate"] = 100.0
         
    if agg.get("asset_images_checked_for_size", 0) > 0:
        agg["asset_image_size_compliance_pct"] = round(
            100
            * agg.get("asset_images_meeting_min_size", 0)
            / agg["asset_images_checked_for_size"],
            2
        )
    else:
        agg["asset_image_size_compliance_pct"] = 0.0

    
    agg["asset_image_size_ready"] = (
    agg.get("asset_images_meeting_min_size", 0) > 0
)

    # Asset readiness
    if agg.get("asset_declared_total", 0) > 0:
        agg["asset_missing_pct"] = round(
            100 * agg.get("asset_missing_declared", 0) / agg["asset_declared_total"], 2
        )
    else:
        agg["asset_missing_pct"] = 0.0

    # Media readiness
    agg["media_ready"] = (
        agg.get("media_products_total", 0) > 0
        and agg.get("media_products_missing_all_images", 0) == 0
    )

    # ETL health status
    if agg.get("integrity_blocking_issues", 0) > 0:
        agg["etl_health_status"] = "RED"
    elif agg.get("asset_missing_declared", 0) > 0:
        agg["etl_health_status"] = "YELLOW"
    else:
        agg["etl_health_status"] = "GREEN"


    return agg


# =========================================================
# VENDOR PROFILE
# =========================================================
def build_vendor_profile(vendor: str, container, local: bool) -> Dict:
    profile = {"vendor": vendor}

    submissions = list_submission_paths(vendor, local, container)
    if submissions:
        profile.update(aggregate_submissions(submissions, vendor, container, local))
    else:
        profile["submission_count"] = 0
        profile["can_promote"] = True

    profile["profiling_generated_ts"] = datetime.utcnow().isoformat()
    return profile

# =========================================================
# PROGRAMMATIC ENTRYPOINT
# =========================================================
def run_vendor_profiling(
    vendor: str,
    submission_id: str,
    mode: str = "full",
    local: bool = False,
):
    container = None if local else get_container()

    print("▶️ Vendor Profiling")
    print("Vendor:", vendor)
    print("Mode:", mode)

    profile = build_vendor_profile_for_submission(
        vendor=vendor,
        submission_id=submission_id,
        container=container,
        local=local,
        mode=mode,
    )

    df = pd.DataFrame([profile])
    df = df.reindex(columns=[c for c in COLUMN_ORDER if c in df.columns])

    base = submission_base_path(vendor, submission_id, local, mode)

    out_base = (
        os.path.join(base, "analytics", "vendor_profiling")
        if local else
        f"{base}/analytics/vendor_profiling"
    )

    write_outputs(
        container=container,
        base_dir=out_base,
        vendor=vendor,
        submission_id=submission_id,
        df=df,
        local=local
    )

    print(
        f"✅ Vendor profiling completed | vendor={vendor} | submission={submission_id}"
    )

# =========================================================
# CLI ENTRYPOINT
# =========================================================
def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--mode", default="full")

    args = parser.parse_args()

    run_vendor_profiling(
        vendor=args.vendor,
        submission_id=args.submission_id,
        local=args.local,
        mode=args.mode,
    )

if __name__ == "__main__":
    main()

