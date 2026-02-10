"""
Azure Blob Storage services for FGI Vendor Portal
"""
import os
import json
from datetime import datetime
from azure.storage.blob import BlobServiceClient
from services.file_service import compute_file_hash

from datetime import datetime, timedelta
from azure.storage.blob import generate_blob_sas, BlobSasPermissions,BlobServiceClient, ContentSettings
from azure.core.exceptions import ResourceNotFoundError


import os

ACCOUNT_NAME = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
ACCOUNT_KEY = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")

from azure.storage.blob import (
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions
)
from datetime import datetime, timedelta
import os

def generate_upload_sas(container: str, blob_path: str) -> str:
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set")

    blob_service = BlobServiceClient.from_connection_string(conn_str)

    account_name = blob_service.account_name
    account_key = blob_service.credential.account_key  # ✅ THIS IS THE FIX

    sas = generate_blob_sas(
        account_name=account_name,
        container_name=container,
        blob_name=blob_path,
        account_key=account_key,
        permission=BlobSasPermissions(write=True, create=True, add=True),
        expiry=datetime.utcnow() + timedelta(hours=1)
    )

    return f"https://{account_name}.blob.core.windows.net/{container}/{blob_path}?{sas}"

def utc_timestamp():
    return datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")

def safe_vendor_key(vendor: str) -> str:
    return vendor.strip().replace(" ", "_")


def get_blob_service_client(connection_string):
    """
    Create and return Azure Blob Service Client.
    
    Args:
        connection_string (str): Azure Storage connection string
        
    Returns:
        BlobServiceClient: Configured blob service client
        
    Raises:
        RuntimeError: If connection string is not configured
    """
    if not connection_string:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is not configured.")
    return BlobServiceClient.from_connection_string(connection_string)


def get_latest_asset_hash(container_client, vendor_folder):
    """
    Return last uploaded asset hash from Azure manifest.
    
    Args:
        container_client: Azure container client
        vendor_folder (str): Vendor folder name (e.g., 'vendor=Grote')
        
    Returns:
        str or None: SHA-256 hash of last uploaded assets, or None if no manifest exists
    """
    prefix = f"raw/{vendor_folder}/logs/"
    blobs = list(container_client.list_blobs(name_starts_with=prefix))

    if not blobs:
        return None  # No manifest present yet

    blobs_sorted = sorted(blobs, key=lambda b: b.name, reverse=True)
    latest_manifest_blob = blobs_sorted[0]

    manifest_data = container_client.download_blob(latest_manifest_blob.name).readall()
    manifest = json.loads(manifest_data)

    # return manifest.get("assets_hash")


def delete_old_asset_zips(container_client, vendor_folder):
    """
    Delete old asset ZIP files, keeping only the most recent one.
    
    Args:
        container_client: Azure container client
        vendor_folder (str): Vendor folder name (e.g., 'vendor=Grote')
    """
    prefix = f"raw/{vendor_folder}/assets/"
    blobs = list(container_client.list_blobs(name_starts_with=prefix))

    if len(blobs) <= 1:
        return  # Nothing to delete

    blobs_sorted = sorted(blobs, key=lambda b: b.name)

    for old_blob in blobs_sorted[:-1]:
        container_client.delete_blob(old_blob.name)
        print(f"[CLEANUP] Deleted old asset ZIP: {old_blob.name}")


def upload_blob(local_path, blob_path, connection_string, container_name):
    """
    Upload a file to Azure Blob Storage.
    
    Args:
        local_path (str): Local file path
        blob_path (str): Destination blob path in container
        connection_string (str): Azure Storage connection string
        container_name (str): Azure container name
        
    Returns:
        str: Full blob path in format 'container/blob_path'
    """
    blob_service_client = get_blob_service_client(connection_string)
    container_client = blob_service_client.get_container_client(container_name)
    blob_client = container_client.get_blob_client(blob_path)

    with open(local_path, "rb") as data:
        blob_client.upload_blob(data, overwrite=True)

    print(f"✅ Uploaded to Azure: {blob_path}")
    return f"{container_name}/{blob_path}"


