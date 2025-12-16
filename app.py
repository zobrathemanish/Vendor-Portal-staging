from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, session
import os
from helpers.lookups import *
from services.file_service import save_file
from services.azure_service import *
from services.excel_service import *
from validators.pricing_validator import validate_single_product_new
from services.azure_service import generate_upload_sas
from datetime import datetime
import hashlib
from dotenv import load_dotenv
load_dotenv()

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

# Azure Storage
AZURE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
AZURE_CONTAINER_NAME = "bronze"

if not AZURE_CONNECTION_STRING:
    print("⚠ WARNING: AZURE_STORAGE_CONNECTION_STRING is not set. "
          "Azure uploads will fail until you configure it.")


# def upload_single_product_excel_to_azure(vendor_name, local_path):
#     timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
#     vendor_folder = f"vendor={vendor_name}"
#     blob_path = f"raw/{vendor_folder}/manual/{timestamp}_single_product.xlsx"
#     return upload_blob(local_path, blob_path)


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
    vendor_type = request.form.get('vendor_type')

    # Shared
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # -------------------------------
    # PROCESS OPTICAT VENDORS
    # -------------------------------
    if vendor_type == "opticat":
        product_file = request.files.get('product_file')
        pricing_file = request.files.get('pricing_file')

        if not product_file or not pricing_file:
            flash('XML and Pricing XLSX are required for OptiCat vendors.', 'danger')
            return redirect(url_for('upload_page'))

        # Save locally first
        product_path = save_file(product_file, vendor_name, "opticat", app.config['UPLOAD_FOLDER'])
        pricing_path = save_file(pricing_file, vendor_name, "opticat", app.config['UPLOAD_FOLDER'])

        try:
            upload_to_azure_bronze_opticat(
                vendor=vendor_name,
                xml_local_path=product_path,
                pricing_local_path=pricing_path,
                connection_string=AZURE_CONNECTION_STRING,
                container_name=AZURE_CONTAINER_NAME,
                upload_folder=app.config['UPLOAD_FOLDER']
            )
            asset_blob_paths = request.form.getlist("asset_blob_path[]")

            if asset_blob_paths:
                latest_asset = asset_blob_paths[-1]

                blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
                container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)

                cleanup_old_assets_except(
                    container_client,
                    f"vendor={vendor_name}",
                    latest_asset
                )


            flash(f'OptiCat files for {vendor_name} uploaded successfully.', 'success')
        except Exception as e:
            flash(f'Azure upload failed: {e}', 'danger')

        return redirect(url_for('upload_page'))

    # -------------------------------
    # PROCESS NON-OPTICAT VENDORS
    # -------------------------------
    elif vendor_type == "non-opticat":
        unified_file = request.files.get('non_opticat_file')

        if not unified_file:
            flash('A unified XLSX file is required for Non-OptiCat vendors.', 'danger')
            return redirect(url_for('upload_page'))

        # Save unified vendor file
        unified_path = save_file(unified_file, vendor_name, "non_opticat", app.config['UPLOAD_FOLDER'])

        try:
            upload_to_azure_bronze_non_opticat(
                vendor=vendor_name,
                unified_local_path=unified_path,
                connection_string=AZURE_CONNECTION_STRING,
                container_name=AZURE_CONTAINER_NAME,
                upload_folder=app.config['UPLOAD_FOLDER']
            )
            asset_blob_paths = request.form.getlist("asset_blob_path[]")

            if asset_blob_paths:
                latest_asset = asset_blob_paths[-1]

                blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
                container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)

                cleanup_old_assets_except(
                    container_client,
                    f"vendor={vendor_name}",
                    latest_asset
                )


            flash(f'Unified file for {vendor_name} uploaded successfully.', 'success')
        except Exception as e:
            flash(f'Azure upload failed: {e}', 'danger')

        return redirect(url_for('upload_page'))

    else:
        flash("Unknown vendor type.", "danger")
        return redirect(url_for("upload_page"))

