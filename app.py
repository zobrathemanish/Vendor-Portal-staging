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

def create_multi_product_excel(vendor_name, products, output_dir):
    """
    Create an Excel file with separate tabs for each section:
    Item_Master, Extended_Info, Descriptions, Attributes,
    Alternate_Parts, Packaging, Media_Assets, Pricing.
    Each product in `products` is one row per sheet.
    """
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{vendor_name}_{timestamp}_products_batch.xlsx"
    filepath = os.path.join(output_dir, filename)

    wb = Workbook()

    # 1) Item_Master
    sheet = wb.active
    sheet.title = "Item_Master"
    item_cols = [
        "SKU", "ChangeType", "BrandCode", "BrandName", "UNSPSC", "HazmatFlag",
        "GTIN", "GTINType", "QuantitySize", "QuantityUOM", "PackagingType",
        "Weight", "WeightUOM", "ApplicationFlag", "Status"
    ]
    sheet.append(item_cols)
    for p in products:
        sheet.append([
            p.get("sku"),
            p.get("change_type"),
            p.get("item_brand_code"),
            p.get("item_brand_name"),
            p.get("item_unspsc"),
            p.get("item_hazmat_flag"),
            p.get("item_gtin"),
            p.get("item_gtin_type"),
            p.get("item_quantity_size"),
            p.get("item_quantity_uom"),
            p.get("item_packaging_type"),
            p.get("item_weight"),
            p.get("item_weight_uom"),
            p.get("item_application_flag"),
            p.get("item_status"),
        ])

    # 2) Extended_Info
    sheet = wb.create_sheet("Extended_Info")
    ext_cols = ["SKU", "ChangeType", "InfoCode", "InfoValue", "Language"]
    sheet.append(ext_cols)
    for p in products:
        sheet.append([
            p.get("sku"),
            p.get("change_type"),
            p.get("ext_info_code"),
            p.get("ext_info_value"),
            p.get("ext_language"),
        ])

    # 3) Descriptions
    sheet = wb.create_sheet("Descriptions")
    desc_cols = ["SKU", "ChangeType", "DescriptionType", "DescriptionText", "Sequence", "Language"]
    sheet.append(desc_cols)
    for p in products:
        sheet.append([
            p.get("sku"),
            p.get("change_type"),
            p.get("desc_type"),
            p.get("desc_text"),
            p.get("desc_sequence"),
            p.get("desc_language"),
        ])

    # 4) Attributes
    sheet = wb.create_sheet("Attributes")
    attr_cols = [
        "SKU", "ChangeType", "FeatureName", "IsPrimaryAttribute",
        "FeatureValue", "UnitOfMeasure", "AttributeOrder", "Language"
    ]
    sheet.append(attr_cols)
    for p in products:
        sheet.append([
            p.get("sku"),
            p.get("change_type"),
            p.get("attr_feature_name"),
            p.get("attr_is_primary"),
            p.get("attr_feature_value"),
            p.get("attr_uom"),
            p.get("attr_order"),
            p.get("attr_language"),
        ])

    # 5) Alternate_Parts
    sheet = wb.create_sheet("Alternate_Parts")
    alt_cols = [
        "SKU", "ChangeType", "AltBrandCode", "AltBrandName",
        "AltPartNumber", "AltUOM", "InterchangeQuality"
    ]
    sheet.append(alt_cols)
    for p in products:
        sheet.append([
            p.get("sku"),
            p.get("change_type"),
            p.get("alt_brand_code"),
            p.get("alt_brand_name"),
            p.get("alt_part_number"),
            p.get("alt_uom"),
            p.get("alt_interchange_quality"),
        ])

    # 6) Packaging
    sheet = wb.create_sheet("Packaging")
    pack_cols = [
        "SKU", "ChangeType", "PackUOM", "QuantityOfEach", "Height", "Width",
        "Length", "DimensionUOM", "PackWeight", "PackWeightUOM", "PackGTIN", "GTINType"
    ]
    sheet.append(pack_cols)
    for p in products:
        sheet.append([
            p.get("sku"),
            p.get("change_type"),
            p.get("pack_uom"),
            p.get("pack_qty_each"),
            p.get("pack_height"),
            p.get("pack_width"),
            p.get("pack_length"),
            p.get("pack_dim_uom"),
            p.get("pack_weight"),
            p.get("pack_weight_uom"),
            p.get("pack_gtin"),
            p.get("pack_gtin_type"),
        ])

    # 7) Media_Assets
    sheet = wb.create_sheet("Media_Assets")
    media_cols = [
        "SKU", "ChangeType", "MediaType", "FileName", "FileURL", "FileFormat",
        "ColorMode", "Color", "Resolution", "AssetHeight", "AssetWidth",
        "AssetUOM", "MediaDateType", "MediaDate"
    ]
    sheet.append(media_cols)
    for p in products:
        sheet.append([
            p.get("sku"),
            p.get("change_type"),
            p.get("media_type"),
            p.get("media_file_name"),
            p.get("media_file_url"),
            p.get("media_file_format"),
            p.get("media_color_mode"),
            p.get("media_color"),
            p.get("media_resolution"),
            p.get("media_height"),
            p.get("media_width"),
            p.get("media_uom"),
            p.get("media_date_type"),
            p.get("media_date"),
        ])

    # 8) Pricing
    sheet = wb.create_sheet("Pricing")
    price_cols = [
        "SKU", "VendorCost", "PriceListCost", "EnvironmentalHandlingCost",
        "Currency", "EffectiveDate", "QuoteCost", "QuoteStart", "QuoteEnd",
        "QuoteUsage", "TenderCost", "TenderNumber", "TenderStart", "TenderEnd",
        "PromoPrice", "PromoStart", "PromoEnd", "Core Part Numbers", "Core Cost"
    ]
    sheet.append(price_cols)
    for p in products:
        sheet.append([
            p.get("sku"),
            p.get("price_vendor_cost"),
            p.get("price_list_cost"),
            p.get("price_ehc"),
            p.get("price_currency"),
            p.get("price_effective_date"),
            p.get("price_quote_cost"),
            p.get("price_quote_start"),
            p.get("price_quote_end"),
            p.get("price_quote_usage"),
            p.get("price_tender_cost"),
            p.get("price_tender_number"),
            p.get("price_tender_start"),
            p.get("price_tender_end"),
            p.get("price_promo_price"),
            p.get("price_promo_start"),
            p.get("price_promo_end"),
            p.get("price_core_part_numbers"),
            p.get("price_core_cost"),
        ])

    wb.save(filepath)
    return filepath


