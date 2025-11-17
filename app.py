from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, session
import os
import json
from datetime import datetime
from werkzeug.utils import secure_filename
from azure.storage.blob import BlobServiceClient
import hashlib
from openpyxl import Workbook


# ---------------------------------------
# CONFIGURATION
# ---------------------------------------
app = Flask(__name__)
app.secret_key = "fgi_vendor_portal_secret"   #change later
app.config['SESSION_TYPE'] = 'filesystem'

# Local upload path (Phase 1 temp before Azure)
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
TEMPLATE_FOLDER = os.path.join(os.getcwd(), "data", "templates")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'xml', 'xlsx'}


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

def create_multi_product_excel(
    item_rows,
    desc_rows,
    ext_rows,
    attr_rows,
    interchange_rows,
    package_rows,
    asset_rows,
    price_rows,
    output_dir
):
    """
    Create an Excel file with separate tabs for:
      - Item_Master
      - Descriptions
      - Extended_Info
      - Attributes
      - Part_Interchange
      - Packages
      - Digital_Assets
      - Pricing

    Each *_rows argument is a list of dicts where keys match the column names.
    """
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"single_products_batch_{timestamp}.xlsx"
    filepath = os.path.join(output_dir, filename)

    wb = Workbook()

    # 1) Item_Master
    ws = wb.active
    ws.title = "Item_Master"
    item_cols = [
        "Vendor",
        "Part Number",
        "UNSPSC",
        "HazmatFlag",
        "Product Status",
        "Barcode Type",
        "Barcode Number",
        "Quantity UOM",
        "Quantity Size",
        "VMRS Code",
    ]
    ws.append(item_cols)
    for row in item_rows:
        ws.append([
            row.get("Vendor", ""),
            row.get("Part Number", ""),
            row.get("UNSPSC", ""),
            row.get("HazmatFlag", ""),
            row.get("Product Status", ""),
            row.get("Barcode Type", ""),
            row.get("Barcode Number", ""),
            row.get("Quantity UOM", ""),
            row.get("Quantity Size", ""),
            row.get("VMRS Code", ""),
        ])

    # 2) Descriptions
    ws = wb.create_sheet("Descriptions")
    desc_cols = [
        "SKU",
        "Description Change Type",
        "Description Code",
        "Description Value",
        "Sequence",
    ]
    ws.append(desc_cols)
    for row in desc_rows:
        ws.append([
            row.get("SKU", ""),
            row.get("Description Change Type", ""),
            row.get("Description Code", ""),
            row.get("Description Value", ""),
            row.get("Sequence", ""),
        ])

    # 3) Extended_Info
    ws = wb.create_sheet("Extended_Info")
    ext_cols = [
        "SKU",
        "Extended Info Change Type",
        "Extended Info Code",
        "Extended Info Value",
    ]
    ws.append(ext_cols)
    for row in ext_rows:
        ws.append([
            row.get("SKU", ""),
            row.get("Extended Info Change Type", ""),
            row.get("Extended Info Code", ""),
            row.get("Extended Info Value", ""),
        ])

    # 4) Attributes
    ws = wb.create_sheet("Attributes")
    attr_cols = [
        "SKU",
        "Attribute Change Type",
        "Attribute Name",
        "Attribute Value",
    ]
    ws.append(attr_cols)
    for row in attr_rows:
        ws.append([
            row.get("SKU", ""),
            row.get("Attribute Change Type", ""),
            row.get("Attribute Name", ""),
            row.get("Attribute Value", ""),
        ])

    # 5) Part_Interchange
    ws = wb.create_sheet("Part_Interchange")
    interchange_cols = [
        "SKU",
        "Part Interchange Change Type",
        "Brand Label",
        "Part Number",
    ]
    ws.append(interchange_cols)
    for row in interchange_rows:
        ws.append([
            row.get("SKU", ""),
            row.get("Part Interchange Change Type", ""),
            row.get("Brand Label", ""),
            row.get("Part Number", ""),
        ])

    # 6) Packages
    ws = wb.create_sheet("Packages")
    package_cols = [
        "SKU",
        "Package Change Type",
        "Package UOM",
        "Package Quantity of Eaches",
        "Weight UOM",
        "Weight",
    ]
    ws.append(package_cols)
    for row in package_rows:
        ws.append([
            row.get("SKU", ""),
            row.get("Package Change Type", ""),
            row.get("Package UOM", ""),
            row.get("Package Quantity of Eaches", ""),
            row.get("Weight UOM", ""),
            row.get("Weight", ""),
        ])

    # 7) Digital_Assets
    ws = wb.create_sheet("Digital_Assets")
    asset_cols = [
        "SKU",
        "Digital Change Type",
        "Media Type",
        "FileName",
        "FileLocalPath",
    ]
    ws.append(asset_cols)
    for row in asset_rows:
        ws.append([
            row.get("SKU", ""),
            row.get("Digital Change Type", ""),
            row.get("Media Type", ""),
            row.get("FileName", ""),
            row.get("FileLocalPath", ""),
        ])

    # 8) Pricing
    ws = wb.create_sheet("Pricing")
    price_cols = [
        "Vendor",
        "Part Number",
        "Pricing Method",
        "Currency",
        "MOQ Unit",
        "MOQ",
        "Pricing Change Type",
        "Pricing Type",
        "List Price",
        "Jobber Price",
        "Discount %",
        "Multiplier",
        "Pricing Amount",
        "Tier Min Qty",
        "Tier Max Qty",
        "Effective Date",
        "Start Date",
        "End Date",
        "Core Part Number",
        "Core Cost",
        "Notes",
    ]
    ws.append(price_cols)
    for row in price_rows:
        ws.append([
            row.get("Vendor", ""),
            row.get("Part Number", ""),
            row.get("Pricing Method", ""),
            row.get("Currency", ""),
            row.get("MOQ Unit", ""),
            row.get("MOQ", ""),
            row.get("Pricing Change Type", ""),
            row.get("Pricing Type", ""),
            row.get("List Price", ""),
            row.get("Jobber Price", ""),
            row.get("Discount %", ""),
            row.get("Multiplier", ""),
            row.get("Pricing Amount", ""),
            row.get("Tier Min Qty", ""),
            row.get("Tier Max Qty", ""),
            row.get("Effective Date", ""),
            row.get("Start Date", ""),
            row.get("End Date", ""),
            row.get("Core Part Number", ""),
            row.get("Core Cost", ""),
            row.get("Notes", ""),
        ])

    wb.save(filepath)
    return filepath