@app.route("/api/get-asset-upload-sas", methods=["POST"])
def get_asset_upload_sas():
    data = request.json

    vendor = data.get("vendor")
    sku = data.get("sku")
    filename = data.get("filename")

    if not vendor or not filename:
        return {"error": "Missing vendor or filename"}, 400

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    blob_path = (
        f"raw/vendor={vendor}/assets/"
        f"{timestamp}_{filename}"
    )
    print("🔐 Generating SAS for:", blob_path)

    sas_url = generate_upload_sas(
        container=AZURE_CONTAINER_NAME,
        blob_path=blob_path
    )

    return {
        "sas_url": sas_url,
        "blob_path": blob_path
    }

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
    if request.args.get("reset") == "1":
        for k in [
            "pending_products",
            "batch_item_rows",
            "batch_desc_rows",
            "batch_ext_rows",
            "batch_attr_rows",
            "batch_interchange_rows",
            "batch_package_rows",
            "batch_asset_rows",
            "batch_price_rows",
            "latest_single_products_excel_path",
        ]:
            session.pop(k, None)

        session.pop("single_vendor_name", None)
        session.modified = True

    pending = session.get("pending_products", [])
    vendor_prefill = session.get("single_vendor_name", "")

    excel_path = session.get("latest_single_products_excel_path")
    latest_excel_available = bool(
        excel_path and os.path.isfile(excel_path)
    )

    return render_template(
        "single_product.html",
        VENDOR_LIST=VENDOR_LIST,
        PRODUCT_STATUS=PRODUCT_STATUS,
        QUANTITY_UOM=QUANTITY_UOM,
        PACKAGE_UOM=PACKAGE_UOM,
        WEIGHT_UOM=WEIGHT_UOM,
        PRICING_METHODS=PRICING_METHODS,
        DIMENSION_UOM=DIMENSION_UOM,
        pending_products=pending,
        vendor_prefill=vendor_prefill,
        latest_excel_available=latest_excel_available,
    )


