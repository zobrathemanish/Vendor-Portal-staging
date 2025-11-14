from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
import os
import json
from datetime import datetime
from werkzeug.utils import secure_filename
from azure.storage.blob import BlobServiceClient
import hashlib

# ---------------------------------------
# CONFIGURATION
# ---------------------------------------
app = Flask(__name__)
app.secret_key = "fgi_vendor_portal_secret"  # Change this later

# Local upload path (Phase 1 temp before Azure)
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
TEMPLATE_FOLDER = os.path.join(os.getcwd(), "data", "templates")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'xml', 'xlsx'}

# OptiCat Vendors Only (Phase 1)
OPTICAT_VENDORS = {
    "Dayton Parts", "Grote Lighting", "Neapco",
    "Truck Lite", "Baldwin Filters", "Stemco", "High Bar Brands"
}

# Azure Storage
AZURE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
AZURE_CONTAINER_NAME = "bronze"

if not AZURE_CONNECTION_STRING:
    print("⚠ WARNING: AZURE_STORAGE_CONNECTION_STRING is not set. "
          "Azure uploads will fail until you configure it.")


# ---------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_file(file, vendor_name, subfolder):
    """Save file locally (Phase 1 temp)."""
    filename = secure_filename(file.filename)
    vendor_folder = os.path.join(app.config['UPLOAD_FOLDER'], vendor_name, subfolder)
    os.makedirs(vendor_folder, exist_ok=True)
    filepath = os.path.join(vendor_folder, filename)
    file.save(filepath)
    return filepath


def compute_file_hash(filepath):
    """Compute SHA-256 of ZIP without loading into RAM."""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def get_blob_service_client():
    if not AZURE_CONNECTION_STRING:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is not configured.")
    return BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)


def get_latest_asset_hash(container_client, vendor_folder):
    """Return last uploaded asset hash from Azure manifest."""
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
    prefix = f"raw/{vendor_folder}/assets/"
    blobs = list(container_client.list_blobs(name_starts_with=prefix))

    if len(blobs) <= 1:
        return  # Nothing to delete

    blobs_sorted = sorted(blobs, key=lambda b: b.name)

    for old_blob in blobs_sorted[:-1]:
        container_client.delete_blob(old_blob.name)
        print(f"[CLEANUP] Deleted old asset ZIP: {old_blob.name}")


def upload_blob(local_path, blob_path):
    """Upload a file to Azure under the bronze container."""
    blob_service_client = get_blob_service_client()
    container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)
    blob_client = container_client.get_blob_client(blob_path)

    with open(local_path, "rb") as data:
        blob_client.upload_blob(data, overwrite=True)

    print(f"✅ Uploaded to Azure: {blob_path}")
    return f"{AZURE_CONTAINER_NAME}/{blob_path}"


def upload_to_azure_bronze(vendor, xml_local_path, pricing_local_path, zip_path):
    """
    Upload product XML, pricing XLSX, and assets ZIP to Azure Bronze.
    Includes: hash check, skip-if-same, smart deletion, manifest updates.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    vendor_folder = f"vendor={vendor}"

    blob_service_client = get_blob_service_client()
    container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)

    # --------------- XML + Pricing always uploaded ---------------
    xml_blob_path = f"raw/{vendor_folder}/product/{timestamp}_product.xml"
    pricing_blob_path = f"raw/{vendor_folder}/pricing/{timestamp}_pricing.xlsx"

    xml_blob_full = upload_blob(xml_local_path, xml_blob_path)
    pricing_blob_full = upload_blob(pricing_local_path, pricing_blob_path)

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
            assets_blob_full = upload_blob(zip_path, assets_blob_path)

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

    local_manifest_dir = os.path.join(app.config['UPLOAD_FOLDER'], vendor, "opticat")
    os.makedirs(local_manifest_dir, exist_ok=True)
    local_manifest_path = os.path.join(local_manifest_dir, f"manifest_{timestamp}.json")

    with open(local_manifest_path, "w") as f:
        json.dump(manifest, f, indent=4)

    manifest_blob_path = f"raw/{vendor_folder}/logs/manifest_{timestamp}.json"
    upload_blob(local_manifest_path, manifest_blob_path)

    print(f"📄 Manifest created and uploaded: {manifest_blob_path}")


# ---------------------------------------
# ROUTES
# ---------------------------------------

@app.route('/')
def index():
    return redirect(url_for('upload_page'))


@app.route('/upload', methods=['GET'])
def upload_page():
    return render_template('uploads.html')


@app.route('/upload', methods=['POST'])
def upload_files():
    vendor_name = request.form.get('vendor_name')
    zip_path = request.form.get('zip_path')

    if vendor_name not in OPTICAT_VENDORS:
        flash('Only OptiCat vendors are supported in Phase 1.', 'danger')
        return redirect(url_for('upload_page'))

    product_file = request.files.get('product_file')
    pricing_file = request.files.get('pricing_file')

    if not product_file or not pricing_file:
        flash('Product XML and Pricing XLSX are required.', 'danger')
        return redirect(url_for('upload_page'))

    product_path = save_file(product_file, vendor_name, "opticat")
    pricing_path = save_file(pricing_file, vendor_name, "opticat")

    try:
        upload_to_azure_bronze(vendor_name, product_path, pricing_path, zip_path)
        flash(f'Files for {vendor_name} uploaded to Azure Bronze successfully.', 'success')
    except Exception as e:
        print(f"❌ Azure upload failed: {e}")
        flash(f'Azure upload failed: {e}', 'danger')

    return redirect(url_for('upload_page'))


@app.route('/download-template')
def download_template():
    try:
        return send_from_directory(TEMPLATE_FOLDER, "standard_template.xlsx", as_attachment=True)
    except FileNotFoundError:
        flash("Template not found on server.", "danger")
        return redirect(url_for('upload_page'))


@app.route('/form-help')
def form_submission_help():
    return "<h4>Coming soon: Vendor submission help guide</h4>"


# ---------------------------------------
# MAIN
# ---------------------------------------
if __name__ == '__main__':
    app.run(debug=True)
