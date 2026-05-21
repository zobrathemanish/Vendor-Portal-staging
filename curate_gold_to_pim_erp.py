#curate_gold_to_pim_erp.py
#Still on development

# =========================================================
# CURATION: GOLD → P21 + AKENEO
# =========================================================

import os
from io import BytesIO
import pandas as pd
from azure.storage.blob import BlobServiceClient
import zipfile
import pyarrow.parquet as pq
import requests

# =========================================================
# CONFIG
# =========================================================
AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
GOLD_CONTAINER = "gold"

# =========================================================
# AZURE HELPERS
# =========================================================
def get_gold_container():
    svc = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
    return svc.get_container_client(GOLD_CONTAINER)

def get_silver_container():
    svc = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
    return svc.get_container_client("silver")  # 👈 adjust if needed

def download_blob(container, path):
    return container.get_blob_client(path).download_blob().readall()

def upload_blob(container, path, data):
    container.upload_blob(path, data, overwrite=True)

# =========================================================
# BUILD AKENEO OUTPUT
# =========================================================
def push_to_akeneo_api(df, vendor):

    from integration.akeneo_client import AkeneoClient

    client = AkeneoClient(
        base_url="https://your-akeneo-instance",
        client_id="xxx",
        client_secret="xxx",
        username="xxx",
        password="xxx"
    )

    products = df_to_akeneo_payload(df)

    url = f"{client.base_url}/api/rest/v1/products"

    # Batch push (Akeneo supports list)
    response = requests.patch(
        url,
        headers=client.headers(),
        json=products
    )

    print("[AKENEO PUSH STATUS]", response.status_code)
    print(response.text)

def push_images_to_akeneo(container, vendor):

    from integration.akeneo_client import AkeneoClient

    client = AkeneoClient(...)

    base_path = f"selected/asset_workflow/vendor={vendor}"

    blobs = container.list_blobs(name_starts_with=base_path)

    for blob in blobs:
        if not blob.name.lower().endswith((".jpg", ".png")):
            continue

        file_bytes = download_blob(container, blob.name)
        filename = os.path.basename(blob.name)

        files = {
            "file": (filename, file_bytes)
        }

        data = {
            "product": filename.split("_")[0],  # adjust if needed
            "attribute": "product_image"
        }

        url = f"{client.base_url}/api/rest/v1/media-files"

        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {client.token}"},
            files=files,
            data=data
        )

        print("[UPLOAD]", filename, response.status_code)

# =========================================================
# AKENEO PAYLOAD CONVERSION
# =========================================================
def df_to_akeneo_payload(df):
    products = []

    for _, row in df.iterrows():

        product = {
            "identifier": row["sku"],
            "family": row["family"],
            "enabled": bool(row["enabled"]),
            "values": {}
        }

        def add_attr(code, value, locale=None):
            if pd.isna(value):
                return
            product["values"][code] = [{
                "locale": locale,
                "scope": None,
                "data": value
            }]

        # Map fields
        add_attr("brand", row.get("brand"))
        add_attr("product_status", row.get("product_status"))
        add_attr("hazardous_material_flag", row.get("hazardous_material_flag"))
        add_attr("category_id", row.get("category_id"))

        add_attr("description", row.get("description"), "en_CA")
        add_attr("extended_description", row.get("extended_description"), "en_CA")

        add_attr("country_of_origin", row.get("country_of_origin"))

        add_attr("default_sales_unit", row.get("default_sales_unit"))
        add_attr("default_sales_pricing_unit", row.get("default_sales_pricing_unit"))

        # Multi-value example
        if pd.notna(row.get("available_uoms")):
            product["values"]["available_uoms"] = [{
                "locale": None,
                "scope": None,
                "data": row["available_uoms"].split(",")
            }]

        # Image
        if pd.notna(row.get("product_image")):
            product["values"]["product_image"] = [{
                "locale": None,
                "scope": None,
                "data": row["product_image"]
            }]

        products.append(product)

    return products

# =========================================================
# LOAD GOLD DATA (EXCEL TABS)
# =========================================================
def load_gold_excel(container, vendor, local=False):

    path = (
        f"selected/unified_workflow/vendor={vendor}/unified_etl_mapped.xlsx"
    )

    try:
        if local:
            tabs = pd.read_excel(path, sheet_name=None, dtype=str)
        else:
            blob = download_blob(container, path)
            tabs = pd.read_excel(BytesIO(blob), sheet_name=None, dtype=str)

        # normalize Part Number
        for df in tabs.values():
            if "Part Number" in df.columns:
                df["Part Number"] = (
                    df["Part Number"]
                    .astype(str)
                    .str.replace(r"\.0$", "", regex=True)
                    .str.strip()
                    .str.zfill(5)   # 👈 adjust length if needed
                )
        
        return tabs

    except Exception as e:
        print("[ERROR] Failed to load GOLD:", e)
        return {}