# def upload_single_product_excel_to_azure(vendor_name, local_path):
#     timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
#     vendor_folder = f"vendor={vendor_name}"
#     blob_path = f"raw/{vendor_folder}/manual/{timestamp}_single_product.xlsx"
#     return upload_blob(local_path, blob_path)

def validate_single_product_new(form: dict) -> tuple[bool, list[str]]:
    errors = []

    vendor = form.get("vendor_name", "").strip()
    sku = form.get("sku", "").strip()
    unspsc = form.get("unspsc_code", "").strip()
    hazmat = form.get("hazmat_flag", "").strip()
    status = form.get("product_status", "").strip()
    barcode_type = form.get("barcode_type", "").strip()
    barcode_number = form.get("barcode_number", "").strip()
    quantity_uom = form.get("quantity_uom", "").strip()
    quantity_size = form.get("quantity_size", "").strip()
    vmrs = form.get("vmrs_code", "").strip()

    # Required basic
    if not vendor:
        errors.append("Vendor is required.")
    elif vendor not in VENDOR_LIST:
        errors.append(f"Vendor '{vendor}' is not in the allowed list.")

    if not sku:
        errors.append("Part Number (SKU) is required.")

    # UNSPSC
    if unspsc:
        if not unspsc.isdigit() or len(unspsc) != 8:
            errors.append("UNSPSC Code must be exactly 8 numeric digits if provided.")

    # Hazmat
    if hazmat and hazmat not in ["Y", "N"]:
        errors.append("Hazardous Material must be Y or N.")

    # Status
    if status and status not in PRODUCT_STATUS:
        errors.append("Product Status must be one of: " + ", ".join(PRODUCT_STATUS))

    # Barcode
    if barcode_number:
        if not barcode_number.isdigit():
            errors.append("Barcode Number must be numeric.")
        else:
            if barcode_type == "UPC" and len(barcode_number) != 12:
                errors.append("UPC Barcode must be exactly 12 digits.")
            elif barcode_type == "EAN" and len(barcode_number) != 14:
                errors.append("EAN Barcode must be exactly 14 digits.")
            elif not barcode_type and len(barcode_number) not in (12, 14):
                errors.append("Barcode must be 12-digit UPC or 14-digit EAN.")

    if barcode_type and barcode_type not in ["UPC", "EAN"]:
        errors.append("Barcode Type must be UPC or EAN.")

    # Quantity / UOM
    if quantity_uom and quantity_uom not in QUANTITY_UOM:
        errors.append("Quantity Size Unit must be one of: " + ", ".join(QUANTITY_UOM))

    if quantity_size:
        try:
            float(quantity_size)
        except ValueError:
            errors.append("Quantity Size must be numeric.")

    # --- Descriptions: check lengths for DES/SHO ---
    desc_codes = form.getlist("desc_code[]")
    desc_values = form.getlist("desc_value[]")

    for code, value in zip(desc_codes, desc_values):
        code = (code or "").strip()
        value = (value or "").strip()
        if code in ("DES", "SHO") and value and len(value) > 40:
            errors.append(f"Description with code {code} must be <= 40 characters.")

    # --- Pricing method ---
    pricing_method = form.get("pricing_method", "").strip()
    if not pricing_method:
        errors.append("Pricing Method is required.")
    elif pricing_method not in PRICING_METHODS.keys():
        errors.append("Invalid Pricing Method selected.")

    # MOQ
    moq_uom = form.get("moq_uom", "").strip()
    moq_qty = form.get("moq_quantity", "").strip()
    if moq_uom and moq_uom not in QUANTITY_UOM:
        errors.append("Minimum Order Quantity Unit must be valid (EA, CS, PL, etc.).")

    if moq_qty:
        try:
            float(moq_qty)
        except ValueError:
            errors.append("Minimum Order Quantity must be numeric.")

    # Price lines
    price_types = form.getlist("price_type[]")
    price_amounts = form.getlist("price_amount[]")
    price_currencies = form.getlist("price_currency[]")
    price_effective_dates = form.getlist("price_effective_date[]")

    base_price_exists = False
    for idx, (ptype, amt, cur, eff) in enumerate(
        zip(price_types, price_amounts, price_currencies, price_effective_dates)
    ):
        ptype = (ptype or "").strip()
        amt = (amt or "").strip()
        cur = (cur or "").strip()
        eff = (eff or "").strip()

        row_label = f"Price line {idx+1}"

        if not ptype and not amt and not cur and not eff:
            # completely blank line - ignore
            continue

        if not ptype:
            errors.append(f"{row_label}: Pricing Type is required if any pricing values are entered.")
        else:
            if ptype not in ["Base", "Bulk", "Case", "Pallet", "Promo", "Tender", "Quote", "Core", "EHC"]:
                errors.append(f"{row_label}: Invalid Pricing Type.")
            if ptype == "Base":
                base_price_exists = True

        if not cur:
            errors.append(f"{row_label}: Pricing Currency is required.")
        elif cur not in ["CAD", "USD"]:
          errors.append(f"{row_label}: Currency must be CAD or USD.")

        if not amt:
            errors.append(f"{row_label}: Pricing Amount is required.")
        else:
            try:
                float(amt)
            except ValueError:
                errors.append(f"{row_label}: Pricing Amount must be numeric.")

        if not eff:
            errors.append(f"{row_label}: Effective Date is required.")

    if not base_price_exists:
        errors.append("At least one Base Price row is required in Price Lines.")

    return (len(errors) == 0, errors)


