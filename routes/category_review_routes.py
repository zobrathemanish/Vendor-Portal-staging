#category_review_routes.py
import json
from io import BytesIO
from datetime import datetime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from flask import Blueprint, request, render_template, current_app, jsonify, abort
from azure.storage.blob import BlobServiceClient
from flask_login import login_required, current_user
import numpy as np

"""
INSERT
    Approve → Add
    Reject  → Ignore

UPDATE
    Approve → Replace
    Reject  → Mark inactive

DELETE
    → Mark inactive

"""

GOLD_CONTAINER = "gold"
GOLD_SELECTED_ROOT = "selected"

image_preview_url = None
jpg_count = 0

category_review_bp = Blueprint(
    "category_review",
    __name__,
    template_folder="../templates"
)

SILVER_CONTAINER = "silver"
CATEGORY_QUEUE_ROOT = "category_queue"
CATEGORY_ACTIVE_DIR = "active"
DELTA_TYPE_COL = "_delta_type"

# =========================================================
# Azure Helpers
# =========================================================

def _svc() -> BlobServiceClient:
    return BlobServiceClient.from_connection_string(
        current_app.config["AZURE_CONNECTION_STRING"]
    )

def _gold_container():
    return _svc().get_container_client(GOLD_CONTAINER)


def _container():
    return _svc().get_container_client(SILVER_CONTAINER)

def _download_bytes(container, path: str) -> bytes:
    return container.get_blob_client(path).download_blob().readall()

def _upload_bytes(container, path: str, data: bytes):
    container.upload_blob(path, data, overwrite=True)

def _df_from_parquet_bytes(b: bytes) -> pd.DataFrame:
    return pq.read_table(BytesIO(b)).to_pandas()

