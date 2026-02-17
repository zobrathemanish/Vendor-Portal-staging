import json
from io import BytesIO
from datetime import datetime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from flask import Blueprint, request, render_template, current_app, jsonify, abort
from azure.storage.blob import BlobServiceClient
from flask_login import login_required, current_user

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

    return jsonify({"ok": True})


@category_review_bp.route("/api/category-review/work-queue")
@login_required
def api_category_review_work_queue():

    if current_user.role != "category":
        abort(403)

    container = _container()
    prefix = f"{CATEGORY_QUEUE_ROOT}/vendor="

    vendors = {}
    items = []

    for blob in container.list_blobs(name_starts_with=prefix):
        parts = blob.name.split("/")

        if len(parts) < 4:
            continue

        if not parts[1].startswith("vendor="):
            continue

        vendor = parts[1].replace("vendor=", "")

        if not blob.name.endswith("delta_mapped.parquet"):
            continue

        # Load delta
        delta = load_delta(container, vendor)
        if delta.empty:
            continue

        metadata = load_metadata(container, vendor)
        submission_id = metadata.get("submission_id") if metadata else None

        decisions = load_decisions(container, vendor)
        decisions_map = {
            str(r["part_number"]): r["decision"]
            for _, r in decisions.iterrows()
        }

        part_numbers = sorted([
            p for p in delta["Part Number"].dropna().astype(str).unique()
            if p.strip()
        ])

        for pn in part_numbers:

            pn_rows = delta[delta["Part Number"].astype(str) == pn]

            items.append({
                "vendor": vendor,
                "submission_id": submission_id,
                "part_number": pn,
                "decision": decisions_map.get(pn, "pending"),
                "row_inserts": int((pn_rows[DELTA_TYPE_COL] == "insert").sum()),
                "row_updates": int((pn_rows[DELTA_TYPE_COL] == "update").sum()),
                "row_deletes": int((pn_rows[DELTA_TYPE_COL] == "delete").sum()),
            })

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
