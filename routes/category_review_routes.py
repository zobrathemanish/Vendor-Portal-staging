import json
from io import BytesIO
from datetime import datetime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from flask import Blueprint, request, render_template, current_app, jsonify, abort
from azure.storage.blob import BlobServiceClient
from flask_login import login_required, current_user

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


# SECTION_COLUMNS = {

#     "Item_Master": [
#         "Part Number",
#         "Brand Label",
#         "Category",
#         "UNSPSC",
#         "HazmatFlag",
#         "Product Status",
#         "Quantity UOM",
#         "Barcode Type",
#         "Barcode Number",
#         "Quantity UOM",
#         "Quantity Size",
#         "Minimum Order Quantity UOM",
#         "VMRS Code"

#     ],

#     "Descriptions": [
#         "Part Number",
#         "Description Code",
#         "Description Value",
#         "Sequence"
#     ],

#     "Extended_Info": [
#         "Part Number",
#         "Extended Info Code",
#         "Extended Info Value"
#     ],
#      "Attributes": [
#         "Part Number",
#         "Attribute Name",
#         "Attribute Value"
#     ],

#     "Packages": [
#         "Part Number",
#         "Package UOM",
#         "Package QuantityofEaches",
#         "Weight",
#         "Weight UOM",
#         "Dimension UOM",
#         "Ship Length",
#         "Ship Width",
#         "Ship Height",
#         "Merch Length",
#         "Merch Width",
#         "Merch Height",
#         "Package Content"
#     ],

#     "Digital_Assets": [
#         "Part Number",
#         "Media Type",
#         "Filename",
#         "FilePath",
#         "FileType",
#         "Representation",
#         "Orientation",
#         "Height",
#         "Width",
#     ],

#     "Pricing": [
#         "Part Number",
#         "Effective Date",
#         "Price",
#         "Currency"
#     ],
# }


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

    except Exception:
        return pd.DataFrame()

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

APPROVED_CURRENT_ROOT = "approved/current_state"
APPROVED_HISTORY_ROOT = "approved/history"