def _parquet_bytes_from_df(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    pq.write_table(pa.Table.from_pandas(df), buf)
    return buf.getvalue()

# =========================================================
# Queue Paths
# =========================================================

def queue_prefix(vendor: str) -> str:
    return f"{CATEGORY_QUEUE_ROOT}/vendor={vendor}/{CATEGORY_ACTIVE_DIR}/"

def metadata_path(vendor: str) -> str:
    return f"{queue_prefix(vendor)}metadata.json"

def delta_path(vendor: str) -> str:
    return f"{queue_prefix(vendor)}delta_mapped.parquet"

def decisions_path(vendor: str) -> str:
    return f"{queue_prefix(vendor)}decisions.parquet"

# =========================================================
# Load Helpers
# =========================================================

def load_metadata(container, vendor: str):
    try:
        b = _download_bytes(container, metadata_path(vendor))
        return json.loads(b.decode())
    except Exception:
        return None

def load_delta(container, vendor: str) -> pd.DataFrame:
    try:
        b = _download_bytes(container, delta_path(vendor))
        df = _df_from_parquet_bytes(b)

        if "Part Number" in df.columns:
            df["Part Number"] = df["Part Number"].astype("string")

        if "__Section" in df.columns:
            df["__Section"] = df["__Section"].astype("string")

        if DELTA_TYPE_COL in df.columns:
            df[DELTA_TYPE_COL] = df[DELTA_TYPE_COL].astype("string")

        return df

    except Exception as e:
        print("❌ DELTA LOAD FAILED:", e)
        raise

def load_decisions(container, vendor: str) -> pd.DataFrame:
    try:
        b = _download_bytes(container, decisions_path(vendor))
        return _df_from_parquet_bytes(b)
    except Exception:
        return pd.DataFrame(columns=["part_number", "decision", "user", "updated_at"])

def upsert_decision(container, vendor: str, part_number: str, decision: str, user: str):
    df = load_decisions(container, vendor)
    now = datetime.utcnow().isoformat() + "Z"

    mask = df["part_number"].astype(str) == str(part_number)

    if mask.any():
        df.loc[mask, ["decision", "user", "updated_at"]] = [decision, user, now]
    else:
        df = pd.concat([
            df,
            pd.DataFrame([{
                "part_number": str(part_number),
                "decision": decision,
                "user": user,
                "updated_at": now
            }])
        ], ignore_index=True)

    _upload_bytes(container, decisions_path(vendor), _parquet_bytes_from_df(df))

def load_asset_quality(container, vendor: str) -> pd.DataFrame:
    try:
        path = f"ready/vendor={vendor}/assets/_metadata/asset_quality.parquet"
        print("LOADING ASSET QUALITY FROM →", path)

        raw = _download_bytes(container, path)
        df = _df_from_parquet_bytes(raw)

        print("ASSET QUALITY ROW COUNT →", len(df))
        print("ASSET QUALITY COLUMNS →", list(df.columns))

        return df

    except Exception as e:
        print("ASSET QUALITY LOAD FAILED ❌", e)
        return pd.DataFrame()

def json_safe(obj):
    """
    Recursively convert pandas / numpy objects to JSON-safe types.
    """

    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [json_safe(v) for v in obj]

    # Pandas NA
    if obj is pd.NA:
        return None

    # numpy nan
    if isinstance(obj, float) and np.isnan(obj):
        return None

    # numpy types
    if isinstance(obj, (np.integer,)):
        return int(obj)

    if isinstance(obj, (np.floating,)):
        return float(obj)

    # pandas timestamp
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()

    return obj
# =========================================================
# PROMOTION LOGGING
# =========================================================

PROMOTION_LOG_ROOT = "approved/logs"

def write_promotion_log(
    container,
    vendor: str,
    submission_id: str,
    approved_parts: list,
    rejected_parts: list,
    baseline_count: int,
    user: str
):
    payload = {
        "vendor": vendor,
        "submission_id": submission_id,
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        "approved_parts": approved_parts,
        "rejected_parts": rejected_parts,
        "total_parts_reviewed": len(approved_parts) + len(rejected_parts),
        "resulting_baseline_row_count": baseline_count,
        "triggered_by": user
    }

    path = (
        f"{PROMOTION_LOG_ROOT}/vendor={vendor}/"
        f"submission={submission_id}/promotion_log.json"
    )

    _upload_bytes(
        container,
        path,
        json.dumps(payload, indent=2).encode("utf-8")
    )

    print("📝 Promotion log written")


# =========================================================
# DELTA MERGE ENGINE (Minimal + Logging)
# =========================================================

APPROVED_CURRENT_ROOT = "approved/unified_workflow"
APPROVED_HISTORY_ROOT = "approved/history"
GOLD_SELECTED_ROOT = "selected/unified_workflow"


def publish_to_gold(container, vendor: str):

    gold = _gold_container()

    # ------------------------------------------------------
    # 🔥 LOAD FINAL UNIFIED STATE (ONLY SOURCE)
    # ------------------------------------------------------
    unified_parquet_path = f"approved/unified_workflow/vendor={vendor}/unified_etl_mapped.parquet"
    unified_xlsx_path    = f"approved/unified_workflow/vendor={vendor}/unified_etl_mapped.xlsx"
    unified_delta_path   = f"approved/unified_workflow/vendor={vendor}/unified_delta.parquet"

    try:
        unified_parquet_bytes = _download_bytes(container, unified_parquet_path)
        unified_xlsx_bytes    = _download_bytes(container, unified_xlsx_path)
        unified_delta_bytes   = _download_bytes(container, unified_delta_path)
    except Exception as e:
        print("❌ Failed to load unified approved state:", e)
        return False

    # ------------------------------------------------------
    # 🔥 WRITE UNIFIED TO GOLD
    # ------------------------------------------------------
    gold_parquet_path = f"{GOLD_SELECTED_ROOT}/unified_workflow/vendor={vendor}/unified_etl_mapped.parquet"
    gold_xlsx_path    = f"{GOLD_SELECTED_ROOT}/unified_workflow/vendor={vendor}/unified_etl_mapped.xlsx"
    gold_delta_path   = f"{GOLD_SELECTED_ROOT}/unified_workflow/vendor={vendor}/unified_delta.parquet"

    _upload_bytes(gold, gold_parquet_path, unified_parquet_bytes)
    _upload_bytes(gold, gold_xlsx_path, unified_xlsx_bytes)
    _upload_bytes(gold, gold_delta_path, unified_delta_bytes)

    print("✅ Unified data pushed to GOLD")

    # ------------------------------------------------------
    # 🔥 ASSET FILTERING
    # ------------------------------------------------------
    df_unified = _df_from_parquet_bytes(unified_parquet_bytes)

    df_item = df_unified[df_unified["__Section"] == "Item_Master"].copy()

    if "delta_status" not in df_item.columns:
        df_item["delta_status"] = "active"

    df_item["delta_status"] = (
        df_item["delta_status"]
        .fillna("active")
        .astype(str)
        .str.strip()
        .str.lower()
    )

    active_parts = (
        df_item[df_item["delta_status"] == "active"]["Part Number"]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )

    print("✅ Active parts (delta_status based):", active_parts)

    # ------------------------------------------------------
    # 🔥 RESET GOLD ASSETS
    # ------------------------------------------------------
    silver_assets_prefix = f"approved/assets_workflow/vendor={vendor}/"
    gold_assets_prefix   = f"{GOLD_SELECTED_ROOT}/assets_workflow/vendor={vendor}/"

    existing_gold = list(gold.list_blobs(name_starts_with=gold_assets_prefix))

    print(f"🧹 Deleting {len(existing_gold)} existing GOLD assets")

    for blob in existing_gold:
        try:
            gold.delete_blob(blob.name)
        except Exception as e:
            print("⚠️ Failed deleting:", blob.name, e)

    # ------------------------------------------------------
    # 🔥 COPY ONLY ACTIVE PARTS
    # ------------------------------------------------------
    total_copied = 0

    for part in active_parts:

        part_prefix = f"{silver_assets_prefix}part_number={part}/"
        blobs = list(container.list_blobs(name_starts_with=part_prefix))

        print(f"📂 {part} → {len(blobs)} assets")

        for blob in blobs:
            relative_path = blob.name.replace(silver_assets_prefix, "")
            gold_path = f"{gold_assets_prefix}{relative_path}"

            try:
                data = _download_bytes(container, blob.name)
                _upload_bytes(gold, gold_path, data)
                total_copied += 1
            except Exception as e:
                print("⚠️ Failed copying:", blob.name, e)

    print(f"✅ Copied {total_copied} filtered assets to GOLD")
    print("✅ GOLD publish complete")

    return True

def apply_delta_to_current_state(container, vendor: str):

    print("🚨 APPLY DELTA START:", vendor)

    metadata = load_metadata(container, vendor)
    submission_id = metadata.get("submission_id") if metadata else None

    delta = load_delta(container, vendor)
    decisions = load_decisions(container, vendor)

    if delta.empty:
        print("No delta found")
        return False

    decisions_map = {
        str(r["part_number"]): r["decision"]
        for _, r in decisions.iterrows()
    }

    # -----------------------------------------------------
    # LOAD BASELINE (APPROVED = CURRENT STATE)
    # -----------------------------------------------------
    current_path = f"{APPROVED_CURRENT_ROOT}/vendor={vendor}/unified_etl_mapped.parquet"

    try:
        df_current = _df_from_parquet_bytes(_download_bytes(container, current_path))
    except:
        df_current = pd.DataFrame()

    if "Part Number" not in df_current.columns:
        df_current["Part Number"] = ""

    df_current["Part Number"] = df_current["Part Number"].astype(str)

    if "delta_status" not in df_current.columns:
        df_current["delta_status"] = "active"

    # -----------------------------------------------------
    # LOAD READY (INCOMING DATA)
    # -----------------------------------------------------
    ready_path = f"category_queue/vendor={vendor}/active/unified_etl_mapped.parquet"
    df_ready = _df_from_parquet_bytes(_download_bytes(container, ready_path))
    df_ready["Part Number"] = df_ready["Part Number"].astype(str)

    if "delta_status" not in df_ready.columns:
        df_ready["delta_status"] = "active"

    approved_parts = []
    rejected_parts = []

    changed_parts = sorted([
        p for p in decisions_map.keys()
        if str(p).strip()
    ])

    # -----------------------------------------------------
    # CORE LOGIC (MIRROR OLD SYSTEM)
    # -----------------------------------------------------
    for part in changed_parts:

        decision = decisions_map.get(part)

        if decision not in {"approve", "reject"}:
            continue

        part_delta = delta[
            delta["Part Number"].astype(str) == str(part)
        ]

        delta_types = set(part_delta[DELTA_TYPE_COL].dropna().tolist())

        if not delta_types:
            delta_types = {"insert"}

        # ============================
        # DELETE
        # ============================
        if "delete" in delta_types:
            print(f"DELETE → inactive {part}")

            df_current.loc[
                df_current["Part Number"] == str(part),
                "delta_status"
            ] = "inactive"

            approved_parts.append(part)
            continue

        # ============================
        # REJECT
        # ============================
        if decision == "reject":

            if "update" in delta_types:
                print(f"UPDATE reject → inactive {part}")

                df_current.loc[
                    df_current["Part Number"] == str(part),
                    "delta_status"
                ] = "inactive"

            else:
                print(f"INSERT reject → remove {part}")

                df_current = df_current[
                    df_current["Part Number"] != str(part)
                ]

            rejected_parts.append(part)
            continue

        # ============================
        # INSERT APPROVE
        # ============================
        if "insert" in delta_types:

            print(f"INSERT approve → add {part}")

            df_insert = df_ready[
                df_ready["Part Number"] == str(part)
            ].copy()

            df_insert["delta_status"] = "active"

            df_current = pd.concat([df_current, df_insert], ignore_index=True)

        # ============================
        # UPDATE APPROVE
        # ============================
        elif "update" in delta_types:

            print(f"UPDATE approve → replace {part}")

            df_current = df_current[
                df_current["Part Number"] != str(part)
            ]

            df_update = df_ready[
                df_ready["Part Number"] == str(part)
            ].copy()

            df_update["delta_status"] = "active"

            df_current = pd.concat([df_current, df_update], ignore_index=True)

        approved_parts.append(part)

    # -----------------------------------------------------
    # FINAL CLEANUP
    # -----------------------------------------------------
    df_current = df_current.reset_index(drop=True)

    # -----------------------------------------------------
    # SAVE PARQUET
    # -----------------------------------------------------
    parquet_bytes = _parquet_bytes_from_df(df_current)

    current_parquet_path = f"{APPROVED_CURRENT_ROOT}/vendor={vendor}/unified_etl_mapped.parquet"
    _upload_bytes(container, current_parquet_path, parquet_bytes)

    # -----------------------------------------------------
    # SAVE DELTA SNAPSHOT
    # -----------------------------------------------------
    latest_delta_bytes = _download_bytes(container, delta_path(vendor))
    delta_save_path = f"{APPROVED_CURRENT_ROOT}/vendor={vendor}/unified_delta.parquet"
    _upload_bytes(container, delta_save_path, latest_delta_bytes)

    # -----------------------------------------------------
    # HISTORY SNAPSHOT
    # -----------------------------------------------------
    if submission_id:
        history_base = f"{APPROVED_HISTORY_ROOT}/vendor={vendor}/submission={submission_id}/"
        _upload_bytes(container, history_base + "etl_mapped.parquet", parquet_bytes)

    # -----------------------------------------------------
    # LOGGING
    # -----------------------------------------------------
    write_promotion_log(
        container=container,
        vendor=vendor,
        submission_id=submission_id,
        approved_parts=approved_parts,
        rejected_parts=rejected_parts,
        baseline_count=len(df_current),
        user=current_user.username
    )

    # -----------------------------------------------------
    # CLEAR QUEUE
    # -----------------------------------------------------
    prefix = f"{CATEGORY_QUEUE_ROOT}/vendor={vendor}/"
    for blob in container.list_blobs(name_starts_with=prefix):
        container.delete_blob(blob.name)

    print("✅ APPLY DELTA COMPLETE")

    return True

def split_unified_to_workflows(unified_df):

    products = unified_df[unified_df["__Section"] != "Pricing"].copy()
    pricing  = unified_df[unified_df["__Section"] == "Pricing"].copy()

    return products, pricing
# =========================================================
# PAGE ROUTE
# =========================================================

@category_review_bp.route("/review/category")
@login_required
def category_review_page():
    if current_user.role != "category_team":
        abort(403)

    return render_template("category_review.html")

# =========================================================
# API: LIST VENDORS WITH ACTIVE QUEUE
# =========================================================

@category_review_bp.route("/api/category-review/vendors")
@login_required
def api_category_review_vendors():

    if current_user.role != "category_team":
        abort(403)

    container = _container()
    prefix = f"{CATEGORY_QUEUE_ROOT}/vendor="

    vendors = set()

    for blob in container.list_blobs(name_starts_with=prefix):
        parts = blob.name.split("/")
        if len(parts) >= 2 and parts[1].startswith("vendor="):
            vendors.add(parts[1].replace("vendor=", ""))

    return jsonify({"vendors": sorted(vendors)})

# =========================================================
# API: GET REVIEW DATA
# =========================================================

@category_review_bp.route("/api/category-review")
@login_required
def api_category_review():

    if current_user.role != "category_team":
        abort(403)

    vendor = request.args.get("vendor")
    if not vendor:
        return jsonify({"error": "Missing vendor"}), 400

    container = _container()

    delta = load_delta(container, vendor)
    metadata = load_metadata(container, vendor)

    if delta.empty:
        return jsonify({
            "summary": {"vendor": vendor, "parts": 0},
            "parts": []
        })

    decisions = load_decisions(container, vendor)
    decisions_map = {
        str(r["part_number"]): r["decision"]
        for _, r in decisions.iterrows()
    }

    parts = sorted(
        delta["Part Number"].dropna().astype(str).unique()
    )

    payload = []

    for pn in parts:
        pn_rows = delta[delta["Part Number"].astype(str) == pn]
        delta_types = set(pn_rows[DELTA_TYPE_COL].dropna().tolist())

        payload.append({
            "part_number": pn,
            "decision": decisions_map.get(pn, "pending"),
            "delta_types": list(delta_types)
        })

    return jsonify({
        "summary": {
            "vendor": vendor,
            "submission_id": metadata.get("submission_id") if metadata else None,
            "parts": len(parts)
        },
        "parts": payload
    })

# =========================================================
# API: SAVE DECISION
# =========================================================

@category_review_bp.route("/api/category-review/decision", methods=["POST"])
@login_required
def api_category_review_decision():

    if current_user.role != "category_team":
        abort(403)

    body = request.get_json(force=True) or {}

    vendor = body.get("vendor")
    part_number = body.get("part_number")
    decision = body.get("decision")

    if not vendor or not part_number or decision not in {"approve", "reject", "pending"}:
        return jsonify({"error": "Invalid payload"}), 400

    container = _container()

    upsert_decision(
        container,
        vendor,
        part_number,
        decision,
        current_user.username
    )

    # Only apply merge when all required parts are reviewed
    delta = load_delta(container, vendor)
    decisions = load_decisions(container, vendor)

    parts = sorted([
        p for p in delta["Part Number"].dropna().astype(str).unique()
        if p.strip()
    ])

    decisions_map = {
        str(r["part_number"]): r["decision"]
        for _, r in decisions.iterrows()
    }

    # -----------------------------------------------------
    # Identify delete-only parts
    # -----------------------------------------------------
    delete_only_parts = []

    for p in parts:
        part_rows = delta[
            delta["Part Number"].astype(str) == p
        ]

        delta_types = set(
            part_rows[DELTA_TYPE_COL].dropna().tolist()
        )

        if delta_types == {"delete"}:
            delete_only_parts.append(p)

    # -----------------------------------------------------
    # Parts that actually require decision
    # -----------------------------------------------------
    review_required_parts = [
        p for p in parts if p not in delete_only_parts
    ]

    print("Review-required parts:", review_required_parts)
    print("Decisions map:", decisions_map)

    print("📝 Decision saved. Waiting for manual publish.")


    return jsonify({"ok": True})

def get_row_key(row):
    section = row.get("__Section")

    if section == "Descriptions":
        return (
            row.get("Part Number"),
            row.get("Description Code"),
            row.get("Sequence"),
        )

    if section == "Extended_Info":
        return (
            row.get("Part Number"),
            row.get("Extended Info Code"),
        )

    if section == "Attributes":
        return (
            row.get("Part Number"),
            row.get("Attribute Name"),
        )

    if section == "Packages":
        return (
            row.get("Part Number"),
            row.get("Package UOM"),
            row.get("Package Quantity of Eaches"),
        )

    if section == "Digital_Assets":
        return (
            row.get("Part Number"),
            row.get("FileName"),
        )

    if section == "Pricing":
        return (
            row.get("Part Number"),
            row.get("Pricing Type"),
            row.get("Currency"),
        )

    return (row.get("Part Number"), section)

@category_review_bp.route("/api/category-review/work-queue")
@login_required
def api_category_review_work_queue():

    if current_user.role != "category_team":
        abort(403)

    container = _container()
    prefix = f"{CATEGORY_QUEUE_ROOT}/vendor="

    # ---------------------------------------------
    # STEP 1: Collect vendors safely
    # ---------------------------------------------
    vendors = set()

    for blob in container.list_blobs(name_starts_with=prefix):
        parts = blob.name.split("/")
        if len(parts) >= 2 and parts[1].startswith("vendor="):
            vendors.add(parts[1].replace("vendor=", ""))

    items = []

    # ---------------------------------------------
    # STEP 2: Process vendors one by one
    # ---------------------------------------------
    for vendor in vendors:

        delta = load_delta(container, vendor)
        if delta.empty:
            continue

        part_numbers = sorted([
            p for p in delta["Part Number"].dropna().astype(str).unique()
            if p.strip()
        ])

        # -----------------------------------------
        # DELETE-ONLY CHECK
        # -----------------------------------------
        delete_only_parts = []

        for p in part_numbers:
            part_rows = delta[
                delta["Part Number"].astype(str) == p
            ]
            delta_types = set(
                part_rows[DELTA_TYPE_COL].dropna().tolist()
            )

            if delta_types == {"delete"}:
                delete_only_parts.append(p)

        if part_numbers and len(delete_only_parts) == len(part_numbers):
            print("🟡 Delete-only queue detected. Auto-promoting.")
            apply_delta_to_current_state(container, vendor)
            continue  # Skip building items for this vendor

        # -----------------------------------------
        # Normal Queue Build
        # -----------------------------------------
        metadata = load_metadata(container, vendor)
        submission_id = metadata.get("submission_id") if metadata else None

        decisions = load_decisions(container, vendor)
        decisions_map = {
            str(r["part_number"]): r["decision"]
            for _, r in decisions.iterrows()
        }

        for pn in part_numbers:
            pn_rows = delta[
                delta["Part Number"].astype(str) == pn
            ]

            is_delete_only = (
                int((pn_rows[DELTA_TYPE_COL] == "delete").sum()) > 0 and
                int((pn_rows[DELTA_TYPE_COL] == "insert").sum()) == 0 and
                int((pn_rows[DELTA_TYPE_COL] == "update").sum()) == 0
            )

            decision = "auto_delete" if is_delete_only else decisions_map.get(pn, "pending")

            # -----------------------------------------
            # REAL CHANGE DETECTION (UI LOGIC)
            # -----------------------------------------
            df_insert = pn_rows[pn_rows[DELTA_TYPE_COL] == "insert"]
            df_delete = pn_rows[pn_rows[DELTA_TYPE_COL] == "delete"]

            insert_map = {get_row_key(r): r for _, r in df_insert.iterrows()}
            delete_map = {get_row_key(r): r for _, r in df_delete.iterrows()}

            common_keys = set(insert_map.keys()) & set(delete_map.keys())

            print("\n===== DEBUG ROW MATCHING =====")

            for key in common_keys:
                before = delete_map[key]
                after  = insert_map[key]

                print("\n--- MATCHED KEY ---")
                print("KEY:", key)

                print("BEFORE (sample):")
                print(before.to_dict())

                print("AFTER (sample):")
                print(after.to_dict())

                break  # just inspect 1 first

            real_update_count = 0

            for key in common_keys:
                before = delete_map[key]
                after  = insert_map[key]

                all_cols = set(before.index).union(set(after.index))

                for col in all_cols:
                    if col.startswith("_") or col in ["__Section", "_sheet"]:
                        continue

                    b = "" if pd.isna(before.get(col)) else str(before.get(col)).strip()
                    a = "" if pd.isna(after.get(col)) else str(after.get(col)).strip()

                    if b != a:
                        real_update_count += 1
                        break
            real_insert_count = len(insert_map.keys() - common_keys)
            real_delete_count = len(delete_map.keys() - common_keys)

            print("INSERT KEYS:", len(insert_map))
            print("DELETE KEYS:", len(delete_map))
            print("COMMON KEYS:", len(common_keys))

            items.append({
                "vendor": vendor,
                "submission_id": submission_id,
                "part_number": pn,
                "decision": decision,
                "row_inserts": real_insert_count,
                "row_updates": real_update_count,
                "row_deletes": real_delete_count,
            })

    # ---------------------------------------------
    # Summary
    # ---------------------------------------------
    summary = {
        "vendors": len(set([i["vendor"] for i in items])),
        "parts_total": len(items),
        "pending": len([i for i in items if i["decision"] == "pending"]),
        "approved": len([i for i in items if i["decision"] == "approve"]),
        "rejected": len([i for i in items if i["decision"] == "reject"]),
    }

    return jsonify({
        "summary": summary,
        "items": items
    })

@category_review_bp.route("/api/category-review/part-intelligence")
@login_required
def api_part_intelligence():

    if current_user.role != "category_team":
        abort(403)

    vendor = request.args.get("vendor")
    part = request.args.get("part")

    # Initialize safely for ALL execution paths
    image_preview_url = None
    jpg_count = 0

    if not vendor or not part:
        return jsonify({"error": "Missing params"}), 400

    container = _container()
    df = load_delta(container, vendor)

    if df.empty:
        return jsonify({"error": "No delta found"}), 404

    # Normalize section column
    if "__Section" not in df.columns and "_sheet" in df.columns:
        df["__Section"] = df["_sheet"]

    df_part = df[
        df["Part Number"].astype(str) == str(part)
    ].copy()

    # =====================================================
    # IMAGE PREVIEW (Independent of Metadata)
    # =====================================================

    primary_filename = f"{part}_P04_01.jpg"

    #debug here
    blob_path = (
        f"approved/assets_workflow/vendor={vendor}/"
        f"part_number={part}/"
        f"images/{primary_filename}"
    )

    try:
        container.get_blob_client(blob_path).get_blob_properties()

        image_preview_url = (
            f"/api/category-review/asset-preview"
            f"?vendor={vendor}&part={part}&file={primary_filename}"
        )

        jpg_count = 1

    except Exception:
        pass

    if df_part.empty:
        return jsonify({"error": "No delta data found"}), 404

    delta_types = set(df_part["_delta_type"].dropna().tolist())
    print("DELTA TYPES FOR PART:", part, delta_types)

    # =====================================================
    # DELETE MODE – Load Baseline Intelligence
    # =====================================================
    if delta_types == {"delete"}:

        gold = _gold_container()

        baseline_path = (
            f"{GOLD_SELECTED_ROOT}/vendor={vendor}/unified_etl_mapped.parquet"
        )

        try:
            baseline_bytes = _download_bytes(gold, baseline_path)
            df_baseline = _df_from_parquet_bytes(baseline_bytes)
        except:
            return jsonify({"error": "Gold Baseline not found"}), 404

        df_part = df_baseline[
            df_baseline["Part Number"].astype(str) == str(part)
        ]

        if df_part.empty:
            return jsonify({"error": "No baseline data found"}), 404

        # Now reuse same logic as insert to extract fields
        item_master = df_part[df_part["__Section"] == "Item_Master"]
        desc_df = df_part[df_part["__Section"] == "Descriptions"]
        ext_df = df_part[df_part["__Section"] == "Extended_Info"]
        pkg_df = df_part[df_part["__Section"] == "Packages"]
        pricing_df = df_part[df_part["__Section"] == "Pricing"]

        def first_val(df, col):
            return df[col].iloc[0] if col in df.columns and not df.empty else ""

        brand = first_val(item_master, "Brand Label")
        category = first_val(item_master, "Category")
        status = first_val(item_master, "Product Status")

        short_desc = ""
        for _, r in desc_df.iterrows():
            if r.get("Description Code") in ["DES", "SHO"]:
                short_desc = r.get("Description Value", "")
                break

        return jsonify(json_safe({
            "mode": "delete",
            "brand": brand,
            "category": category,
            "status": status,
            "short_description": short_desc,
            "image_preview_url": image_preview_url
        }))

    # =====================================================
    # UPDATE MODE – DERIVED FROM INSERT + DELETE (FINAL)
    # =====================================================
    df_insert = df_part[df_part["_delta_type"] == "insert"].copy()
    df_delete = df_part[df_part["_delta_type"] == "delete"].copy()

    insert_map = {get_row_key(r): r for _, r in df_insert.iterrows()}
    delete_map = {get_row_key(r): r for _, r in df_delete.iterrows()}

    common_keys = set(insert_map.keys()) & set(delete_map.keys())

    # Only enter update mode when there are matched insert/delete pairs
    if common_keys:

        IGNORE_COLUMNS = {
            "__Section",
            "_sheet",
            "_domain",
            "_workflow",
            "_delta_type",
            "_merge",
            "_row_hash_before",
            "_row_hash_after",
            "_entity_key",
            "_entity_id",
            "_row_id",
            "_vendor",
            "_autofix_run_id",
            "_autofix_timestamp",
            "_transformation_applied",
            "_hash",
            "delta_status"
        }

        def normalize(v):
            if pd.isna(v):
                return ""
            try:
                num = float(v)
                if num.is_integer():
                    return str(int(num))
                return str(num)
            except:
                return str(v).strip()

        changes = []

        for key in common_keys:
            before_row = delete_map[key]
            after_row = insert_map[key]
            section = after_row.get("__Section")

            all_cols = set(before_row.index).union(set(after_row.index))

            for col in all_cols:
                if col in IGNORE_COLUMNS:
                    continue

                before = normalize(before_row[col]) if col in before_row else ""
                after = normalize(after_row[col]) if col in after_row else ""

                if before != after and (before or after):
                    changes.append({
                        "section": section,
                        "field": col,
                        "before": before or "-",
                        "after": after or "-"
                    })

        return jsonify(json_safe({
            "mode": "update",
            "changes": changes,
            "image_preview_url": image_preview_url
        }))

    print("DELTA ROWS FOR PART:", part)
    cols = ["Part Number"]
    if "__Section" in df_part.columns:
        cols.append("__Section")

    print(df_part[cols])

    if df_part.empty:
        return jsonify({"error": "No insert data found"}), 404

    # =====================================================
    # SECTION SPLITS
    # =====================================================

    item_master = df_part[df_part["__Section"] == "Item_Master"]
    desc_df = df_part[df_part["__Section"] == "Descriptions"]
    ext_df = df_part[df_part["__Section"] == "Extended_Info"]
    pkg_df = df_part[df_part["__Section"] == "Packages"]
    asset_df = df_part[df_part["__Section"] == "Digital_Assets"]
    pricing_df = df_part[df_part["__Section"] == "Pricing"]


    # =====================================================
    # CORE FIELDS
    # =====================================================

    def first_val(df, col):
        return df[col].iloc[0] if col in df.columns and not df.empty else ""

    brand = first_val(item_master, "Brand Label")
    category = first_val(item_master, "Category")
    unspsc = first_val(item_master, "UNSPSC")
    hazmat = first_val(item_master, "HazmatFlag")
    status = first_val(item_master, "Product Status")
    quantity_uom = first_val(item_master, "Quantity UOM")

    # Short Description
    short_desc = ""
    for _, r in desc_df.iterrows():
        if r.get("Description Code") in ["DES", "SHO"]:
            short_desc = r.get("Description Value", "")
            break

    # Extended Info
    cto = ""
    hsb = ""

    for _, r in ext_df.iterrows():
        if r.get("Extended Info Code") == "CTO":
            cto = r.get("Extended Info Value", "")
        if r.get("Extended Info Code") == "HSB":
            hsb = r.get("Extended Info Value", "")

    # Effective Date (Pricing tab)
    effective_date = ""

    if not pricing_df.empty and "Effective Date" in pricing_df.columns:
        val = pricing_df["Effective Date"].iloc[0]
        if pd.notna(val):
            effective_date = str(val)


    # =====================================================
    # PACKAGE WEIGHT CHECK
    # =====================================================

    valid_weight = False

    if not pkg_df.empty:
        for _, r in pkg_df.iterrows():
            weight = r.get("Weight")
            weight_uom = r.get("Weight UOM")
            if pd.notna(weight) and float(weight) > 0 and pd.notna(weight_uom):
                valid_weight = True
                break


    # =====================================================
    # ASSET QUALITY (fallback to Digital_Assets)
    # =====================================================

    asset_meta = load_asset_quality(container, vendor)

    asset_part = pd.DataFrame()

    if not asset_meta.empty and "part_number" in asset_meta.columns:
        asset_meta["part_number"] = asset_meta["part_number"].astype(str).str.strip()

        asset_part = asset_meta[
            asset_meta["part_number"] == str(part).strip()
        ]

    # =====================================================
    # FALLBACK → Digital_Assets
    # =====================================================

    if asset_part.empty:
        print("⚠️ Using Digital_Assets fallback")

        jpg_count = len(asset_df)

        # minimal assumptions (until metadata exists)
        valid_resolution = jpg_count > 0
        avg_size_ok = True

    else:
        jpg_count = len(asset_part)

        valid_resolution = False
        avg_size_ok = False

        if "final_resolution" in asset_part.columns:
            valid_resolution = any(
                asset_part["final_resolution"] == "1000x1000"
            )

        if "file_size_bytes" in asset_part.columns:
            avg_size = asset_part["file_size_bytes"].mean()
            if avg_size and avg_size < 5_000_000:
                avg_size_ok = True

    # -----------------------------------------------------
    # Default values
    # -----------------------------------------------------
    valid_resolution = False
    avg_size_ok = False

    # =====================================================
    # METADATA-BASED SCORING (If Available)
    # =====================================================

    if not asset_part.empty:

        # Image count from metadata
        jpg_count = len(asset_part)

        # Resolution compliance
        if "final_resolution" in asset_part.columns:
            valid_resolution = any(
                asset_part["final_resolution"] == "1000x1000"
            )

        # File size sanity
        if "file_size_bytes" in asset_part.columns:
            avg_size = asset_part["file_size_bytes"].mean()
            if avg_size and avg_size < 5_000_000:
                avg_size_ok = True


    # =====================================================
    # ASSET QUALITY SCORE
    # =====================================================

    asset_score = 0

    # Image count score
    if jpg_count >= 3:
        asset_score += 40
    elif jpg_count >= 1:
        asset_score += 20

    # Resolution score
    if valid_resolution:
        asset_score += 40

    # File size score
    if avg_size_ok:
        asset_score += 20


    # =====================================================
    # MISSING ATTRIBUTES
    # =====================================================


    missing_attributes = []

    if not brand:
        missing_attributes.append("Brand")

    if not category:
        missing_attributes.append("Category")

    if not status:
        missing_attributes.append("Product Status")

    if not short_desc:
        missing_attributes.append("Short Description")

    if not cto:
        missing_attributes.append("Country of Origin")

    if not valid_weight:
        missing_attributes.append("Weight")

    if not quantity_uom:
        missing_attributes.append("Quantity UOM")

    if not effective_date:
        missing_attributes.append("Effective Date")



    # =====================================================
    # DATA QUALITY SCORE
    # =====================================================

    data_score = 0

    if brand: data_score += 15
    if category: data_score += 20
    if status: data_score += 10
    if short_desc: data_score += 15
    if cto: data_score += 10
    if valid_weight: data_score += 15
    if quantity_uom: data_score += 10
    if effective_date: data_score += 5

    print("IMAGE DEBUG →", {
    "vendor": vendor,
    "part": part,
    "jpg_count": jpg_count,
    "preview_url": image_preview_url
})

    # =====================================================
    # RESPONSE
    # =====================================================

    payload = {
        "brand": brand,
        "hazmat": hazmat,
        "category": category,
        "unspsc": unspsc,
        "status": status,
        "short_description": short_desc,
        "country_of_origin": cto,
        "hsb": hsb,
        "effective_date": effective_date,
        "weight_present": valid_weight,
        "image_completeness": f"{jpg_count} / 3",
        "data_quality_score": data_score,
        "asset_quality_score": asset_score,
        "missing_attributes": missing_attributes,
        "image_preview_url": image_preview_url
    }

    return jsonify(json_safe(payload))

@category_review_bp.route("/api/category-review/asset-preview")
@login_required
def api_asset_preview():

    if current_user.role != "category_team":
        abort(403)

    vendor = request.args.get("vendor")
    part = request.args.get("part")
    filename = request.args.get("file")

    if not vendor or not part or not filename:
        abort(400)

    container = _container()

    gold = _gold_container()

    blob_path = (
        f"{GOLD_SELECTED_ROOT}/assets_workflow/vendor={vendor}/"
        f"part_number={part}/"
        f"images/{filename}"
    )

    print("ASSET PREVIEW PATH:", blob_path)

    try:
        data = _download_bytes(gold, blob_path)
    except Exception:
        try:
            blob_path = (
                f"approved/assets_workflow/vendor={vendor}/"
                f"part_number={part}/images/{filename}"
            )
            data = _download_bytes(container, blob_path)
        except Exception:
            abort(404)

    return current_app.response_class(
        data,
        mimetype="image/jpeg"
    )

from azure.core.exceptions import ResourceNotFoundError

def clear_category_queue(container, vendor: str):
    base_path = f"category_queue/vendor={vendor}/active"

    delta_parquet = f"{base_path}/delta_mapped.parquet"
    delta_excel = f"{base_path}/delta_mapped.xlsx"
    decisions_path = f"{base_path}/decisions.parquet"   # 🔥 ADD THIS

    print(f"[QUEUE] Clearing category queue for vendor={vendor}")

    # -----------------------------------
    # Delete delta parquet
    # -----------------------------------
    try:
        container.delete_blob(delta_parquet)
        print("[QUEUE] Delta parquet deleted")
    except ResourceNotFoundError:
        print("[QUEUE] Delta parquet already empty")

    # -----------------------------------
    # Delete delta excel
    # -----------------------------------
    try:
        container.delete_blob(delta_excel)
        print("[QUEUE] Delta excel deleted")
    except ResourceNotFoundError:
        print("[QUEUE] Delta excel already empty")

    # -----------------------------------
    # 🔥 Delete decisions
    # -----------------------------------
    try:
        container.delete_blob(decisions_path)
        print("[QUEUE] Decisions cleared")
    except ResourceNotFoundError:
        print("[QUEUE] Decisions already empty")

@category_review_bp.route("/api/category-review/publish-gold", methods=["POST"])
@login_required
def api_publish_gold():

    if current_user.role != "category_team":
        abort(403)

    body = request.get_json(force=True) or {}
    vendor = body.get("vendor")

    if not vendor:
        return jsonify({"error": "Missing vendor"}), 400

    container = _container()

    # -----------------------------------------------------
    # Resolve workflow FROM QUEUE (authoritative)
    # -----------------------------------------------------
    delta = load_delta(container, vendor)

    if delta.empty or "_workflow" not in delta.columns:
        return jsonify({"error": "No workflow info found in queue"}), 400

    success = True

    print("🔥 APPLYING DELTA BEFORE MANUAL PUBLISH")
    apply_delta_to_current_state(container, vendor)

    # 🔥 ADD THIS LINE
    container = _container()   # force fresh client / avoid cached state

    success = publish_to_gold(container, vendor)

    # clear_category_queue(container, vendor)

    return jsonify({
        "success": success,
        "message": "Gold updated successfully" if success else "Failed"
    })