# =========================================================
# BUILD P21 OUTPUT
# =========================================================
def build_p21(item_master, packages, pricing):

    p21 = item_master.copy()

    # CORE
    p21["item_id"] = p21["Part Number"]
    p21["description"] = p21.get("Description")

    # UOM
    p21["base_uom"] = p21.get("Quantity UOM")
    p21["selling_uom"] = p21.get("Minimum Order Quantity UOM")

    # DISCONTINUED
    p21["discontinued"] = p21.get("Product Status").apply(
        lambda x: 1 if str(x).lower() == "inactive" else 0
    )

    # PACKAGES
    if not packages.empty:
        pkg = packages.groupby("Part Number").first()

        p21["weight"] = p21["Part Number"].map(pkg.get("Weight"))
        p21["length"] = p21["Part Number"].map(pkg.get("Merch Length"))
        p21["width"]  = p21["Part Number"].map(pkg.get("Merch Width"))
        p21["height"] = p21["Part Number"].map(pkg.get("Merch Height"))

    # PRICING
    if not pricing.empty:
        price = pricing.groupby("Part Number").first()

        p21["currency"]  = p21["Part Number"].map(price.get("Currency"))
        p21["net_price"] = p21["Part Number"].map(price.get("Net Price"))

    # FINAL
    cols = [
        "item_id", "description",
        "base_uom", "selling_uom",
        "weight", "length", "width", "height",
        "currency", "net_price", "discontinued"
    ]

    return p21[cols].drop_duplicates()

# =========================================================
# BUILD AKENEO OUTPUT
# =========================================================
def build_akeneo(item_master, descriptions, attributes, extended_info, digital_assets, packages, pricing, container, vendor):

    # ---------------------------
    # BASE (Item Master)
    # ---------------------------
    akeneo = item_master.copy()

    akeneo = akeneo.rename(columns={
        "Part Number": "sku",
        "Brand Label": "brand",
        "Product Status": "product_status",
        "HazmatFlag": "hazardous_material_flag",
        "PartTerminologyID": "category_id"
    })

    akeneo = akeneo[[
        "sku",
        "brand",
        "product_status",
        "hazardous_material_flag",
        "category_id"
    ]]

    # ---------------------------
    # FAMILY (Required for Akeneo)
    # ---------------------------
    akeneo["family"] = "default_parts"

    akeneo["enabled"] = 1

    # ---------------------------
    # UPC / EAN (combine)
    # ---------------------------
    if "Barcode Number" in item_master.columns:
        akeneo["upc"] = item_master["Barcode Number"].astype(str)

    # ---------------------------
    # PRICING (MOQ + Currency)
    # ---------------------------
    if not pricing.empty:
        price = pricing.groupby("Part Number").first()

        akeneo["default_sales_unit"] = akeneo["sku"].map(price.get("MOQ Unit"))
        akeneo["default_sales_pricing_unit"] = akeneo["sku"].map(price.get("Currency"))

    # ---------------------------
    # BASE UNIT
    # ---------------------------
    if "Minimum Order Quantity UOM" in item_master.columns:
        akeneo["base_unit"] = item_master.set_index("Part Number")["Minimum Order Quantity UOM"]
        akeneo["base_unit"] = akeneo["sku"].map(akeneo["base_unit"])

    # ---------------------------
    # PACKAGES (UOM + Weight)
    # ---------------------------
    if not packages.empty:
        pkg = packages.groupby("Part Number")

        akeneo["available_uoms"] = akeneo["sku"].map(
            pkg["Package UOM"].apply(
                lambda x: ",".join(sorted(set(str(v).strip().replace("'", "") for v in x if pd.notna(v))))
            )
        )

        first_pkg = pkg.first()

        akeneo["weight"] = akeneo["sku"].map(first_pkg.get("Weight"))
        akeneo["weight_uom"] = akeneo["sku"].map(first_pkg.get("Weight UOM"))

    # ---------------------------
    # DESCRIPTIONS
    # ---------------------------
    if not descriptions.empty:
        desc_pivot = descriptions.pivot_table(
            index="Part Number",
            columns="Description Code",
            values="Description Value",
            aggfunc="first"
        )

        if "DES" in desc_pivot:
            akeneo["description"] = akeneo["sku"].map(desc_pivot["DES"])

        if "MKT" in desc_pivot:
            akeneo["extended_description"] = akeneo["sku"].map(desc_pivot["MKT"])

    # ---------------------------
    # EXTENDED INFO (CTO)
    # ---------------------------
    if not extended_info.empty:
        ext_pivot = extended_info.pivot_table(
            index="Part Number",
            columns="Extended Info Code",
            values="Extended Info Value",
            aggfunc="first"
        )

        if "CTO" in ext_pivot:
            akeneo["country_of_origin"] = akeneo["sku"].map(ext_pivot["CTO"])

    # ---------------------------
    # IMAGES
    # ---------------------------
    # Load canonical mapping
    try:
        canonical_path = f"approved/assets_workflow/vendor={vendor}/media_canonical.parquet"
        silver_container = get_silver_container()
        raw = download_blob(silver_container, canonical_path)
        canonical_df = pq.read_table(BytesIO(raw)).to_pandas()

        # Filter images only
        canonical_df = canonical_df[canonical_df["media_category"] == "image"]

        # Pick first image per part
        image_map = (
            canonical_df.sort_values("sequence")
            .drop_duplicates("part_number")
            .set_index("part_number")["canonical_filename"]
        )

        akeneo["product_image"] = akeneo["sku"].map(image_map)

    except Exception as e:
        print("[WARN] Canonical mapping failed:", e)

    cols = ["sku", "family", "enabled"] + [c for c in akeneo.columns if c not in ["sku", "family", "enabled"]]
    akeneo = akeneo[cols]

    return akeneo

