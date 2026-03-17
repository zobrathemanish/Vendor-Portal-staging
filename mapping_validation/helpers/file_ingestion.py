# helpers/file_ingestion.py

import os
from azure.storage.blob import BlobServiceClient

CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = "bronze"

blob_service = BlobServiceClient.from_connection_string(CONNECTION_STRING)


def list_vendor_files(vendor: str, workflow: str, submission_id: str) -> list[str]:
    """
    Locate files uploaded for a specific vendor submission.

    Expected folder structure:

        raw/vendor=<vendor>/<workflow>/submission=<submission_id>/

    Example:

        raw/vendor=Grote Lighting/products/submission=123/product.xml
    """

    RAW_PREFIX = "raw/vendor="

    prefix = (
        f"{RAW_PREFIX}{vendor}/"
        f"{workflow}/"
        f"submission={submission_id}/"
    )

    container = blob_service.get_container_client(CONTAINER_NAME)

    blobs = list(container.list_blobs(name_starts_with=prefix))

    if not blobs:
        return []

    # newest first
    blobs.sort(key=lambda b: b.last_modified, reverse=True)

    return [b.name for b in blobs]


def download_blob_bytes(path: str) -> bytes:
    """
    Download blob content as bytes.
    """

    container = blob_service.get_container_client(CONTAINER_NAME)
    blob = container.get_blob_client(path)

    return blob.download_blob().readall()