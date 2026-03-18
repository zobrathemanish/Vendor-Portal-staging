# scripts/pre_etl_validation.py
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from mapping_validation.mapping_engine.xml_mapper import XMLMapper

import os
import json
from typing import Dict, Any, List
import datetime
import pandas as pd
import yaml
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

from mapping_validation.validation_engine import ValidationEngine
import zipfile
from io import BytesIO
import time
import argparse
from common.status_writer import write_status

load_dotenv()
AZURE_CONN = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
blob_service = BlobServiceClient.from_connection_string(AZURE_CONN)

BRONZE_CONTAINER = "bronze"
SILVER_CONTAINER = "silver"

def silver_blob_exists(blob_path: str) -> bool:
    try:
        blob_service.get_container_client(SILVER_CONTAINER) \
            .get_blob_client(blob_path) \
            .get_blob_properties()
        return True
    except Exception:
        return False
    
def bronze_blob_exists(blob_path: str) -> bool:
    try:
        blob_service.get_container_client(BRONZE_CONTAINER) \
            .get_blob_client(blob_path) \
            .get_blob_properties()
        return True
    except Exception:
        return False    

def load_parquet_from_silver(blob_path: str) -> pd.DataFrame | None:
    try:
        blob = blob_service.get_container_client(SILVER_CONTAINER) \
                           .get_blob_client(blob_path)
        data = blob.download_blob().readall()
        return pd.read_parquet(BytesIO(data))
    except Exception:
        return None
    
def load_parquet_from_bronze(blob_path: str):
    try:
        blob = blob_service.get_container_client(BRONZE_CONTAINER) \
                           .get_blob_client(blob_path)
        data = blob.download_blob().readall()
        print(f"📥 Downloaded {len(data)} bytes from {blob_path}")
        return pd.read_parquet(BytesIO(data))
    except Exception as e:
        print(f"❌ FAILED reading parquet: {blob_path}")
        print(type(e).__name__, e)
        raise   # ← IMPORTANT

# -------------------------------
# CONFIG
# -------------------------------

MAPPED_DIR = "mapped"                  # Output from mapping
SILVER_BASE_DIR = os.path.join("silver", "in_review")
VALIDATION_CONFIG_DIR = "validation_config"    # external YAML folder

# ================================================================
# 1. LOAD VALIDATION CONFIG
# ================================================================
def load_validation_config(vendor: str) -> Dict[str, Any] | None:
    fname = vendor.lower().strip() + ".yaml"
    path = os.path.join(VALIDATION_CONFIG_DIR, fname)

    if not os.path.exists(path):
        print(f"[WARNING]  No validation config YAML found: {path}")
        return None

    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

# ================================================================
# SIMPLE PRICE CLEANER (reused)
# ================================================================
import re

def clean_price_generic(val):
    if val is None:
        return None
    text = str(val)
    text = re.sub(r'[^\d.,-]', '', text)
    text = text.replace(",", "")
    if text.strip() == "":
        return None
    try:
        return float(text)
    except Exception:
        return None