def upload_json_blob(
    data: dict,
    blob_path: str,
    connection_string: str,
    container_name: str,
):
    """
    Upload a JSON marker file to Azure Blob Storage.

    Used for notify markers (human-in-the-loop triggers).
    """
    blob_service_client = BlobServiceClient.from_connection_string(
        connection_string
    )
    container_client = blob_service_client.get_container_client(
        container_name
    )

    blob_client = container_client.get_blob_client(blob_path)

    payload = json.dumps(data, indent=2)

    blob_client.upload_blob(
        payload,
        overwrite=True,
        content_settings=ContentSettings(
            content_type="application/json"
        )
    )



def upload_to_azure_bronze_opticat(vendor,submission_id, xml_local_path, pricing_local_path, 
                           connection_string, container_name, upload_folder):
    """
    Upload product XML, pricing XLSX, and assets ZIP to Azure Bronze.
    Includes: hash check, skip-if-same, smart deletion, manifest updates.
    
    Args:
        vendor (str): Vendor name
        xml_local_path (str): Local path to XML file
        pricing_local_path (str): Local path to pricing Excel file
        connection_string (str): Azure Storage connection string
        container_name (str): Azure container name
        upload_folder (str): Base upload folder for local manifest storage
        
    Returns:
        None
    """
    timestamp = utc_timestamp()
    safe_vendor = safe_vendor_key(vendor)
    vendor_folder = f"vendor={vendor}"

    blob_service_client = get_blob_service_client(connection_string)
    container_client = blob_service_client.get_container_client(container_name)

    # --------------- XML + Pricing always uploaded ---------------
    xml_blob_path = f"raw/{vendor_folder}/submission={submission_id}/product/{timestamp}_product.xml"
    pricing_blob_path = f"raw/{vendor_folder}/submission={submission_id}/pricing/{timestamp}_pricing.xlsx"

    xml_blob_full = upload_blob(xml_local_path, xml_blob_path, connection_string, container_name)
    pricing_blob_full = upload_blob(pricing_local_path, pricing_blob_path, connection_string, container_name)


    # --------------- Create manifest ---------------
    manifest = {
    "vendor": vendor,
    "submission_id": submission_id,
    "timestamp": timestamp,
    "azure_xml_blob": xml_blob_full,
    "azure_pricing_blob": pricing_blob_full,
    "assets_uploaded_via": "browser_sas",
}


    local_manifest_dir = os.path.join(upload_folder, vendor, "opticat")
    os.makedirs(local_manifest_dir, exist_ok=True)
    manifest_filename = f"manifest-{safe_vendor}-{timestamp}.json"
    local_manifest_path = os.path.join(local_manifest_dir, manifest_filename)


    with open(local_manifest_path, "w") as f:
        json.dump(manifest, f, indent=4)

    manifest_blob_path = f"raw/{vendor_folder}/submission={submission_id}/logs/{manifest_filename}"
    upload_blob(local_manifest_path, manifest_blob_path, connection_string, container_name)

    print(f"📄 Manifest created and uploaded: {manifest_blob_path}")

    