@app.route('/single-product', methods=['POST'])
def submit_single_product():
    action = request.form.get("action")

    # ---- Pull batch lists from session (single source of truth) ----
    item_rows        = session.get("batch_item_rows", [])
    desc_rows        = session.get("batch_desc_rows", [])
    ext_rows         = session.get("batch_ext_rows", [])
    attr_rows        = session.get("batch_attr_rows", [])
    interchange_rows = session.get("batch_interchange_rows", [])
    package_rows     = session.get("batch_package_rows", [])
    asset_rows       = session.get("batch_asset_rows", [])
    price_rows       = session.get("batch_price_rows", [])
    pending          = session.get("pending_products", [])

    # ==========================================================
    # GENERATE (READ-ONLY): do NOT parse request.form product data
    # ==========================================================
    if action == "generate":
        if not price_rows:
            flash("At least one pricing row is required to generate Excel.", "danger")
            return redirect(url_for('single_product_page'))

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
        for k in [
            "batch_item_rows",
            "batch_desc_rows",
            "batch_ext_rows",
            "batch_attr_rows",
            "batch_interchange_rows",
            "batch_package_rows",
            "batch_asset_rows",
            "batch_price_rows",
            "pending_products",
        ]:
            session[k] = []

        session.pop("single_vendor_name", None)

        session.modified = True

        flash("Excel generated successfully. You can download it below.", "success")
        return redirect(url_for('single_product_page', generated = 1))

    # ==========================================================
    # ADD (MUTATION): validate + parse request.form + append to batch
    # ==========================================================
    if action != "add":
        flash("Unknown action.", "danger")
        return redirect(url_for('single_product_page'))

    ok, errors = validate_single_product_new(request.form)
    if not ok:
        for e in errors:
            flash(e, "danger")
        return redirect(url_for('single_product_page'))

    # Core identifiers (used by many sections)
    vendor_name    = request.form.get("vendor_name", "").strip()
    existing_vendor = session.get("single_vendor_name")
    if existing_vendor and existing_vendor != vendor_name:
        flash(
            f"Batch already contains products for vendor '{existing_vendor}'. "
            "Please generate or clear the batch first.",
            "danger"
        )
        return redirect(url_for("single_product_page"))

    sku            = request.form.get("sku", "").strip()
    product_status = request.form.get("product_status", "").strip()
    session['single_vendor_name'] = vendor_name

    # --------- SECTION 1: Item Master row ----------
    unspsc = request.form.get("unspsc_code", "").strip()
    hazmat = request.form.get("hazmat_flag", "").strip()
    barcode_type = request.form.get("barcode_type", "").strip()
    barcode_number = request.form.get("barcode_number", "").strip()
    quantity_uom = request.form.get("quantity_uom", "").strip()
    quantity_size = request.form.get("quantity_size", "").strip()
    vmrs = request.form.get("vmrs_code", "").strip()

    existing_skus = {r["Part Number"] for r in item_rows}
    if sku in existing_skus:
        flash(f"SKU '{sku}' is already in the batch.", "danger")
        return redirect(url_for("single_product_page"))


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

    # New fields
    pack_dim_uom = request.form.getlist("pack_dim_uom[]")
    pack_merch_length = request.form.getlist("pack_merch_length[]")
    pack_merch_width = request.form.getlist("pack_merch_width[]")
    pack_merch_height = request.form.getlist("pack_merch_height[]")
    pack_ship_length = request.form.getlist("pack_ship_length[]")
    pack_ship_width = request.form.getlist("pack_ship_width[]")
    pack_ship_height = request.form.getlist("pack_ship_height[]")

    for ct, uom, qty, wuom, wt, dim_uom, merch_len, merch_wid, merch_ht, ship_len, ship_wid, ship_ht in zip(
        pack_change_types,
        pack_uoms,
        pack_qty_each,
        pack_weight_uoms,
        pack_weights,
        pack_dim_uom,
        pack_merch_length,
        pack_merch_width,
        pack_merch_height,
        pack_ship_length,
        pack_ship_width,
        pack_ship_height
    ):
        # Skip if nothing entered
        if not (ct or uom or qty or wuom or wt or dim_uom or merch_len or merch_wid or merch_ht or ship_len or ship_wid or ship_ht):
            continue

        package_rows.append({
            "SKU": sku,
            "Package Change Type": ct.strip(),
            "Package UOM": uom.strip(),
            "Package Quantity of Eaches": qty.strip(),
            "Weight UOM": wuom.strip(),
            "Weight": wt.strip(),

            # New fields
            "Dimension UOM": dim_uom.strip(),
            "Merch Length": merch_len.strip(),
            "Merch Width": merch_wid.strip(),
            "Merch Height": merch_ht.strip(),
            "Ship Length": ship_len.strip(),
            "Ship Width": ship_wid.strip(),
            "Ship Height": ship_ht.strip(),
        })


    # --------- SECTION 7: Digital Assets (1:M) ----------
    asset_change_types = request.form.getlist("asset_change_type[]")
    asset_media_types  = request.form.getlist("asset_media_type[]")
    asset_filenames    = request.form.getlist("asset_filename[]")
    asset_paths        = request.form.getlist("asset_path[]")

    for ct, mt, fname, path in zip(
        asset_change_types,
        asset_media_types,
        asset_filenames,
        asset_paths,
    ):
        # Require media type + filename at minimum
        if not mt or not fname:
            continue

        asset_rows.append({
            "SKU": sku,
            "Digital Change Type": ct.strip(),
            "Media Type": mt.strip(),
            "File Name": fname.strip(),
            "File Path": path.strip() if path else "",
        })


    # --------- SECTION 8: Pricing (multi-level) ----------
    level_types = request.form.getlist("level_type[]")
    level_price_change_types = request.form.getlist("level_price_change_type[]")
    level_moq_uoms = request.form.getlist("level_moq_uom[]")
    level_moq_qtys = request.form.getlist("level_moq_qty[]")
    level_currencies = request.form.getlist("level_currency[]")
    level_methods = request.form.getlist("level_pricing_method[]")
    level_tier_min_qtys = request.form.getlist("level_tier_min_qty[]")
    level_tier_max_qtys = request.form.getlist("level_tier_max_qty[]")

    net_list = request.form.getlist("level_net_list_price[]")
    net_costs = request.form.getlist("level_net_net_cost[]")
    net_effective_dates = request.form.getlist("level_net_effective_date[]")

    pl_list = request.form.getlist("level_pl_list_price[]")
    pl_jobber = request.form.getlist("level_pl_jobber_price[]")
    pl_net = request.form.getlist("level_pl_net_cost[]")
    pl_effective_dates = request.form.getlist("level_pl_effective_date[]")

    db_base = request.form.getlist("level_db_base_price[]")
    db_discount = request.form.getlist("level_db_discount_pct[]")
    db_list_opt = request.form.getlist("level_db_list_price_opt[]")
    db_effective_dates = request.form.getlist("level_db_effective_date[]")

    ehc_base = request.form.getlist("level_ehc_base_price[]")
    ehc_cb = request.form.getlist("level_ehc_canadian_blue[]")
    ehc_qty_case = request.form.getlist("level_ehc_qty_case[]")
    ehc_upc_each = request.form.getlist("level_ehc_upc_each[]")
    ehc_upc_case = request.form.getlist("level_ehc_upc_case[]")
    ehc_moq = request.form.getlist("level_ehc_moq[]")

    ehc_abmbsk_each = request.form.getlist("level_ehc_abmbsk_each[]")
    ehc_abmbsk_case = request.form.getlist("level_ehc_abmbsk_case[]")
    ehc_bc_each = request.form.getlist("level_ehc_bc_each[]")
    ehc_bc_case = request.form.getlist("level_ehc_bc_case[]")
    ehc_nl_each = request.form.getlist("level_ehc_nl_each[]")
    ehc_nl_case = request.form.getlist("level_ehc_nl_case[]")
    ehc_ns_each = request.form.getlist("level_ehc_ns_each[]")
    ehc_ns_case = request.form.getlist("level_ehc_ns_case[]")
    ehc_nbqc_each = request.form.getlist("level_ehc_nbqc_each[]")
    ehc_nbqc_case = request.form.getlist("level_ehc_nbqc_case[]")
    ehc_pei_each = request.form.getlist("level_ehc_pei_each[]")
    ehc_pei_case = request.form.getlist("level_ehc_pei_case[]")
    ehc_yk_each = request.form.getlist("level_ehc_yk_each[]")
    ehc_yk_case = request.form.getlist("level_ehc_yk_case[]")

    pr_price = request.form.getlist("level_pr_promo_price[]")
    pr_start = request.form.getlist("level_pr_start_date[]")
    pr_end = request.form.getlist("level_pr_end_date[]")

    qt_price = request.form.getlist("level_qt_price[]")
    qt_number = request.form.getlist("level_qt_number[]")
    qt_start = request.form.getlist("level_qt_start_date[]")
    qt_end = request.form.getlist("level_qt_end_date[]")

    td_price = request.form.getlist("level_td_price[]")
    td_number = request.form.getlist("level_td_number[]")
    td_start = request.form.getlist("level_td_start_date[]")
    td_end = request.form.getlist("level_td_end_date[]")

    cp_price = request.form.getlist("level_core_list_price[]")
    cp_part_number = request.form.getlist("level_core_part_number[]")

    n_levels = len(level_types)

    def pad(lst):
        return lst + [""] * (n_levels - len(lst))

    level_price_change_types = pad(level_price_change_types)
    level_moq_uoms = pad(level_moq_uoms)
    level_moq_qtys = pad(level_moq_qtys)
    level_currencies = pad(level_currencies)
    level_methods = pad(level_methods)
    level_tier_min_qtys = pad(level_tier_min_qtys)
    level_tier_max_qtys = pad(level_tier_max_qtys)

    net_list = pad(net_list)
    net_costs = pad(net_costs)
    net_effective_dates = pad(net_effective_dates)
    pl_list = pad(pl_list)
    pl_jobber = pad(pl_jobber)
    pl_net = pad(pl_net)
    pl_effective_dates = pad(pl_effective_dates)
    db_base = pad(db_base)
    db_discount = pad(db_discount)
    db_list_opt = pad(db_list_opt)
    db_effective_dates = pad(db_effective_dates)
    ehc_base = pad(ehc_base)
    ehc_cb = pad(ehc_cb)
    ehc_qty_case = pad(ehc_qty_case)
    ehc_upc_each = pad(ehc_upc_each)
    ehc_upc_case = pad(ehc_upc_case)
    ehc_moq = pad(ehc_moq)
    ehc_abmbsk_each = pad(ehc_abmbsk_each)
    ehc_abmbsk_case = pad(ehc_abmbsk_case)
    ehc_bc_each = pad(ehc_bc_each)
    ehc_bc_case = pad(ehc_bc_case)
    ehc_nl_each = pad(ehc_nl_each)
    ehc_nl_case = pad(ehc_nl_case)
    ehc_ns_each = pad(ehc_ns_each)
    ehc_ns_case = pad(ehc_ns_case)
    ehc_nbqc_each = pad(ehc_nbqc_each)
    ehc_nbqc_case = pad(ehc_nbqc_case)
    ehc_pei_each = pad(ehc_pei_each)
    ehc_pei_case = pad(ehc_pei_case)
    ehc_yk_each = pad(ehc_yk_each)
    ehc_yk_case = pad(ehc_yk_case)
    pr_price = pad(pr_price)
    pr_start = pad(pr_start)
    pr_end = pad(pr_end)
    qt_price = pad(qt_price)
    qt_number = pad(qt_number)
    qt_start = pad(qt_start)
    qt_end = pad(qt_end)
    td_price = pad(td_price)
    td_number = pad(td_number)
    td_start = pad(td_start)
    td_end = pad(td_end)
    cp_price = pad(cp_price)
    cp_part_number = pad(cp_part_number)

    pricing_method_labels_set = set()

    for i in range(n_levels):
        lvl_type = (level_types[i] or "").strip()
        lvl_change = (level_price_change_types[i] or "").strip() or "A"
        lvl_moq_uom = (level_moq_uoms[i] or "").strip()
        lvl_moq_qty = (level_moq_qtys[i] or "").strip()
        lvl_currency = (level_currencies[i] or "").strip()
        lvl_method = (level_methods[i] or "").strip()

        if not (
            lvl_type or lvl_method or lvl_currency or lvl_moq_uom or lvl_moq_qty
            or net_list[i] or net_costs[i] or net_effective_dates[i]
            or pl_list[i] or pl_jobber[i] or pl_net[i] or pl_effective_dates[i]
            or db_base[i] or db_discount[i] or db_effective_dates[i]
            or ehc_base[i] or pr_price[i] or qt_price[i] or td_price[i] or cp_part_number[i] or cp_price[i]
        ):
            continue

        method_label = PRICING_METHODS.get(lvl_method, lvl_method)
        pricing_method_labels_set.add(method_label)

        row = {
            "Vendor": vendor_name,
            "Part Number": sku,
            "Pricing Method": method_label,
            "Currency": lvl_currency,
            "MOQ Unit": lvl_moq_uom,
            "MOQ": lvl_moq_qty,
            "Pricing Change Type": lvl_change,
            "Pricing Type": lvl_type,  # Each / Case / Pallet / Bulk
            "List Price": "",
            "Jobber Price": "",
            "Discount %": "",
            "Multiplier": "",
            "Pricing Amount": "",
            "Tier Min Qty": (level_tier_min_qtys[i] or "").strip(),
            "Tier Max Qty": (level_tier_max_qtys[i] or "").strip(),
            "Effective Date": "",
            "Start Date": "",
            "End Date": "",
            "Core Part Number": "",
            "Core Cost": "",
            # --- EHC FIELDS ---
            "EHC AB_MB_SK Each": "",
            "EHC AB_MB_SK Case": "",
            "EHC BC Each": "",
            "EHC BC Case": "",
            "EHC NL Each": "",
            "EHC NL Case": "",
            "EHC NS Each": "",
            "EHC NS Case": "",
            "EHC NB_QC Each": "",
            "EHC NB_QC Case": "",
            "EHC PEI Each": "",
            "EHC PEI Case": "",
            "EHC YK Each": "",
            "EHC YK Case": "",
            "Notes": "",
        }

        if lvl_method == "net_cost":
            row["List Price"] = (net_list[i] or "").strip()
            row["Pricing Amount"] = row["Net Price"] = (net_costs[i] or "").strip()
            row["Effective Date"] = (net_effective_dates[i] or "").strip()

        elif lvl_method == "price_levels":
            row["List Price"] = (pl_list[i] or "").strip()
            row["Jobber Price"] = (pl_jobber[i] or "").strip()
            row["Pricing Amount"] = row["Net Price"] = (pl_net[i] or "").strip()
            row["Effective Date"] = (pl_effective_dates[i] or "").strip()

        elif lvl_method == "discount_based":
            base_val = (db_base[i] or "").strip()
            disc_val = (db_discount[i] or "").strip()
            row["List Price"] = (db_list_opt[i] or "").strip()
            row["Discount %"] = disc_val
            try:
                b = float(base_val)
                d = float(disc_val) if disc_val else 0.0
                net_val = b * (1 - d)
                row["Pricing Amount"] = row["Net Price"] = f"{net_val:.4f}"
            except ValueError:
                row["Pricing Amount"] = row["Net Price"] = base_val
            row["Effective Date"] = (db_effective_dates[i] or "").strip()

        elif lvl_method == "ehc_based":
            base = (ehc_base[i] or "").strip()
            row["Pricing Amount"] = base

            row["EHC AB_MB_SK Each"] = (ehc_abmbsk_each[i] or "").strip()
            row["EHC AB_MB_SK Case"] = (ehc_abmbsk_case[i] or "").strip()
            row["EHC BC Each"] = (ehc_bc_each[i] or "").strip()
            row["EHC BC Case"] = (ehc_bc_case[i] or "").strip()
            row["EHC NL Each"] = (ehc_nl_each[i] or "").strip()
            row["EHC NL Case"] = (ehc_nl_case[i] or "").strip()
            row["EHC NS Each"] = (ehc_ns_each[i] or "").strip()
            row["EHC NS Case"] = (ehc_ns_case[i] or "").strip()
            row["EHC NB_QC Each"] = (ehc_nbqc_each[i] or "").strip()
            row["EHC NB_QC Case"] = (ehc_nbqc_case[i] or "").strip()
            row["EHC PEI Each"] = (ehc_pei_each[i] or "").strip()
            row["EHC PEI Case"] = (ehc_pei_case[i] or "").strip()
            row["EHC YK Each"] = (ehc_yk_each[i] or "").strip()
            row["EHC YK Case"] = (ehc_yk_case[i] or "").strip()

            row["Notes"] = "EHC fees provided by region."


        elif lvl_method == "promo_pricing":
            row["Pricing Amount"] = (pr_price[i] or "").strip()
            row["Start Date"] = (pr_start[i] or "").strip()
            row["End Date"] = (pr_end[i] or "").strip()

        elif lvl_method == "quote_pricing":
            row["Pricing Amount"] = (qt_price[i] or "").strip()
            row["Start Date"] = (qt_start[i] or "").strip()
            row["End Date"] = (qt_end[i] or "").strip()
            qn = (qt_number[i] or "").strip()
            if qn:
                row["Notes"] = f"Quote #: {qn}"

        elif lvl_method == "tender_pricing":
            row["Pricing Amount"] = (td_price[i] or "").strip()
            row["Start Date"] = (td_start[i] or "").strip()
            row["End Date"] = (td_end[i] or "").strip()
            tn = (td_number[i] or "").strip()
            if tn:
                row["Notes"] = f"Tender #: {tn}"
        
        elif lvl_method == "core_pricing":
            row["Core Cost"] = (cp_price[i] or "").strip()
            row["Core Part Number"] = (cp_part_number[i] or "").strip()

        price_rows.append(row)

    pricing_method_label_summary = (
        ", ".join(sorted(pricing_method_labels_set)) if pricing_method_labels_set else ""
    )


    # --------- Update pending products summary (for UI table) ----------
    pending.append({
        "sku": sku,
        "vendor_name": vendor_name,
        "product_status": product_status,
        "pricing_method": pricing_method_label_summary,
    })