# ---------------------------------------
# LOOKUP CONSTANTS / CONTROLLED VOCAB
# ---------------------------------------

VENDOR_LIST = [
    # OptiCat vendors
    "Dayton Parts", "Grote Lighting", "Neapco",
    "Truck Lite", "Baldwin Filters", "Stemco", "High Bar Brands",
    # Non-OptiCat vendors
    "Ride Air", "Tetran", "SAF Holland",
    "Consolidated Metco", "Tiger Tool", "J.W Speaker", "Rigid Industries"
]

CHANGE_TYPES = ["A", "M", "D"]  # Add, Modify, Delete

QUANTITY_UOM = ["EA", "PC", "BOX", "CS", "PK", "SET", "RL", "BG", "BT", "DZ"]
PACKAGING_TYPES = ["BOX", "CASE", "PALLET", "BAG", "BOTTLE", "CAN", "CARTON", "WRAP", "PACK", "CRATE", "TUBE"]
PACKAGE_UOM = ["EA", "BX", "CS", "PL"]


WEIGHT_UOM = ["LB", "KG", "G", "OZ"]
DIMENSION_UOM = ["IN", "CM", "MM"]

LIFECYCLE_STATUS = ["Active", "Inactive", "Obsolete"]
HAZMAT_OPTIONS = ["Y", "N"]
APPLICATION_FLAG = ["Y", "N"]