# def upload_single_product_excel_to_azure(vendor_name, local_path):
#     timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
#     vendor_folder = f"vendor={vendor_name}"
#     blob_path = f"raw/{vendor_folder}/manual/{timestamp}_single_product.xlsx"
#     return upload_blob(local_path, blob_path)

def validate_single_product(form_data):
    """
    Validate a single product row from the manual entry form.
    Returns (is_valid: bool, errors: list[str]).
    """
    errors = []

    vendor_name = form_data.get("vendor_name", "").strip()
    sku = form_data.get("sku", "").strip()
    change_type = form_data.get("change_type", "").strip()

    # --- Basic required fields ---
    if not vendor_name:
        errors.append("Vendor Name is required.")
    elif vendor_name not in VENDOR_LIST:
        errors.append(f"Vendor '{vendor_name}' is not in the allowed vendor list.")

    if not sku:
        errors.append("Part Number (SKU) is mandatory.")

    if change_type and change_type not in CHANGE_TYPES:
        errors.append("Change Type must be one of A (Add), M (Modify), or D (Delete).")

    # --- Item Master validations ---
    status = form_data.get("item_status", "").strip()
    if status and status not in LIFECYCLE_STATUS:
        errors.append("Status must be one of: Active, Inactive, Obsolete.")

    hazmat = form_data.get("item_hazmat_flag", "").strip()
    if hazmat and hazmat not in HAZMAT_OPTIONS:
        errors.append("Hazardous Material must be Y or N.")

    app_flag = form_data.get("item_application_flag", "").strip()
    if app_flag and app_flag not in APPLICATION_FLAG:
        errors.append("Has Applications / Fitments must be Y or N.")

    qty_uom = form_data.get("item_quantity_uom", "").strip()
    if qty_uom and qty_uom not in QUANTITY_UOM:
        errors.append("Quantity Unit must be one of the standard values (EA, PC, BOX, etc.).")

    packaging_type = form_data.get("item_packaging_type", "").strip()
    if packaging_type and packaging_type not in PACKAGING_TYPES:
        errors.append("Packaging Type must be a standard packaging type (BOX, CASE, PALLET, etc.).")

    weight_uom = form_data.get("item_weight_uom", "").strip()
    if weight_uom and weight_uom not in WEIGHT_UOM:
        errors.append("Weight Unit must be one of: LB, KG, G, OZ.")

    gtin_type = form_data.get("item_gtin_type", "").strip()
    gtin = form_data.get("item_gtin", "").strip()

    if gtin_type and gtin_type not in GTIN_TYPES:
        errors.append("Barcode Type must be UPC or EAN.")

    if gtin:
        if not gtin.isdigit():
            errors.append("Barcode Number (GTIN) must contain only digits.")
        elif gtin_type == "UPC" and len(gtin) != 12:
            errors.append("UPC must be exactly 12 digits.")
        elif gtin_type == "EAN" and len(gtin) != 14:
            errors.append("EAN must be exactly 14 digits.")
        elif not gtin_type and len(gtin) not in (12, 14):
            errors.append("GTIN must be 12-digit UPC or 14-digit EAN.")

    # --- Descriptions ---
    desc_type = form_data.get("desc_type", "").strip()
    if desc_type and desc_type not in DESCRIPTION_TYPES:
        errors.append("Description Type must be one of: Short, Long, Marketing, Web, Extended, Technical.")

    desc_language = form_data.get("desc_language", "").strip()
    if desc_language and desc_language not in LANGUAGES:
        errors.append("Description Language must be a valid language code (EN, FR, ES).")

    # --- Attributes ---
    attr_is_primary = form_data.get("attr_is_primary", "").strip()
    if attr_is_primary and attr_is_primary not in ("Y", "N"):
        errors.append("Is Primary Attribute must be Y or N.")

    attr_language = form_data.get("attr_language", "").strip()
    if attr_language and attr_language not in LANGUAGES:
        errors.append("Attribute Language must be a valid language code (EN, FR, ES).")

    # --- Alternate Parts ---
    alt_uom = form_data.get("alt_uom", "").strip()
    if alt_uom and alt_uom not in ALT_UOM:
        errors.append("Alternate Unit of Measure must be a standard UOM (EA, PC, SET, BOX).")

    # --- Packaging ---
    pack_uom = form_data.get("pack_uom", "").strip()
    if pack_uom and pack_uom not in PACKAGING_TYPES:
        errors.append("Packaging Unit must be a standard packaging type (BOX, CASE, PALLET, etc.).")

    pack_dim_uom = form_data.get("pack_dim_uom", "").strip()
    if pack_dim_uom and pack_dim_uom not in DIMENSION_UOM:
        errors.append("Dimension Unit must be one of: IN, CM, MM.")

    pack_weight_uom = form_data.get("pack_weight_uom", "").strip()
    if pack_weight_uom and pack_weight_uom not in WEIGHT_UOM:
        errors.append("Package Weight UOM must be one of: LB, KG, G, OZ.")

    pack_gtin_type = form_data.get("pack_gtin_type", "").strip()
    if pack_gtin_type and pack_gtin_type not in GTIN_TYPES:
        errors.append("Package Barcode Type must be UPC or EAN.")

    # --- Media Assets ---
    media_type = form_data.get("media_type", "").strip()
    if media_type and media_type not in MEDIA_TYPES:
        errors.append("Media Type must be a valid value (MainImage, PDF, SpecSheet, etc.).")

    media_file_format = form_data.get("media_file_format", "").strip()
    if media_file_format and media_file_format not in FILE_FORMATS:
        errors.append("File Format must be one of: JPEG, PNG, GIF, PDF, WEBP.")

    media_color_mode = form_data.get("media_color_mode", "").strip()
    if media_color_mode and media_color_mode not in COLOR_MODES:
        errors.append("Color Mode must be RGB or CMYK.")

    media_uom = form_data.get("media_uom", "").strip()
    if media_uom and media_uom not in ("PX", "IN"):
        errors.append("Media Dimension Unit must be PX or IN.")

    media_date_type = form_data.get("media_date_type", "").strip()
    if media_date_type and media_date_type not in MEDIA_DATE_TYPES:
        errors.append("Media Date Type must be Created or Updated.")

    # --- Pricing ---
    currency = form_data.get("price_currency", "").strip()
    if currency and currency not in CURRENCIES:
        errors.append("Currency must be CAD or USD.")

    # Numeric sanity checks (optional, light)
    for field in ["item_weight", "pack_weight", "media_height", "media_width",
                  "pack_height", "pack_width", "pack_length",
                  "price_vendor_cost", "price_list_cost", "price_ehc",
                  "price_quote_cost", "price_tender_cost",
                  "price_promo_price", "price_core_cost"]:
        val = form_data.get(field, "").strip()
        if val:
            try:
                float(val)
            except ValueError:
                errors.append(f"{field} must be numeric if provided.")

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