def preprocess_pricing(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None or df.empty:
        return df
    for col in df.columns:
        if "price" in col.lower() or "cost" in col.lower():
            df[col] = df[col].apply(clean_price_generic)
    return df

# ================================================================
# 3. Promote to Silver
# ================================================================

def promote_to_silver(vendor: str, workflow:str, submission_id: str, submission_type: str):

    """
    Canonical promotion step:
    - Upload mapped.parquet + mapped.xlsx
    - Promote & unzip assets from bronze → silver
    """

    print(f"  🚀 Promoting vendor '{vendor}' to silver")

    silver_client = blob_service.get_container_client("silver")
    bronze_client = blob_service.get_container_client("bronze")

    mapped_dir = os.path.join(
        "mapped",
        f"vendor={vendor}",
        f"submission={submission_id}"
    )

    # ------------------------------
    # PROMOTE MAPPED DATA (bronze → silver)
    # ------------------------------
    bronze_mapped_prefix = (
        f"raw/vendor={vendor}/"
        f"{workflow}/"
        f"submission={submission_id}/"
        f"submission_type={submission_type}/"
        f"mapped/"
    )
    silver_mapped_prefix = (
        f"in_review/{workflow}_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}/"
        f"mapped/"
    )

    bronze_client = blob_service.get_container_client("bronze")
    silver_client = blob_service.get_container_client("silver")

    blobs = bronze_client.list_blobs(name_starts_with=bronze_mapped_prefix)

    found_mapped = False

    for blob in blobs:
        filename = os.path.basename(blob.name)
        if not filename:
            continue

        found_mapped = True
        target_blob = silver_mapped_prefix + filename

        data = bronze_client.download_blob(blob.name).readall()
        silver_client.upload_blob(
            name=target_blob,
            data=data,
            overwrite=True
        )

        print(f"📄 Promoted mapped file → silver/{target_blob}")

    if not found_mapped:
        raise RuntimeError(
            f"Mapped data missing in bronze for vendor={vendor}, submission={submission_id}"
        )


    # ------------------------------
    # PROMOTE ASSETS (bronze → silver)
    # ------------------------------
    bronze_prefix = (
        f"raw/vendor={vendor}/"
        f"{workflow}/"
        f"submission={submission_id}/assets/"
    )
    silver_prefix = f"in_review/vendor={vendor}/submission={submission_id}/assets_staging/"

    blobs = bronze_client.list_blobs(name_starts_with=bronze_prefix)
    found_assets = False

    for blob in blobs:
        if not blob.name.lower().endswith(".zip"):
            continue

        found_assets = True
        print(f"    🖼️ Processing asset zip → {blob.name}")

        # ------------------------------
        # Download ZIP
        # ------------------------------
        zip_bytes = bronze_client.download_blob(blob.name).readall()
        zip_size_mb = len(zip_bytes) / (1024 * 1024)

        with zipfile.ZipFile(BytesIO(zip_bytes)) as z:
            members = [m for m in z.namelist() if not m.endswith("/")]
            total_files = len(members)

            print(f"      📦 ZIP size: {zip_size_mb:.2f} MB")
            print(f"      📂 Files in ZIP: {total_files}")

            uploaded = 0
            LOG_EVERY = 50           # log every N files
            LOG_INTERVAL_SEC = 10    # heartbeat
            last_log = time.time()

            for member in members:
                filename = os.path.basename(member)
                if not filename:
                    continue

                target_blob = silver_prefix + filename

                with z.open(member) as f:
                    silver_client.upload_blob(
                        name=target_blob,
                        data=f.read(),
                        overwrite=True
                    )

                uploaded += 1
                now = time.time()

                # Count-based progress
                if uploaded % LOG_EVERY == 0 or uploaded == total_files:
                    print(
                        f"      ⏳ Uploaded {uploaded}/{total_files} "
                        f"({uploaded/total_files*100:.1f}%)"
                    )

                # Time-based heartbeat (prevents "frozen" feeling)
                elif now - last_log > LOG_INTERVAL_SEC:
                    print(
                        f"      ⏳ Still processing assets… "
                        f"{uploaded}/{total_files} done"
                    )
                    last_log = now

            print(f" ✅ Finished ZIP → {uploaded} files uploaded")


    if found_assets:
        print(f"    ✅ Assets promoted → silver/{silver_prefix}")
    else:
        print("    ℹ️ No assets found in bronze")

    print(f"  🎉 Vendor '{vendor}' promotion complete.\n")

    return True


# LOCAL_DATA_ROOT = os.getenv("LOCAL_DATA_ROOT", "./local_data")

# ================================================================
# 4. MAIN PER-VENDOR VALIDATION PIPELINE USING ENGINE
# ================================================================
def assert_identifier_integrity(df, col="Part Number"):
    if col in df.columns:
        bad = (
            df[col]
            .astype(str)
            .str.match(r"^\d+$")
            & (df[col].str.len() < 5)   # adjust per vendor if needed
        )
        if bad.any():
            raise RuntimeError(
                f"Identifier corruption detected in validation for {col}"
            )

def process_vendor(vendor: str, workflow: str,submission_type: str, submission_id: str):
    if not submission_id:
        raise RuntimeError("submission_id is REQUIRED")

    print(f"\n Running validation for vendor: {vendor}")

    success_marker = (
        f"in_review/{workflow}_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}/"
        f"mapped/_SUCCESS.json"
    )

    if not silver_blob_exists(success_marker):
        write_status(
            vendor=vendor,
            stage="VALIDATION",
            status="FAILED",
            message="Mapping not finalized (_SUCCESS.json missing)",
            submission_id=submission_id,
        )
        raise RuntimeError("MAPPING_NOT_FINALIZED")


    cfg_file = load_validation_config(vendor)
    if not cfg_file:
        print(f"[WARNING] Skipping vendor '{vendor}' — no YAML config.")
        return

    vcfg = cfg_file.get("validation", {})
    # -------------------------------------------------
    # GLOBAL VALIDATION SWITCH
    # -------------------------------------------------
    if vcfg.get("enabled", True) is False:
        write_status(
            vendor=vendor,
            stage="VALIDATION",
            status="SKIPPED",
            message="Validation disabled for vendor",
            submission_id=submission_id,
        )
        promote_to_silver(vendor, workflow, submission_id, submission_type)
        return

    base = (
        f"in_review/{workflow}_workflow/"
        f"vendor={vendor}/"
        f"submission_type={submission_type}/"
        f"submission={submission_id}/"
        f"mapped"
    )

    df_item   = load_parquet_from_silver(f"{base}/Item_Master.parquet")
    df_desc   = load_parquet_from_silver(f"{base}/Descriptions.parquet")
    df_price  = load_parquet_from_silver(f"{base}/Pricing.parquet")
    df_assets = load_parquet_from_silver(f"{base}/Digital_Assets.parquet")
    df_ext    = load_parquet_from_silver(f"{base}/Extended_Info.parquet")
    df_attr   = load_parquet_from_silver(f"{base}/Attributes.parquet")
    df_pi     = load_parquet_from_silver(f"{base}/Part_InterChange.parquet")
    df_pkg    = load_parquet_from_silver(f"{base}/Packages.parquet")

    try:
        assert_identifier_integrity(df_item)
    except Exception as e:
        write_status(
            vendor=vendor,
            stage="VALIDATION",
            status="FAILED",
            message=str(e),
            submission_id=submission_id
        )
        raise

    if df_item is None or df_item.empty:
        print("❌ Item_Master.parquet is EMPTY. Cannot validate.")
        return

    # Clean pricing numerics for generic numeric rules
    df_price = preprocess_pricing(df_price)

    engine = ValidationEngine(vendor, vcfg)
    errors: List[Dict[str, Any]] = []

    # Provide context for section rules (item_master for assets etc.)
    context = {
        "item_master": df_item,
        "extended_info": df_ext,
    }

    # Map YAML section keys → df + human names
    section_map = {
        "item_master":      ("Item Master", df_item),
        "descriptions":     ("Descriptions", df_desc),
        "extended_info":    ("Extended Info", df_ext),
        "attributes":       ("Attributes", df_attr),
        "part_interchange": ("Part InterChange", df_pi),
        "packages":         ("Packages", df_pkg),
        "pricing":          ("Pricing", df_price),
        "assets":           ("Digital Assets", df_assets),
    }

    section_validity: Dict[str, pd.Series | None] = {}

    for key, (pretty, df_section) in section_map.items():
        sec_cfg = vcfg.get(key, {})
        if not sec_cfg:
            # no rules defined → treat as all valid
            section_validity[key] = pd.Series(True, index=df_section.index) if df_section is not None else None
            continue

        if key == "item_master":
            mask = engine._validate_item_master(df_section, sec_cfg, errors)

        elif key == "descriptions":
            mask = engine._validate_descriptions(df_item, df_section, sec_cfg, errors)

        elif key == "extended_info":
            mask = engine._validate_extended_info(df_item, df_section, sec_cfg, errors)

        elif key == "attributes":
            mask = engine._validate_attributes(df_section, sec_cfg, errors)

        elif key == "part_interchange":
            mask = engine._validate_part_interchange(df_section, sec_cfg, errors)

        elif key == "packages":
            mask = engine._validate_packages(df_item, df_section, sec_cfg, errors)

        elif key == "pricing":
            mask = engine._validate_pricing(df_item, df_section, sec_cfg, errors)

        elif key == "assets":
            mask = engine._validate_assets(df_item, df_section, sec_cfg, errors)

        else:
            mask = pd.Series(True, index=df_section.index)

        section_validity[key] = mask

    # ===============================
    # BUILD SKU-LEVEL FLAGS (like before)
    # ===============================
    sku_col = "Part Number"
    df_flags = df_item.copy()
    df_flags["is_item_master_valid"] = section_validity.get("item_master", pd.Series(True, index=df_item.index))

    # Descriptions – aggregate per SKU
    if df_desc is not None and section_validity.get("descriptions") is not None:
        desc_valid_mask = section_validity["descriptions"]
        sku_valid_desc = (
            df_desc.assign(_v=desc_valid_mask)
                  .groupby(sku_col)["_v"]
                  .any()
        )
        df_flags["has_valid_descriptions"] = df_flags[sku_col].map(sku_valid_desc).fillna(False)
    else:
        df_flags["has_valid_descriptions"] = False

    # Pricing
    if df_price is not None and section_validity.get("pricing") is not None:
        price_valid_mask = section_validity["pricing"]
        sku_valid_price = (
            df_price.assign(_v=price_valid_mask)
                    .groupby(sku_col)["_v"]
                    .any()
        )
        df_flags["has_pricing"] = df_flags[sku_col].isin(df_price[sku_col].unique())
        df_flags["has_valid_pricing"] = df_flags[sku_col].map(sku_valid_price).fillna(False)
    else:
        df_flags["has_pricing"] = False
        df_flags["has_valid_pricing"] = False

    # Assets
    if df_assets is not None and section_validity.get("assets") is not None:
        sku_has_assets = df_assets.groupby(sku_col).size() > 0
        df_flags["has_assets"] = df_flags[sku_col].map(sku_has_assets).fillna(False)
    else:
        df_flags["has_assets"] = False

    # overall
    df_flags["is_overall_valid"] = (
        df_flags["is_item_master_valid"]
        & df_flags["has_valid_descriptions"]
        & df_flags["has_valid_pricing"]
        & df_flags["has_assets"]
    )

    # ===============================
    # WRITE OUTPUTS TO SILVER
    # ===============================
    # silver_client = blob_service.get_container_client(SILVER_CONTAINER)

    # flags_blob = (
    #     f"in_review/vendor={vendor}/"
    #     f"submission={submission_id}/validation/"
    #     f"item_master_with_validation.parquet"
    # )

    # buf = BytesIO()
    # df_flags.to_parquet(buf, index=False)
    # buf.seek(0)

    # silver_client.upload_blob(
    #     name=flags_blob,
    #     data=buf,
    #     overwrite=True
    # )

    # print(f"☁️ Validation flags written → silver/{flags_blob}")

    if errors:
        df_err = pd.DataFrame(errors)

        for col in ["Part Number", "Field", "Vendor", "Section", "ErrorCode", "Message"]:
            if col in df_err.columns:
                df_err[col] = df_err[col].astype(str)

        # ------------------------------
        # Prepare rejected folder & filenames
        # ------------------------------
        rejected_prefix = (
            f"rejected/logs/vendor={vendor}/"
            f"submission={submission_id}/"
        )

        ts_str = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
        safe_vendor = vendor.replace(" ", "_")

        json_name = f"{safe_vendor}-VALIDATION-{ts_str}.json"
        xlsx_name = f"{safe_vendor}-VALIDATION-{ts_str}.xlsx"

        tmp_dir = os.path.join(
            "tmp",
            "rejected",
            f"vendor={vendor}",
            f"submission={submission_id}"
        )
        os.makedirs(tmp_dir, exist_ok=True)

        rejected_json_path = os.path.join(tmp_dir, json_name)
        rejected_excel_path = os.path.join(tmp_dir, xlsx_name)

        # ------------------------------
        # Save Excel locally
        # ------------------------------
        df_err.to_excel(rejected_excel_path, index=False)
        print(f"  📊 Validation Excel saved → {rejected_excel_path}")

        # ------------------------------
        # Prepare JSON payload
        # ------------------------------
        log_record: Dict[str, Any] = {
            "vendor": vendor,
            "file": (
                f"raw/vendor={vendor}/"
                f"workflow={workflow}/"
                f"submission_type={submission_type}/"
                f"submission={submission_id}/"
                f"mapped/Item_Master.parquet"
            ),
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "stage": "VALIDATION",
            "error_type": "ValidationError",
            "error_message": f"{len(errors)} validation errors found.",
            "details": errors,
        }

        with open(rejected_json_path, "w", encoding="utf-8") as f:
            json.dump(log_record, f, indent=2)

        print(f"  ⚠️ Validation JSON saved → {rejected_json_path}")

        # ------------------------------
        # Upload Excel + JSON to Azure Blob
        # ------------------------------
        silver_client = blob_service.get_container_client("silver")

        # Upload JSON (Logic App listens to this)
        with open(rejected_json_path, "rb") as f:
            silver_client.upload_blob(
                name=rejected_prefix + json_name,
                data=f,
                overwrite=True
            )

        # Upload Excel (Analyst readable)
        with open(rejected_excel_path, "rb") as f:
            silver_client.upload_blob(
                name=rejected_prefix + xlsx_name,
                data=f,
                overwrite=True
            )


        print(f"  ☁️ Uploaded Excel → silver/rejected/logs/{xlsx_name}")
        print(f"  ☁️ Uploaded JSON → silver/rejected/logs/{json_name}")

        excel_blob_path = rejected_prefix + xlsx_name

        write_status(
            vendor,
            stage="VALIDATION",
            status="FAILED",
            message=f"{len(errors)} validation errors detected",
            submission_id=submission_id,
            extra={
                "error_count": len(errors),
                "excel_log": excel_blob_path
            }
        )


        raise RuntimeError("VALIDATION_FAILED")

    else:
        print("  🎉 No validation errors for this vendor!")
        
         # WRITE validation flags ONLY ON SUCCESS
        silver_client = blob_service.get_container_client(SILVER_CONTAINER)

        flags_blob = (
            f"in_review/{workflow}_workflow/"
            f"vendor={vendor}/"
            f"submission_type={submission_type}/"
            f"submission={submission_id}/"
            f"validation/item_master_with_validation.parquet"
        )

        buf = BytesIO()
        df_flags.to_parquet(buf, index=False)
        buf.seek(0)

        silver_client.upload_blob(
            name=flags_blob,
            data=buf,
            overwrite=True
        )

        print(f"☁️ Validation flags written → silver/{flags_blob}")

        promote_to_silver(vendor, workflow, submission_id, submission_type)

# ================================================================
# ENTRYPOINT
# ================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--submission-type", required=True)
    args = parser.parse_args()

    process_vendor(args.vendor,args.workflow, args.submission_type, args.submission_id)


if __name__ == "__main__":
    main()