def upload_to_azure_bronze_non_opticat(
        vendor,
        submission_id,
        unified_local_path,
        connection_string,
        container_name,
        upload_folder):
    """
    Upload unified XLSX and optional assets ZIP to Azure Bronze for NON-OptiCat vendors.
    Mirrors the structure and behavior of upload_to_azure_bronze_opticat.

    Structure created in Azure:
        raw/vendor=<Vendor>/submission=<submission_id>/unified/<timestamp>_unified.xlsx
        raw/vendor=<Vendor>/submission=<submission_id>/assets/<timestamp>_assets.zip
        raw/vendor=<Vendor>/submission=<submission_id>/logs/manifest_<timestamp>.json

    Includes:
        - hash check for assets ZIP
        - skip upload if same hash
        - delete old ZIPs, keep only latest
        - store manifest locally and in Azure
    """

    vendor_folder = f"vendor={vendor}/submission={submission_id}"

    blob_service_client = get_blob_service_client(connection_string)
    container_client = blob_service_client.get_container_client(container_name)
    timestamp = utc_timestamp()
    safe_vendor = safe_vendor_key(vendor)

    # -------------------- Upload unified XLSX --------------------
    unified_blob_path = f"raw/{vendor_folder}/submission={submission_id}/unified/{timestamp}_unified.xlsx"
    azure_unified_blob = upload_blob(
        unified_local_path,
        unified_blob_path,
        connection_string,
        container_name,
    )

 

    # -------------------- Create manifest --------------------
    manifest = {
    "vendor": vendor,
    "submission_id": submission_id,
    "timestamp": timestamp,
    "azure_unified_blob": azure_unified_blob,
    "assets_uploaded_via": "browser_sas",
}


    local_manifest_dir = os.path.join(upload_folder, vendor, "non_opticat")
    os.makedirs(local_manifest_dir, exist_ok=True)

    manifest_filename = f"manifest-{safe_vendor}-{timestamp}.json"
    local_manifest_path = os.path.join(local_manifest_dir, manifest_filename)

    with open(local_manifest_path, "w") as f:
        json.dump(manifest, f, indent=4)

    manifest_blob_path = f"raw/{vendor_folder}/submission={submission_id}/logs/{manifest_filename}"
    upload_blob(
        local_manifest_path,
        manifest_blob_path,
        connection_string,
        container_name,
    )

    print(f"📄 Manifest created and uploaded: {manifest_blob_path}")

def cleanup_old_assets_except(container_client, vendor_folder, submission_id, keep_blob_path):
    prefix = f"raw/{vendor_folder}/submission={submission_id}/assets/"
    blobs = container_client.list_blobs(name_starts_with=prefix)

    for blob in blobs:
        if blob.name != keep_blob_path:
            container_client.delete_blob(blob.name)
            print(f"[CLEANUP] Deleted old asset: {blob.name}")


def read_json_blob_from_azure(
    blob_path: str,
    container_name: str,
    connection_string: str | None = None
) -> dict:
    """
    Reads a JSON file from Azure Blob Storage and returns it as a dict.

    Purpose:
    --------
    - Used by the Vendor Portal UI to fetch ETL status artifacts
    - Shared read-only utility (no side effects)

    Parameters:
    -----------
    blob_path : str
        Full blob path inside the container
        Example:
            logs/vendor=Grote Lighting/submission=20260201_123456/status.json

    container_name : str
        Azure container name (e.g. 'silver')

    connection_string : str | None
        Optional override. If not provided, uses AZURE_STORAGE_CONNECTION_STRING env var.

    Returns:
    --------
    dict
        Parsed JSON content

    Raises:
    -------
    FileNotFoundError
        If blob does not exist
    """

    conn_str = connection_string or os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        raise RuntimeError("Azure connection string not configured")

    blob_service = BlobServiceClient.from_connection_string(conn_str)
    container_client = blob_service.get_container_client(container_name)

    try:
        blob_client = container_client.get_blob_client(blob_path)
        blob_bytes = blob_client.download_blob().readall()
        return json.loads(blob_bytes)

    except ResourceNotFoundError:
        raise FileNotFoundError(f"Blob not found: {blob_path}")
           
def write_status_to_azure(vendor, submission_id, stage, status, message=""):
    from azure.storage.blob import BlobServiceClient
    import json, os
    from datetime import datetime

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    blob_service = BlobServiceClient.from_connection_string(conn)
    container = blob_service.get_container_client("silver")

    payload = {
        "vendor": vendor,
        "submission_id": submission_id,
        "stage": stage,
        "status": status,
        "message": message,
        "updated_at": datetime.utcnow().isoformat() + "Z"
    }

    blob_path = (
        f"logs/vendor={vendor}/"
        f"submission={submission_id}/status.json"
    )

    container.upload_blob(
        name=blob_path,
        data=json.dumps(payload, indent=2),
        overwrite=True
    )

def generate_read_sas_url(blob_service, container_name, blob_name, expiry_hours=2):
    sas = generate_blob_sas(
        account_name=blob_service.account_name,
        container_name=container_name,
        blob_name=blob_name,
        account_key=blob_service.credential.account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(hours=expiry_hours),
    )

    return (
        f"https://{blob_service.account_name}.blob.core.windows.net/"
        f"{container_name}/{blob_name}?{sas}"
    )
