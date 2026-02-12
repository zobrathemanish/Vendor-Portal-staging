import os
from azure.storage.blob import BlobServiceClient

AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

blob_service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)

CONTAINERS = {
    "bronze": "raw/",
    "silver": ""
}

def delete_prefix(container_name, prefix):
    container = blob_service.get_container_client(container_name)

    blobs = container.list_blobs(name_starts_with=prefix)

    for blob in blobs:
        container.delete_blob(blob.name)
        print(f"🗑️ Deleted: {blob.name}")

if __name__ == "__main__":
    confirm = input("⚠️ This will DELETE Azure bronze/raw and silver data. Type DELETE to continue: ")

    if confirm == "DELETE":
        delete_prefix("bronze", "raw/")
        delete_prefix("silver", "")
        print("✅ Azure cleanup complete.")
    else:
        print("❌ Aborted.")
