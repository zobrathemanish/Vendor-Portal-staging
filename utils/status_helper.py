import os
import json
from azure.storage.blob import BlobServiceClient

_connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
_container = "silver"

_blob_service = BlobServiceClient.from_connection_string(_connection_string)
_container_client = _blob_service.get_container_client(_container)


def get_status_blob_path(vendor, workflow, submission_id, filename):
    return f"logs/vendor={vendor}/workflow={workflow}/submission={submission_id}/{filename}"


def read_status(vendor, workflow, submission_id, filename):
    try:
        path = get_status_blob_path(vendor, workflow, submission_id, filename)
        blob = _container_client.get_blob_client(path)
        data = blob.download_blob().readall()
        return json.loads(data)
    except:
        return None


def write_status(vendor, workflow, submission_id, filename, data):
    path = get_status_blob_path(vendor, workflow, submission_id, filename)
    blob = _container_client.get_blob_client(path)
    blob.upload_blob(json.dumps(data), overwrite=True)

def append_log(vendor, workflow, submission_id, message):

    path = f"logs/vendor={vendor}/workflow={workflow}/submission={submission_id}/pipeline.log"

    blob = _container_client.get_blob_client(path)

    try:
        existing = blob.download_blob().readall().decode("utf-8")
    except:
        existing = ""

    updated = existing + message + "\n"

    blob.upload_blob(updated, overwrite=True)    