def apply_delta_to_current_state(container, vendor: str):

    metadata = load_metadata(container, vendor)
    if not metadata:
        return False

    submission_id = metadata.get("submission_id")
    if not submission_id:
        return False

    delta = load_delta(container, vendor)
    decisions = load_decisions(container, vendor)

    if delta.empty:
        print("No delta found. Nothing to apply.")
        return False

    decisions_map = {
        str(r["part_number"]): r["decision"]
        for _, r in decisions.iterrows()
    }

    print("---- APPLY DELTA DEBUG ----")
    print("Vendor:", vendor)
    print("Metadata:", metadata)


    # -----------------------------------------------------
    # Load current baseline (if exists)
    # -----------------------------------------------------
    current_path = f"{APPROVED_CURRENT_ROOT}/vendor={vendor}/etl_mapped.parquet"

    try:
        current_bytes = _download_bytes(container, current_path)
        df_current = _df_from_parquet_bytes(current_bytes)
    except Exception:
        df_current = pd.DataFrame()

    if "Part Number" in df_current.columns:
        df_current["Part Number"] = df_current["Part Number"].astype(str)
    else:
        df_current["Part Number"] = ""

    if "Product Status" not in df_current.columns:
      df_current["Product Status"] = ""

    # -----------------------------------------------------
    # Load submission ready file (full mapped dataset)
    # -----------------------------------------------------
    ready_path = (
        f"category_queue/vendor={vendor}/"
        f"active/etl_mapped.parquet"
    )
    ready_bytes = _download_bytes(container, ready_path)
    df_ready = _df_from_parquet_bytes(ready_bytes)
    df_ready["Part Number"] = df_ready["Part Number"].astype(str)

    # -----------------------------------------------------
    # Process each changed part
    # -----------------------------------------------------
    changed_parts = sorted([
        p for p in delta["Part Number"].dropna().astype(str).unique()
        if p.strip()
    ])

    approved_parts = []
    rejected_parts = []

    for part in changed_parts:

        decision = decisions_map.get(part)

        if decision not in {"approve", "reject"}:
            continue

        print(f"Processing part {part} → {decision}")

        part_delta_rows = delta[
            delta["Part Number"].astype(str) == part
        ]

        delta_types = set(
            part_delta_rows[DELTA_TYPE_COL].dropna().tolist()
        )

        # =====================================================
        # DELETE (always mark inactive)
        # =====================================================
        if "delete" in delta_types:
            print(f"DELETE detected → marking {part} inactive")
            df_current.loc[
                df_current["Part Number"] == part,
                "Product Status"
            ] = "Inactive"
            approved_parts.append(part)
            continue

        # =====================================================
        # REJECT LOGIC
        # =====================================================
        if decision == "reject":

            if "update" in delta_types:
                print(f"UPDATE rejected → marking {part} inactive")
                df_current.loc[
                    df_current["Part Number"] == part,
                    "Product Status"
                ] = "Inactive"
            else:
                print(f"INSERT rejected → ignoring {part}")

            rejected_parts.append(part)
            continue

        # =====================================================
        # APPROVE LOGIC
        # =====================================================
        if "insert" in delta_types:
            df_insert = df_ready[df_ready["Part Number"] == part]
            df_current = pd.concat(
                [df_current, df_insert],
                ignore_index=True
            )

        elif "update" in delta_types:
            df_current = df_current[
                df_current["Part Number"] != part
            ]
            df_update = df_ready[
                df_ready["Part Number"] == part
            ]
            df_current = pd.concat(
                [df_current, df_update],
                ignore_index=True
            )

        approved_parts.append(part)

    # -----------------------------------------------------
    # Save updated baseline (Parquet + XLSX)
    # -----------------------------------------------------

    # Ensure clean index
    df_current = df_current.reset_index(drop=True)

    # ---------- PARQUET ----------
    parquet_buf = BytesIO()
    pq.write_table(pa.Table.from_pandas(df_current), parquet_buf)
    parquet_bytes = parquet_buf.getvalue()

    current_parquet_path = (
        f"{APPROVED_CURRENT_ROOT}/vendor={vendor}/etl_mapped.parquet"
    )

    _upload_bytes(container, current_parquet_path, parquet_bytes)

    # ---------- XLSX (Dynamic Multi-Tab Clean Version) ----------
    xlsx_buf = BytesIO()

    with pd.ExcelWriter(xlsx_buf, engine="xlsxwriter") as writer:

        if "__Section" in df_current.columns:

            sections = sorted(
                df_current["__Section"].dropna().unique()
            )

            INTERNAL_COLUMNS = {"__Section", "_delta_type"}

            for section in sections:

                df_section = df_current[
                    df_current["__Section"] == section
                ].copy()

                # Drop internal/governance columns
                df_section = df_section.drop(
                    columns=[
                        c for c in df_section.columns
                        if c in INTERNAL_COLUMNS
                    ],
                    errors="ignore"
                )

                # 🔥 Keep only columns that have at least one non-null value
                non_empty_cols = [
                    c for c in df_section.columns
                    if df_section[c].notna().any()
                ]

                df_section = df_section[non_empty_cols]

                # Keep Part Number first if present
                if "Part Number" in df_section.columns:
                    cols = ["Part Number"] + [
                        c for c in df_section.columns
                        if c != "Part Number"
                    ]
                    df_section = df_section[cols]

                df_section.to_excel(
                    writer,
                    sheet_name=str(section)[:31],  # Excel limit
                    index=False
                )

        else:
            df_current.to_excel(
                writer,
                sheet_name="Data",
                index=False
            )

    xlsx_bytes = xlsx_buf.getvalue()

    current_xlsx_path = (
        f"{APPROVED_CURRENT_ROOT}/vendor={vendor}/etl_mapped.xlsx"
    )

    _upload_bytes(container, current_xlsx_path, xlsx_bytes)

    print("📦 Current state Parquet + Clean Multi-Tab XLSX written")


    # -----------------------------------------------------
    # Archive history snapshot (Parquet + XLSX)
    # -----------------------------------------------------

    history_base = (
        f"{APPROVED_HISTORY_ROOT}/vendor={vendor}/"
        f"submission={submission_id}/"
    )

    _upload_bytes(container, history_base + "etl_mapped.parquet", parquet_bytes)
    _upload_bytes(container, history_base + "etl_mapped.xlsx", xlsx_bytes)

    print("🗂 History snapshot written")


    print(f"✅ Baseline updated for vendor {vendor}")
    write_promotion_log(
        container=container,
        vendor=vendor,
        submission_id=submission_id,
        approved_parts=approved_parts,
        rejected_parts=rejected_parts,
        baseline_count=len(df_current),
        user=current_user.username
    )

    # Clear category queue
    prefix = f"{CATEGORY_QUEUE_ROOT}/vendor={vendor}/"
    for blob in container.list_blobs(name_starts_with=prefix):
        container.delete_blob(blob.name)

    print("🧹 Category queue cleared")


    return True

# =========================================================
# PAGE ROUTE
# =========================================================

@category_review_bp.route("/review/category")
@login_required
def category_review_page():
    if current_user.role != "category":
        abort(403)

    return render_template("category_review.html")

# =========================================================
# API: LIST VENDORS WITH ACTIVE QUEUE
# =========================================================