@app.route('/single-product', methods=['GET'])
def single_product_page():
    pending = session.get('pending_products', [])
    vendor_prefill = session.get('single_vendor_name', "")
    change_prefill = session.get('single_change_type', "")
    latest_excel = session.get('latest_single_products_excel_path', None)

    return render_template(
        'single_product.html',
        pending_products=pending,
        vendor_prefill=vendor_prefill,
        change_prefill=change_prefill,
        latest_excel_available=bool(latest_excel),
        VENDOR_LIST=VENDOR_LIST,
        CHANGE_TYPES=CHANGE_TYPES,
        QUANTITY_UOM=QUANTITY_UOM,
        PACKAGING_TYPES=PACKAGING_TYPES,
        WEIGHT_UOM=WEIGHT_UOM,
        DIMENSION_UOM=DIMENSION_UOM,
        LIFECYCLE_STATUS=LIFECYCLE_STATUS,
        HAZMAT_OPTIONS=HAZMAT_OPTIONS,
        APPLICATION_FLAG=APPLICATION_FLAG,
        GTIN_TYPES=GTIN_TYPES,
        DESCRIPTION_TYPES=DESCRIPTION_TYPES,
        LANGUAGES=LANGUAGES,
        MEDIA_TYPES=MEDIA_TYPES,
        FILE_FORMATS=FILE_FORMATS,
        COLOR_MODES=COLOR_MODES,
        MEDIA_DATE_TYPES=MEDIA_DATE_TYPES,
        CURRENCIES=CURRENCIES,
        ALT_UOM=ALT_UOM
    )

