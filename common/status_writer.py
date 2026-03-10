# status_writer.py

import os
import json
from datetime import datetime
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError

AZURE_CONN = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

blob_service = BlobServiceClient.from_connection_string(AZURE_CONN)
container = blob_service.get_container_client("silver")


def write_status(vendor, stage, status, message="", submission_id=None, extra=None):

    # ----------------------------------------------------
    # Determine correct status file location
    # ----------------------------------------------------

    if submission_id:

        blob_path = (
            f"logs/vendor={vendor}/assets/"
            f"submission={submission_id}/"
            f"asset_etl_status.json"
        )

    else:

        blob_path = f"logs/vendor={vendor}/status.json"

    # ----------------------------------------------------
    # Load existing status (if exists)
    # ----------------------------------------------------

    try:

        raw = container.get_blob_client(blob_path).download_blob().readall()
        payload = json.loads(raw)

        if "events" not in payload:
            payload["events"] = []

    except ResourceNotFoundError:

        payload = {
            "vendor": vendor,
            "submission_id": submission_id,
            "events": []
        }

    # ----------------------------------------------------
    # Create event
    # ----------------------------------------------------

    event = {
        "stage": stage,
        "status": status,
        "message": message,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }

    # Add optional metadata safely
    if extra and isinstance(extra, dict):
        event.update(extra)

    payload["events"].append(event)

    # ----------------------------------------------------
    # Write updated status
    # ----------------------------------------------------

    container.upload_blob(
        name=blob_path,
        data=json.dumps(payload, indent=2),
        overwrite=True
    )