@category_review_bp.route("/api/category-review/vendors")
@login_required
def api_category_review_vendors():

    if current_user.role != "category":
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

    if current_user.role != "category":
        abort(403)

    vendor = request.args.get("vendor")
    if not vendor:
        return jsonify({"error": "Missing vendor"}), 400

    container = _container()

    metadata = load_metadata(container, vendor)
    delta = load_delta(container, vendor)

    if delta.empty:
        return jsonify({
            "summary": {
                "vendor": vendor,
                "parts": 0,
                "status": "no_active_review"
            },
            "parts": []
        })

    decisions = load_decisions(container, vendor)
    decisions_map = {
        str(r["part_number"]): r["decision"]
        for _, r in decisions.iterrows()
    }

    parts = sorted([
        p for p in delta["Part Number"].dropna().astype(str).unique()
        if p.strip()
    ])

    summary = {
        "vendor": vendor,
        "submission_id": metadata.get("submission_id") if metadata else None,
        "parts": len(parts),
        "row_inserts": int((delta[DELTA_TYPE_COL] == "insert").sum()),
        "row_updates": int((delta[DELTA_TYPE_COL] == "update").sum()),
        "row_deletes": int((delta[DELTA_TYPE_COL] == "delete").sum()),
        "status": "pending"
    }

    parts_payload = []

    for pn in parts:
        pn_rows = delta[delta["Part Number"].astype(str) == pn]
        sections = {}

        for tab in pn_rows["__Section"].dropna().unique():
            tab_rows = pn_rows[pn_rows["__Section"] == tab]

            rows_payload = []

            for _, r in tab_rows.iterrows():
                rows_payload.append({
                    "delta_type": str(r.get(DELTA_TYPE_COL, "")).lower(),
                    "changes": [
                        {
                            "field": col,
                            "before": "",
                            "after": str(r.get(col, ""))
                        }
                        for col in r.index
                        if col not in ["Part Number", "__Section", DELTA_TYPE_COL]
                    ]
                })

            sections[tab] = {
                "rows": rows_payload,
                "baseline_rows": 0
            }

        parts_payload.append({
            "part_number": pn,
            "decision": decisions_map.get(pn, "pending"),
            "sections": sections
        })

    return jsonify({
        "summary": summary,
        "parts": parts_payload
    })

# =========================================================
# API: SAVE DECISION
# =========================================================

@category_review_bp.route("/api/category-review/decision", methods=["POST"])
@login_required
def api_category_review_decision():

    if current_user.role != "category":
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

    # -----------------------------------------------------
    # Promotion trigger condition
    # -----------------------------------------------------
    if all(decisions_map.get(p) in {"approve", "reject"} for p in review_required_parts):
        print("✅ All review-required parts decided. Triggering promotion.")
        apply_delta_to_current_state(container, vendor)
    else:
        print("⏳ Waiting on remaining decisions.")


    return jsonify({"ok": True})



@category_review_bp.route("/api/category-review/work-queue")
@login_required
def api_category_review_work_queue():

    if current_user.role != "category":
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

            items.append({
                "vendor": vendor,
                "submission_id": submission_id,
                "part_number": pn,
                "decision": decisions_map.get(pn, "pending"),
                "row_inserts": int((pn_rows[DELTA_TYPE_COL] == "insert").sum()),
                "row_updates": int((pn_rows[DELTA_TYPE_COL] == "update").sum()),
                "row_deletes": int((pn_rows[DELTA_TYPE_COL] == "delete").sum()),
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

    if current_user.role != "category":
        abort(403)

    vendor = request.args.get("vendor")
    part = request.args.get("part")

    if not vendor or not part:
        return jsonify({"error": "Missing params"}), 400

    container = _container()
    df = load_delta(container, vendor)

    if df.empty:
        return jsonify({"error": "No delta found"}), 404

    df_part = df[
        (df["Part Number"].astype(str) == str(part)) &
        (df["_delta_type"] == "insert")
    ].copy()

    print("DELTA ROWS FOR PART:", part)
    print(df_part[["Part Number", "__Section"]])


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
    # ASSET QUALITY (Metadata for scoring only)
    # =====================================================

    asset_meta = load_asset_quality(container, vendor)

    asset_part = pd.DataFrame()

    if not asset_meta.empty and "part_number" in asset_meta.columns:
        asset_meta["part_number"] = (
            asset_meta["part_number"]
            .astype(str)
            .str.strip()
        )

        asset_part = asset_meta[
            asset_meta["part_number"] == str(part).strip()
        ]

    # -----------------------------------------------------
    # Default values
    # -----------------------------------------------------
    image_preview_url = None
    jpg_count = 0
    valid_resolution = False
    avg_size_ok = False

    # =====================================================
    # IMAGE PREVIEW (Independent of Metadata)
    # =====================================================

    primary_filename = f"{part}_P04_01.jpg"

    blob_path = (
        f"ready/vendor={vendor}/"
        f"assets/part_number={part}/"
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

    return jsonify({
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
        "missing_attributes" : missing_attributes,
        "image_preview_url": image_preview_url
    })

@category_review_bp.route("/api/category-review/asset-preview")
@login_required
def api_asset_preview():

    if current_user.role != "category":
        abort(403)

    vendor = request.args.get("vendor")
    part = request.args.get("part")
    filename = request.args.get("file")

    if not vendor or not part or not filename:
        abort(400)

    container = _container()

    blob_path = (
        f"ready/vendor={vendor}/"
        f"assets/part_number={part}/"
        f"images/{filename}"
    )

    print("ASSET PREVIEW PATH:", blob_path)

    try:
        data = _download_bytes(container, blob_path)
    except Exception:
        abort(404)

    return current_app.response_class(
        data,
        mimetype="image/jpeg"
    )