# -------- SAVE batch back to session (single write) --------
    session.update({
        "batch_item_rows": item_rows,
        "batch_desc_rows": desc_rows,
        "batch_ext_rows": ext_rows,
        "batch_attr_rows": attr_rows,
        "batch_interchange_rows": interchange_rows,
        "batch_package_rows": package_rows,
        "batch_asset_rows": asset_rows,
        "batch_price_rows": price_rows,
        "pending_products": pending,
    })
    session.modified = True

    flash(f"Product {sku} added to batch. You can add more or Generate Excel.", "success")
    return redirect(url_for('single_product_page', generated=1))


@app.route('/download-single-products-excel')
def download_single_products_excel():
    excel_path = session.get('latest_single_products_excel_path')

    if not excel_path or not os.path.isfile(excel_path):
        flash("No generated Excel file found for download.", "warning")
        return redirect(url_for('single_product_page'))

    directory, filename = os.path.split(excel_path)

    # Prepare response FIRST
    response = send_from_directory(directory, filename, as_attachment=True)

    # ---- CLEAR SINGLE PRODUCT SESSION STATE (AFTER response prepared) ----
    for k in [
        "batch_item_rows",
        "batch_desc_rows",
        "batch_ext_rows",
        "batch_attr_rows",
        "batch_interchange_rows",
        "batch_package_rows",
        "batch_asset_rows",
        "batch_price_rows",
        "pending_products",
        "latest_single_products_excel_path",
    ]:
        session.pop(k, None)

    session.pop("single_vendor_name", None)
    session.modified = True

    return response

    

