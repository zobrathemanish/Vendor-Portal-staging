#services/submission_service.py

import os
import requests
from azure.storage.blob import BlobServiceClient
from extensions import logger

ETL_TRIGGER_URL = os.getenv("ETL_TRIGGER_URL")  # Logic App or API


from services.azure_service import upload_json_blob  # if needed
def trigger_etl(vendor, submission_id):
    if not ETL_TRIGGER_URL:
        logger.error("ETL_TRIGGER_URL not configured")
        return

    payload = {
        "vendor": vendor,
        "submission_id": submission_id
    }

    try:
        requests.post(ETL_TRIGGER_URL, json=payload, timeout=5)
        logger.info(f"ETL triggered for vendor={vendor}")
    except Exception as e:
        logger.error(f"ETL trigger failed: {e}")

def get_latest_submission_files(vendor, connection_string, container_name="bronze"):
    """
    Returns dict:
    {
        "product": "<blob_path or None>",
        "pricing": "<blob_path or None>",
        "assets": "<blob_path or None>"
    }
    """
    blob_service = BlobServiceClient.from_connection_string(connection_string)
    container = blob_service.get_container_client(container_name)

    prefix = f"raw/vendor={vendor}/submission="

    blobs = list(container.list_blobs(name_starts_with=prefix))
    if not blobs:
        return {}

    # Extract submission ids
    submission_ids = sorted(
        list({b.name.split("/")[2].replace("submission=", "") for b in blobs}),
        reverse=True
    )

    if not submission_ids:
        return {}

    latest_id = submission_ids[0]

    return {
        "submission_id": latest_id,
        "product": f"raw/vendor={vendor}/submission={latest_id}/product.xml",
        "pricing": f"raw/vendor={vendor}/submission={latest_id}/pricing.xlsx",
        "assets": f"raw/vendor={vendor}/submission={latest_id}/assets/"
    }