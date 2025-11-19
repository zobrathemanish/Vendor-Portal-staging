"""
Azure Blob Storage services for FGI Vendor Portal
"""
import os
import json
from datetime import datetime
from azure.storage.blob import BlobServiceClient
from services.file_service import compute_file_hash


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

    return manifest.get("assets_hash")


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


def upload_to_azure_bronze_opticat(vendor, xml_local_path, pricing_local_path, zip_path, 
                           connection_string, container_name, upload_folder):
    """
    Upload product XML, pricing XLSX, and assets ZIP to Azure Bronze.
    Includes: hash check, skip-if-same, smart deletion, manifest updates.
    
    Args:
        vendor (str): Vendor name
        xml_local_path (str): Local path to XML file
        pricing_local_path (str): Local path to pricing Excel file
        zip_path (str): Local path to assets ZIP file
        connection_string (str): Azure Storage connection string
        container_name (str): Azure container name
        upload_folder (str): Base upload folder for local manifest storage
        
    Returns:
        None
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    vendor_folder = f"vendor={vendor}"

    blob_service_client = get_blob_service_client(connection_string)
    container_client = blob_service_client.get_container_client(container_name)

    # --------------- XML + Pricing always uploaded ---------------
    xml_blob_path = f"raw/{vendor_folder}/product/{timestamp}_product.xml"
    pricing_blob_path = f"raw/{vendor_folder}/pricing/{timestamp}_pricing.xlsx"

    xml_blob_full = upload_blob(xml_local_path, xml_blob_path, connection_string, container_name)
    pricing_blob_full = upload_blob(pricing_local_path, pricing_blob_path, connection_string, container_name)

    # --------------- Assets ZIP handling ---------------
    assets_blob_full = None
    assets_hash = None

    if zip_path and os.path.isfile(zip_path):

        # Compute hash of new ZIP
        new_hash = compute_file_hash(zip_path)

        # Get previous asset hash from last manifest
        last_hash = get_latest_asset_hash(container_client, vendor_folder)

        if last_hash == new_hash:
            print("🟡 Assets unchanged. Skipping ZIP upload.")
        else:
            print("🟢 Assets changed. Uploading new ZIP...")

            assets_hash = new_hash
            assets_blob_path = f"raw/{vendor_folder}/assets/{timestamp}_assets.zip"
            assets_blob_full = upload_blob(zip_path, assets_blob_path, connection_string, container_name)

            # Clean up old ZIPs
            delete_old_asset_zips(container_client, vendor_folder)
    else:
        print("⚠ No ZIP path provided or file does not exist. Skipping assets upload.")

    # --------------- Create manifest ---------------
    manifest = {
        "vendor": vendor,
        "timestamp": timestamp,
        "azure_xml_blob": xml_blob_full,
        "azure_pricing_blob": pricing_blob_full,
        "azure_assets_blob": assets_blob_full,
        "assets_hash": assets_hash,
    }

    local_manifest_dir = os.path.join(upload_folder, vendor, "opticat")
    os.makedirs(local_manifest_dir, exist_ok=True)
    local_manifest_path = os.path.join(local_manifest_dir, f"manifest_{timestamp}.json")

    with open(local_manifest_path, "w") as f:
        json.dump(manifest, f, indent=4)

    manifest_blob_path = f"raw/{vendor_folder}/logs/manifest_{timestamp}.json"
    upload_blob(local_manifest_path, manifest_blob_path, connection_string, container_name)

    print(f"📄 Manifest created and uploaded: {manifest_blob_path}")

    
def upload_to_azure_bronze_non_opticat(
        vendor,
        unified_local_path,
        zip_path,
        connection_string,
        container_name,
        upload_folder):
    """
    Upload unified XLSX and optional assets ZIP to Azure Bronze for NON-OptiCat vendors.
    Mirrors the structure and behavior of upload_to_azure_bronze_opticat.

    Structure created in Azure:
        raw/vendor=<Vendor>/unified/<timestamp>_unified.xlsx
        raw/vendor=<Vendor>/assets/<timestamp>_assets.zip
        raw/vendor=<Vendor>/logs/manifest_<timestamp>.json

    Includes:
        - hash check for assets ZIP
        - skip upload if same hash
        - delete old ZIPs, keep only latest
        - store manifest locally and in Azure
    """

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    vendor_folder = f"vendor={vendor}"

    blob_service_client = get_blob_service_client(connection_string)
    container_client = blob_service_client.get_container_client(container_name)

    # -------------------- Upload unified XLSX --------------------
    unified_blob_path = f"raw/{vendor_folder}/unified/{timestamp}_unified.xlsx"
    azure_unified_blob = upload_blob(
        unified_local_path,
        unified_blob_path,
        connection_string,
        container_name
    )

    # -------------------- Assets ZIP handling --------------------
    assets_blob_full = None
    assets_hash = None

    if zip_path and os.path.isfile(zip_path):

        # Compute hash of the new ZIP
        new_hash = compute_file_hash(zip_path)

        # Get last uploaded asset hash from manifest
        last_hash = get_latest_asset_hash(container_client, vendor_folder)

        if last_hash == new_hash:
            print("🟡 Assets unchanged. Skipping ZIP upload.")
        else:
            print("🟢 Assets changed. Uploading new ZIP...")

            assets_hash = new_hash
            assets_blob_path = f"raw/{vendor_folder}/assets/{timestamp}_assets.zip"

            assets_blob_full = upload_blob(
                zip_path,
                assets_blob_path,
                connection_string,
                container_name
            )

            # Delete old ZIPs (keep only newest)
            delete_old_asset_zips(container_client, vendor_folder)

    else:
        print("⚠ No ZIP path provided or file does not exist. Skipping assets upload.")

    # -------------------- Create manifest --------------------
    manifest = {
        "vendor": vendor,
        "timestamp": timestamp,
        "azure_unified_blob": azure_unified_blob,
        "azure_assets_blob": assets_blob_full,
        "assets_hash": assets_hash
    }

    # Store manifest locally
    local_manifest_dir = os.path.join(upload_folder, vendor, "non_opticat")
    os.makedirs(local_manifest_dir, exist_ok=True)

    local_manifest_path = os.path.join(
        local_manifest_dir,
        f"manifest_{timestamp}.json"
    )

    with open(local_manifest_path, "w") as f:
        json.dump(manifest, f, indent=4)

    # Upload manifest to Azure
    manifest_blob_path = f"raw/{vendor_folder}/logs/manifest_{timestamp}.json"
    upload_blob(
        local_manifest_path,
        manifest_blob_path,
        connection_string,
        container_name
    )

    print(f"📄 Manifest created and uploaded: {manifest_blob_path}")


    