@app.route("/api/check-asset-hash", methods=["POST"])
def check_asset_hash():
    data = request.json
    vendor = data["vendor"]
    client_hash = data["file_hash"]

    blob_service = BlobServiceClient.from_connection_string(
        os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    )
    container_client = blob_service.get_container_client(AZURE_CONTAINER_NAME)

    prefix = f"raw/vendor={vendor}/assets/"

    blobs = list(container_client.list_blobs(name_starts_with=prefix))
    if not blobs:
        return {"skip": False}

    # Only one asset should exist
    blob = blobs[0]

    blob_data = container_client.download_blob(blob.name).readall()
    server_hash = hashlib.sha256(blob_data).hexdigest()

    if server_hash == client_hash:
        return {
            "skip": True,
            "existing_blob_path": blob.name
        }

    return {"skip": False}

@app.route("/api/cleanup-old-assets", methods=["POST"])
def cleanup_old_assets():
    data = request.json
    vendor = data["vendor"]
    keep_blob_path = data["keep_blob_path"]

    blob_service = BlobServiceClient.from_connection_string(
        os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    )
    container_client = blob_service.get_container_client(AZURE_CONTAINER_NAME)

    prefix = f"raw/vendor={vendor}/assets/"

    for blob in container_client.list_blobs(name_starts_with=prefix):
        if blob.name != keep_blob_path:
            container_client.delete_blob(blob.name)
            print(f"🧹 Deleted old asset: {blob.name}")

    return {"status": "cleanup_complete"}


# ---------------------------------------
# MAIN
# ---------------------------------------
if __name__ == '__main__':
    app.run(debug=True)