@app.route('/single-product', methods=['POST'])
def submit_single_product():
    action = request.form.get("action")
    form_data = dict(request.form)
    form_data.pop("action", None)

    vendor_name = form_data.get("vendor_name", "").strip()
    sku = form_data.get("sku", "").strip()
    change_type = form_data.get("change_type", "").strip()

    # Keep vendor & change type for pre-fill
    session['single_vendor_name'] = vendor_name
    session['single_change_type'] = change_type

    # Validate current product
    ok, errors = validate_single_product(form_data)
    if not ok:
        for e in errors:
            flash(e, "danger")
        return redirect(url_for('single_product_page'))

    # Get existing pending list
    pending = session.get('pending_products', [])

    if action == "add":
        # Only add to in-memory batch, don't save file or upload
        pending.append(form_data)
        session['pending_products'] = pending
        flash(f"Product {sku} added. You can add another or click Generate Excel.", "success")
        return redirect(url_for('single_product_page'))

    if action == "generate":
        # Include current product in batch as well
        pending.append(form_data)
        session['pending_products'] = pending

        if not pending:
            flash("No products available to generate Excel.", "warning")
            return redirect(url_for('single_product_page'))

        # Create Excel file
        output_dir = os.path.join(app.config['UPLOAD_FOLDER'], vendor_name, "single_product_batch")
        excel_path = create_multi_product_excel(vendor_name, pending, output_dir)

        # Upload to Azure Bronze
        # try:
        #     upload_single_products_excel_to_azure(vendor_name, excel_path)
        #     flash(f"Excel for {len(pending)} products uploaded to Azure Bronze.", "success")
        # except Exception as e:
        #     flash(f"Excel saved locally but Azure upload failed: {e}", "danger")

        # Store last path for download
        session['latest_single_products_excel_path'] = excel_path

        # Clear batch
        session['pending_products'] = []

        return redirect(url_for('single_product_page'))

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
