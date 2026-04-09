import json
from io import BytesIO
from datetime import datetime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


# =========================================================
# IO HELPERS
# =========================================================
def read_df(container, path: str) -> pd.DataFrame:
    data = container.get_blob_client(path).download_blob().readall()
    return pq.read_table(BytesIO(data)).to_pandas()


def write_df(container, path: str, df: pd.DataFrame):
    buf = BytesIO()
    pq.write_table(pa.Table.from_pandas(df), buf)
    container.upload_blob(path, buf.getvalue(), overwrite=True)


def write_json(container, path: str, obj: dict):
    container.upload_blob(
        path,
        json.dumps(obj, indent=2).encode(),
        overwrite=True
    )


# =========================================================
# MAIN
# =========================================================
def run_unified_integrity(container, vendor: str) -> bool:

    print(f"\n🧠 UNIFIED INTEGRITY → vendor={vendor}")

    unified_path = (
        f"approved/unified_workflow/vendor={vendor}/unified_etl_mapped.parquet"
    )

    try:
        df = read_df(container, unified_path)
    except Exception as e:
        print("❌ Failed to load unified dataset:", e)
        return True

    if df.empty:
        print("⚠️ Unified dataset empty")
        return True

    df["Part Number"] = df["Part Number"].astype(str).str.strip()

    issues = []

    # =====================================================
    # SECTION SPLITS
    # =====================================================
    item_master = df[df["__Section"] == "Item_Master"]
    pricing = df[df["__Section"] == "Pricing"]
    assets = df[df["__Section"] == "Digital_Assets"]

    product_parts = set(item_master["Part Number"]) if not item_master.empty else set()
    pricing_parts = set(pricing["Part Number"]) if not pricing.empty else set()
    asset_parts = set(assets["Part Number"]) if not assets.empty else set()

    # =====================================================
    # 🔴 1. PRICING WITHOUT PRODUCT (BLOCKING)
    # =====================================================
    for p in pricing_parts - product_parts:
        issues.append({
            "part_number": p,
            "issue_type": "pricing_without_product",
            "severity": "blocking",
            "details": "Pricing exists but no product"
        })

    # =====================================================
    # 🔴 2. PRODUCT WITHOUT PRICING (BLOCKING)
    # =====================================================
    for p in product_parts - pricing_parts:
        issues.append({
            "part_number": p,
            "issue_type": "item_without_pricing",
            "severity": "blocking",
            "details": "Product exists without pricing"
        })

    # =====================================================
    # ⚠️ 3. PRODUCT WITHOUT ASSETS (WARNING)
    # =====================================================
    for p in product_parts - asset_parts:
        issues.append({
            "part_number": p,
            "issue_type": "item_without_assets",
            "severity": "warning",
            "details": "Product exists without assets"
        })

    # =====================================================
    # ⚠️ 4. PRICING WITHOUT ASSETS (OPTIONAL WARNING)
    # =====================================================
    for p in pricing_parts - asset_parts:
        issues.append({
            "part_number": p,
            "issue_type": "pricing_without_assets",
            "severity": "warning",
            "details": "Pricing exists without assets"
        })

    # =====================================================
    # BUILD OUTPUT
    # =====================================================
    issues_df = pd.DataFrame(issues).drop_duplicates() if issues else pd.DataFrame()

    blocking = int(
        (issues_df["severity"] == "blocking").sum()
    ) if not issues_df.empty else 0

    warnings = int(
        (issues_df["severity"] == "warning").sum()
    ) if not issues_df.empty else 0

    summary = {
        "vendor": vendor,
        "checked_at": datetime.utcnow().isoformat(),
        "total_issues": len(issues_df),
        "blocking": blocking,
        "warnings": warnings,
        "can_publish": blocking == 0
    }

    base = f"unified_integrity/vendor={vendor}"

    write_df(container, f"{base}/integrity_issues.parquet", issues_df)
    write_json(container, f"{base}/integrity_summary.json", summary)

    print("🧾 Summary:", summary)

    return summary["can_publish"]