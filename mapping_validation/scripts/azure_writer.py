# scripts/azure_writer.py
import json
from io import BytesIO
from azure.storage.blob import BlobServiceClient
import pandas as pd
import os

def get_silver_container():
    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    service = BlobServiceClient.from_connection_string(conn)
    return service.get_container_client("silver")


def upload_parquet(df: pd.DataFrame, blob_path: str):
    buf = BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)

    container = get_silver_container()
    container.upload_blob(blob_path, buf, overwrite=True)


def upload_excel(excel_bytes: bytes, blob_path: str):
    container = get_silver_container()
    container.upload_blob(blob_path, excel_bytes, overwrite=True)


def upload_json(data: dict, blob_path: str):
    container = get_silver_container()
    container.upload_blob(
        blob_path,
        json.dumps(data, indent=2),
        overwrite=True
    )
