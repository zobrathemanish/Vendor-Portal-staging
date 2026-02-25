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

def move_staging_assets_to_submission(
    vendor,
    final_submission_id,
    container_name="bronze"
):
    blob_service = BlobServiceClient.from_connection_string(
        os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    )
    container = blob_service.get_container_client(container_name)

    # 🔹 Staging location (no submission id anymore)
    staging_prefix = (
        f"raw/vendor={vendor}/staging/assets/"
    )

    # 🔹 Final submission location
    final_prefix = (
        f"raw/vendor={vendor}/submission={final_submission_id}/assets/"
    )

    blobs = list(container.list_blobs(name_starts_with=staging_prefix))

    if not blobs:
        logger.info("📦 No staging assets to move")
        return

    for blob in blobs:
        source_blob = container.get_blob_client(blob.name)

        # Replace staging path with submission path
        target_name = blob.name.replace(staging_prefix, final_prefix)
        target_blob = container.get_blob_client(target_name)

        # Copy → then delete original
        target_blob.start_copy_from_url(source_blob.url)
        source_blob.delete_blob()

        logger.info(f"📦 Asset moved → {target_name}")

    logger.info("✅ All staging assets moved successfully")