def build_akeneo_media_zip(container, vendor, local=False):

    print("[START] Building media.zip for Akeneo")

    base_path = f"selected/asset_workflow/vendor={vendor}"

    blob_list = container.list_blobs(name_starts_with=base_path)

    # In-memory zip
    zip_buffer = BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:

        for blob in blob_list:

            blob_name = blob.name

            # skip folders
            if blob_name.endswith("/"):
                continue

            # only images
            if not blob_name.lower().endswith((".jpg", ".jpeg", ".png")):
                continue

            try:
                file_bytes = download_blob(container, blob_name)

                # extract filename only
                part_number = blob_name.split("part_number=")[-1].split("/")[0]
                filename = os.path.basename(blob_name)

                # add to zip (flat structure)
                zf.writestr(filename, file_bytes)

                print(f"[ADD] {filename}")

            except Exception as e:
                print(f"[ERROR] Failed to add {blob_name}: {e}")

    zip_buffer.seek(0)

    # Save
    zip_path = f"akeneo/vendor={vendor}/media.zip"

    if local:
        os.makedirs(f"gold/akeneo/vendor={vendor}", exist_ok=True)
        with open(f"gold/{zip_path}", "wb") as f:
            f.write(zip_buffer.read())
    else:
        upload_blob(container, zip_path, zip_buffer.getvalue())

    print("[DONE] media.zip created and uploaded")

# =========================================================
# SAVE OUTPUTS
# =========================================================
def save_outputs(container, vendor, p21_df, akeneo_df, local=False):

    p21_path = f"p21/vendor={vendor}/products.csv"
    akeneo_path = f"akeneo/vendor={vendor}/products.csv"

    if local:
        os.makedirs(f"gold/p21/vendor={vendor}", exist_ok=True)
        os.makedirs(f"gold/akeneo/vendor={vendor}", exist_ok=True)

        p21_df.to_csv(f"gold/{p21_path}", index=False)
        akeneo_df.to_csv(f"gold/{akeneo_path}", index=False)

    else:
        upload_blob(container, p21_path, p21_df.to_csv(index=False))
        upload_blob(container, akeneo_path, akeneo_df.to_csv(index=False))

    print("[DONE] Outputs saved to GOLD")

# =========================================================
# MAIN
# =========================================================
def run_curation(vendor, local=False):

    container = get_gold_container() if not local else None

    print(f"[START] Curation for vendor={vendor}")

    tabs = load_gold_excel(container, vendor, local)

    if not tabs:
        print("[EXIT] No data")
        return

    item_master   = tabs.get("Item_Master", pd.DataFrame())
    descriptions  = tabs.get("Descriptions", pd.DataFrame())
    attributes    = tabs.get("Attributes", pd.DataFrame())
    extended_info = tabs.get("Extended_Info", pd.DataFrame())
    packages      = tabs.get("Packages", pd.DataFrame())
    digital_assets= tabs.get("Digital_Assets", pd.DataFrame())
    pricing       = tabs.get("Pricing", pd.DataFrame())

    # BUILD
    p21_df = build_p21(item_master, packages, pricing)
    akeneo_df = build_akeneo(
        item_master,
        descriptions,
        attributes,
        extended_info,
        digital_assets,
        packages,
        pricing,
        container,
        vendor
    )
    # SAVE
    save_outputs(container, vendor, p21_df, akeneo_df, local)

    # BUILD MEDIA ZIP
    if not local:
        build_akeneo_media_zip(container, vendor, local)

    print("[COMPLETE] Curation finished")

    # SAVE
    save_outputs(container, vendor, p21_df, akeneo_df, local)

    print("[COMPLETE] Curation finished")

    # AFTER save_outputs
    push_to_akeneo_api(akeneo_df, vendor)

# =========================================================
# CLI
# =========================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", required=True)
    parser.add_argument("--local", action="store_true")

    args = parser.parse_args()

    run_curation(
        vendor=args.vendor,
        local=args.local
    )