GTIN_TYPES = ["UPC", "EAN"]

DESCRIPTION_TYPES = ["Short", "Long", "Marketing", "Web", "Extended", "Technical"]

LANGUAGES = ["EN", "FR", "ES"]

MEDIA_TYPES = ["MainImage", "AngleImage", "PDF", "SpecSheet", "Logo", "Thumbnail", "InstallationGuide"]
FILE_FORMATS = ["JPEG", "PNG", "GIF", "PDF", "WEBP"]
COLOR_MODES = ["RGB", "CMYK"]
MEDIA_DATE_TYPES = ["Created", "Updated"]

CURRENCIES = ["CAD", "USD"]

ALT_UOM = ["EA", "PC", "SET", "BOX"]

PRICING_METHODS = {
    "net_cost": "Net Cost Provided",
    "list_plus_discount": "List Price + Discount",
    "jobber_plus_discount": "Jobber Price + Discount",
    "list_only": "List Price Only",
    "jobber_only": "Jobber Price Only"
}

PRODUCT_STATUS = ["Active", "Inactive", "Obsolete"]



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

    # if vendor_name not in OPTICAT_VENDORS:
    #     flash('Only OptiCat vendors are supported in Phase 1.', 'danger')
    #     return redirect(url_for('upload_page'))

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

@app.route('/single-product', methods=['GET'])
def single_product_page():
    pending = session.get('pending_products', [])
    vendor_prefill = session.get('single_vendor_name', "")
    latest_excel = session.get('latest_single_products_excel_path', None)

    return render_template(
        'single_product.html',
        VENDOR_LIST=VENDOR_LIST,
        PRODUCT_STATUS=PRODUCT_STATUS,
        QUANTITY_UOM=QUANTITY_UOM,
        PACKAGE_UOM=PACKAGE_UOM,
        WEIGHT_UOM=WEIGHT_UOM,
        PRICING_METHODS=PRICING_METHODS,
        pending_products=pending,
        vendor_prefill=vendor_prefill,
        latest_excel_available=bool(latest_excel)
    )


