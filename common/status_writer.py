import os
import json
from datetime import datetime
from azure.storage.blob import BlobServiceClient

AZURE_CONN = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

def write_status(vendor, stage, status, message="", extra=None):
    blob_path = (
        f"logs/vendor={vendor}/"
        f"status.json"
    )

    payload = {
        "vendor": vendor,
        "stage": stage,
        "status": status,
        "message": message,
        "updated_at": datetime.utcnow().isoformat() + "Z"
    }

     # 🔥 Add extra metadata safely
    if extra and isinstance(extra, dict):
        payload.update(extra)


    blob_service = BlobServiceClient.from_connection_string(AZURE_CONN)
    container = blob_service.get_container_client("silver")

    container.upload_blob(
        name=blob_path,
        data=json.dumps(payload, indent=2),
        overwrite=True
    )
