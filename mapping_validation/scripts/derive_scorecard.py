import json
import os
from datetime import datetime
from typing import Dict, List
from azure.storage.blob import BlobServiceClient
import pandas as pd

# -------------------------------------------------
# CONFIG
# -------------------------------------------------
AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER = "bronze"

MANIFEST_PREFIX = "raw/vendor={vendor}/logs/"
PIPELINE_LOG_PREFIX = "raw/logs/vendor={vendor}/"

OUTPUT_PATH = "analytics/scorecards/scorecards.parquet"

# -------------------------------------------------
# HELPERS
# -------------------------------------------------
def load_json(blob_client) -> Dict:
    return json.loads(blob_client.download_blob().readall())

def list_blobs(container_client, prefix: str):
    return [b.name for b in container_client.list_blobs(name_starts_with=prefix)]

def parse_run_id(ts: str) -> str:
    """
    Normalize timestamps like:
    2025-12-17_14-19-01
    2025-12-17T20:21:24.523506
    """
    return ts.replace(":", "-").replace("T", "_").split(".")[0]

# -------------------------------------------------
# SCORING LOGIC
# -------------------------------------------------
def compute_score(ctx: Dict) -> Dict:
    score = 100

    # Fatal caps
    if ctx["XMLStatus"] == "FAILED":
        score = min(score, 30)
    elif ctx["PricingStatus"] == "FAILED":
        score = min(score, 40)

    # Validation penalties
    score -= ctx["ItemErrors"] * 3
    score -= ctx["PricingErrors"] * 4
    score -= ctx["InterchangeErrors"] * 1

    score = max(score, 0)

    # Grade
    if score >= 90:
        grade = "A"
    elif score >= 75:
        grade = "B"
    elif score >= 60:
        grade = "C"
    elif score >= 40:
        grade = "D"
    else:
        grade = "F"

    ctx["Score"] = score
    ctx["Grade"] = grade
    return ctx

# -------------------------------------------------
# MAIN AGGREGATOR
# -------------------------------------------------
def derive_vendor_scorecard(vendor: str) -> pd.DataFrame:
    blob_service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
    container = blob_service.get_container_client(CONTAINER)

    # ---------------------------
    # Load manifest logs
    # ---------------------------
    manifest_blobs = list_blobs(
        container,
        MANIFEST_PREFIX.format(vendor=vendor)
    )

    runs = {}

    for blob_name in manifest_blobs:
        blob = container.get_blob_client(blob_name)
        m = load_json(blob)

        run_id = parse_run_id(m["timestamp"])

        runs[run_id] = {
            "Vendor": vendor,
            "RunId": run_id,
            "RunTimestamp": m["timestamp"],
            "XMLStatus": "SUCCESS",
            "PricingStatus": "SUCCESS",
            "ValidationStatus": "SKIPPED",
            "FatalStage": None,
            "TotalValidationErrors": 0,
            "ItemErrors": 0,
            "PricingErrors": 0,
            "InterchangeErrors": 0,
            "PromotedToSilver": False
        }

    # ---------------------------
    # Load ingestion + validation logs
    # ---------------------------
    pipeline_blobs = list_blobs(
        container,
        PIPELINE_LOG_PREFIX.format(vendor=vendor)
    )

    for blob_name in pipeline_blobs:
        blob = container.get_blob_client(blob_name)
        log = load_json(blob)

        run_id = parse_run_id(log["timestamp"])
        if run_id not in runs:
            continue

        ctx = runs[run_id]
        stage = log.get("stage")

        # -------- Fatal ingestion errors --------
        if stage == "XML_PARSE":
            ctx["XMLStatus"] = "FAILED"
            ctx["FatalStage"] = "XML_PARSE"

        elif stage == "PRICING_LOAD_ERROR":
            ctx["PricingStatus"] = "FAILED"
            ctx["FatalStage"] = "PRICING_LOAD_ERROR"

        # -------- Validation --------
        elif stage == "VALIDATION":
            ctx["ValidationStatus"] = "FAILED"
            details = log.get("details", [])
            ctx["TotalValidationErrors"] = len(details)

            for d in details:
                sec = d.get("Section", "")
                if sec == "Item Master":
                    ctx["ItemErrors"] += 1
                elif sec == "Pricing":
                    ctx["PricingErrors"] += 1
                elif sec == "Part InterChange":
                    ctx["InterchangeErrors"] += 1

    # ---------------------------
    # Finalize scoring
    # ---------------------------
    rows = []
    for ctx in runs.values():
        rows.append(compute_score(ctx))

    return pd.DataFrame(rows)

# -------------------------------------------------
# ENTRY POINT
# -------------------------------------------------
if __name__ == "__main__":
    vendors = ["Grote Lighting"]  # or dynamically discover
    frames = []

    for v in vendors:
        print(f"📊 Deriving scorecard for {v}")
        frames.append(derive_vendor_scorecard(v))

    df = pd.concat(frames, ignore_index=True)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    df.to_parquet(OUTPUT_PATH, index=False)

    print(f"✅ Scorecards written → {OUTPUT_PATH}")