@app.route('/single-product', methods=['POST'])
@app.route('/single-product', methods=['POST'])
def submit_single_product():
    action = request.form.get("action")
    ok, errors = validate_single_product_new(request.form)

    vendor_name = request.form.get("vendor_name", "").strip()
    sku = request.form.get("sku", "").strip()
    product_status = request.form.get("product_status", "").strip()
    pricing_method_key = request.form.get("pricing_method", "").strip()

    session['single_vendor_name'] = vendor_name

    if not ok:
        for e in errors:
            flash(e, "danger")
        return redirect(url_for('single_product_page'))

    # --------- Pull / init batch lists from session ----------
    item_rows = session.get("batch_item_rows", [])
    desc_rows = session.get("batch_desc_rows", [])
    ext_rows = session.get("batch_ext_rows", [])
    attr_rows = session.get("batch_attr_rows", [])
    interchange_rows = session.get("batch_interchange_rows", [])
    package_rows = session.get("batch_package_rows", [])
    asset_rows = session.get("batch_asset_rows", [])
    price_rows = session.get("batch_price_rows", [])
    pending = session.get("pending_products", [])

    # --------- SECTION 1: Item Master row ----------
    unspsc = request.form.get("unspsc_code", "").strip()
    hazmat = request.form.get("hazmat_flag", "").strip()
    barcode_type = request.form.get("barcode_type", "").strip()
    barcode_number = request.form.get("barcode_number", "").strip()
    quantity_uom = request.form.get("quantity_uom", "").strip()
    quantity_size = request.form.get("quantity_size", "").strip()
    vmrs = request.form.get("vmrs_code", "").strip()

    item_rows.append({
        "Vendor": vendor_name,
        "Part Number": sku,
        "UNSPSC": unspsc,
        "HazmatFlag": hazmat,
        "Product Status": product_status,
        "Barcode Type": barcode_type,
        "Barcode Number": barcode_number,
        "Quantity UOM": quantity_uom,
        "Quantity Size": quantity_size,
        "VMRS Code": vmrs,
    })

    # --------- SECTION 2: Descriptions (1:M) ----------
    desc_change_types = request.form.getlist("desc_change_type[]")
    desc_codes = request.form.getlist("desc_code[]")
    desc_values = request.form.getlist("desc_value[]")
    desc_sequences = request.form.getlist("desc_sequence[]")

    for ct, code, val, seq in zip(desc_change_types, desc_codes, desc_values, desc_sequences):
        if not (ct or code or val or seq):
            continue
        desc_rows.append({
            "SKU": sku,
            "Description Change Type": ct.strip(),
            "Description Code": code.strip(),
            "Description Value": val.strip(),
            "Sequence": seq.strip(),
        })

    # --------- SECTION 3: Extended Info (1:M) ----------
    ext_change_types = request.form.getlist("ext_change_type[]")
    ext_codes = request.form.getlist("ext_code[]")
    ext_values = request.form.getlist("ext_value[]")

    for ct, code, val in zip(ext_change_types, ext_codes, ext_values):
        if not (ct or code or val):
            continue
        ext_rows.append({
            "SKU": sku,
            "Extended Info Change Type": ct.strip(),
            "Extended Info Code": code.strip(),
            "Extended Info Value": val.strip(),
        })

    # --------- SECTION 4: Attributes (1:M) ----------
    attr_change_types = request.form.getlist("attr_change_type[]")
    attr_names = request.form.getlist("attr_name[]")
    attr_values = request.form.getlist("attr_value[]")

    for ct, name, val in zip(attr_change_types, attr_names, attr_values):
        if not (ct or name or val):
            continue
        attr_rows.append({
            "SKU": sku,
            "Attribute Change Type": ct.strip(),
            "Attribute Name": name.strip(),
            "Attribute Value": val.strip(),
        })

    # --------- SECTION 5: Part Interchange (1:M) ----------
    # NOTE: Your HTML needs inputs:
    #   int_change_type[]
    #   int_brand_label[]
    #   int_part_number[]
    int_change_types = request.form.getlist("int_change_type[]")
    int_brand_labels = request.form.getlist("int_brand_label[]")
    int_part_numbers = request.form.getlist("int_part_number[]")

    for ct, brand, part in zip(int_change_types, int_brand_labels, int_part_numbers):
        if not (ct or brand or part):
            continue
        interchange_rows.append({
            "SKU": sku,
            "Part Interchange Change Type": ct.strip(),
            "Brand Label": brand.strip(),
            "Part Number": part.strip(),
        })

    # --------- SECTION 6: Packages (1:M) ----------
    pack_change_types = request.form.getlist("pack_change_type[]")
    pack_uoms = request.form.getlist("pack_uom[]")
    pack_qty_each = request.form.getlist("pack_qty_each[]")
    pack_weight_uoms = request.form.getlist("pack_weight_uom[]")
    pack_weights = request.form.getlist("pack_weight[]")

    for ct, uom, qty, wuom, wt in zip(
        pack_change_types, pack_uoms, pack_qty_each, pack_weight_uoms, pack_weights
    ):
        if not (ct or uom or qty or wuom or wt):
            continue
        package_rows.append({
            "SKU": sku,
            "Package Change Type": ct.strip(),
            "Package UOM": uom.strip(),
            "Package Quantity of Eaches": qty.strip(),
            "Weight UOM": wuom.strip(),
            "Weight": wt.strip(),
        })

    # --------- SECTION 7: Digital Assets (1:M) ----------
    asset_change_types = request.form.getlist("asset_change_type[]")
    asset_media_types = request.form.getlist("asset_media_type[]")
    asset_files = request.files.getlist("asset_file[]")

    # Save files to uploads/manual_assets/vendor/sku/
    asset_base_dir = os.path.join(app.config['UPLOAD_FOLDER'], "manual_assets", vendor_name, sku)
    os.makedirs(asset_base_dir, exist_ok=True)

    for ct, mtype, file in zip(asset_change_types, asset_media_types, asset_files):
        filename = file.filename if file else ""
        if not (ct or mtype or filename):
            continue
        safe_name = secure_filename(filename)
        local_path = os.path.join(asset_base_dir, safe_name)
        if filename:
            file.save(local_path)
        asset_rows.append({
            "SKU": sku,
            "Digital Change Type": ct.strip(),
            "Media Type": mtype.strip(),
            "FileName": safe_name,
            "FileLocalPath": local_path,
        })

    # --------- SECTION 8: Pricing (1:M) ----------
    pricing_method_label = PRICING_METHODS.get(pricing_method_key, "")
    moq_uom = request.form.get("moq_uom", "").strip()
    moq_qty = request.form.get("moq_quantity", "").strip()

    price_change_types = request.form.getlist("price_change_type[]")
    price_types = request.form.getlist("price_type[]")
    price_currencies = request.form.getlist("price_currency[]")
    price_amounts = request.form.getlist("price_amount[]")
    price_min_qtys = request.form.getlist("price_min_qty[]")
    price_max_qtys = request.form.getlist("price_max_qty[]")
    price_effective_dates = request.form.getlist("price_effective_date[]")
    price_start_dates = request.form.getlist("price_start_date[]")
    price_end_dates = request.form.getlist("price_end_date[]")
    price_core_parts = request.form.getlist("price_core_part[]")
    price_core_costs = request.form.getlist("price_core_cost[]")

    # NEW: optional list/jobber/discount/multiplier/notes fields
    price_list_prices = request.form.getlist("price_list_price[]")
    price_jobber_prices = request.form.getlist("price_jobber_price[]")
    price_discounts = request.form.getlist("price_discount[]")
    price_multipliers = request.form.getlist("price_multiplier[]")
    price_notes = request.form.getlist("price_notes[]")

    for ct, ptype, cur, amt, lpr, jpr, disc, mult, minq, maxq, eff, st, en, cpart, ccost, note in zip(
        price_change_types,
        price_types,
        price_currencies,
        price_amounts,
        price_list_prices,
        price_jobber_prices,
        price_discounts,
        price_multipliers,
        price_min_qtys,
        price_max_qtys,
        price_effective_dates,
        price_start_dates,
        price_end_dates,
        price_core_parts,
        price_core_costs,
        price_notes,
    ):
        if not (ptype or cur or amt or eff or lpr or jpr or disc or mult):
            # completely blank row, skip
            continue

        price_rows.append({
            "Vendor": vendor_name,
            "Part Number": sku,
            "Pricing Method": pricing_method_label,
            "Currency": cur.strip(),
            "MOQ Unit": moq_uom,
            "MOQ": moq_qty,
            "Pricing Change Type": ct.strip(),
            "Pricing Type": ptype.strip(),
            "List Price": lpr.strip(),
            "Jobber Price": jpr.strip(),
            "Discount %": disc.strip(),
            "Multiplier": mult.strip(),
            "Pricing Amount": amt.strip(),
            "Tier Min Qty": minq.strip(),
            "Tier Max Qty": maxq.strip(),
            "Effective Date": eff.strip(),
            "Start Date": st.strip(),
            "End Date": en.strip(),
            "Core Part Number": cpart.strip(),
            "Core Cost": ccost.strip(),
            "Notes": note.strip(),
        })

    # --------- Update pending products summary (for UI table) ----------
    pending.append({
        "sku": sku,
        "vendor_name": vendor_name,
        "product_status": product_status,
        "pricing_method": pricing_method_label,
    })

    # --------- Save back to session ----------
    session['batch_item_rows'] = item_rows
    session['batch_desc_rows'] = desc_rows
    session['batch_ext_rows'] = ext_rows
    session['batch_attr_rows'] = attr_rows
    session['batch_interchange_rows'] = interchange_rows
    session['batch_package_rows'] = package_rows
    session['batch_asset_rows'] = asset_rows
    session['batch_price_rows'] = price_rows
    session['pending_products'] = pending

    # --------- Handle action: Add vs Generate ----------
    if action == "add":
        flash(f"Product {sku} added to batch. You can add more or Generate Excel.", "success")
        return redirect(url_for('single_product_page'))

    if action == "generate":
        output_dir = os.path.join(app.config['UPLOAD_FOLDER'], "single_product_batches")
        excel_path = create_multi_product_excel(
            item_rows,
            desc_rows,
            ext_rows,
            attr_rows,
            interchange_rows,
            package_rows,
            asset_rows,
            price_rows,
            output_dir,
        )
        session['latest_single_products_excel_path'] = excel_path

        # Clear batch after generation
        session['batch_item_rows'] = []
        session['batch_desc_rows'] = []
        session['batch_ext_rows'] = []
        session['batch_attr_rows'] = []
        session['batch_interchange_rows'] = []
        session['batch_package_rows'] = []
        session['batch_asset_rows'] = []
        session['batch_price_rows'] = []
        session['pending_products'] = []

        flash(f"Excel generated for {vendor_name}. You can download it below.", "success")
        return redirect(url_for('single_product_page'))

    # Fallback
    return redirect(url_for('single_product_page'))




@app.route('/download-single-products-excel')
def download_single_products_excel():
    excel_path = session.get('latest_single_products_excel_path')
    if not excel_path or not os.path.isfile(excel_path):
        flash("No generated Excel file found for download.", "warning")
        return redirect(url_for('single_product_page'))

    directory, filename = os.path.split(excel_path)
    return send_from_directory(directory, filename, as_attachment=True)




# ---------------------------------------
# MAIN
# ---------------------------------------
if __name__ == '__main__':
    app.run(debug=True)
