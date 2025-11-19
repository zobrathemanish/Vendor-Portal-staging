# FGI Vendor Portal - Technical Documentation
## Phase 1 Implementation Report

**Prepared by:** Manish Dangi  
**Organization:** Fort Garry Industries Ltd.  
**Project:** ETL Staging Environment - Vendor Data Ingestion Portal  
**Date:** November 2025  
**Version:** 1.0

---

## Executive Summary

The FGI Vendor Portal is a Flask-based web application designed to streamline vendor data ingestion, validation, and processing as part of Phase 1 of the ETL Staging Environment project. The portal provides two primary submission methods:

1. **Bulk Upload:** For OptiCat and Non-OptiCat vendors to submit standardized files
2. **Single Product Entry:** A comprehensive form-based interface for manual product data entry

All submitted data is validated, stored locally, and uploaded to Azure Blob Storage with intelligent asset management and manifest tracking.

---

## Table of Contents

1. [Project Architecture](#project-architecture)
2. [Technology Stack](#technology-stack)
3. [Application Structure](#application-structure)
4. [Module Documentation](#module-documentation)
5. [Data Flow](#data-flow)
6. [Key Features](#key-features)
7. [Azure Integration](#azure-integration)
8. [Security Considerations](#security-considerations)
9. [Future Enhancements](#future-enhancements)

---

## 1. Project Architecture

### 1.1 High-Level Architecture

The vendor portal follows a modular, service-oriented architecture with clear separation of concerns:

```
┌─────────────────────────────────────────────────────────┐
│                    Flask Web Application                │
│                        (app.py)                         │
└────────────────────┬────────────────────────────────────┘
                     │
         ┌───────────┴───────────┐
         │                       │
    ┌────▼────┐           ┌─────▼─────┐
    │  Routes │           │ Templates │
    │ Layer   │           │  (HTML)   │
    └────┬────┘           └───────────┘
         │
    ┌────▼──────────────────────────────────┐
    │         Service Layer                 │
    ├───────────────────────────────────────┤
    │ • file_service.py                     │
    │ • azure_service.py                    │
    │ • excel_service.py                    │
    └────┬──────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────┐
    │      Validation & Helpers             │
    ├───────────────────────────────────────┤
    │ • pricing_validator.py                │
    │ • lookups.py                          │
    └────┬──────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────┐
    │        External Systems               │
    ├───────────────────────────────────────┤
    │ • Azure Blob Storage (Bronze Layer)   │
    │ • Local File System (Temp Storage)    │
    └───────────────────────────────────────┘
```

### 1.2 Design Principles

- **Separation of Concerns:** Business logic separated from presentation and data access
- **Modularity:** Each service handles a specific domain (files, Azure, Excel, validation)
- **Reusability:** Services can be imported and used across different routes
- **Testability:** Independent modules enable unit and integration testing
- **Scalability:** Modular design allows easy feature additions

---

## 2. Technology Stack

### 2.1 Backend Framework
- **Flask 2.x:** Lightweight Python web framework
- **Python 3.9+:** Core programming language

### 2.2 Cloud Services
- **Azure Blob Storage:** Cloud storage for vendor files and assets
- **Azure Storage SDK:** Python library for blob operations

### 2.3 File Processing
- **openpyxl:** Excel file generation and manipulation
- **Werkzeug:** Secure filename handling and file uploads

### 2.4 Frontend
- **Bootstrap 5.3:** Responsive UI framework
- **Vanilla JavaScript:** Form interactions and dynamic content

### 2.5 Data Management
- **Flask Sessions:** User session management
- **JSON:** Manifest and metadata storage format

### 2.6 Development Tools
- **python-dotenv:** Environment variable management
- **hashlib:** File integrity verification (SHA-256)

---

## 3. Application Structure

### 3.1 Directory Layout

```
app/
│
├── app.py                          # Main Flask application
│
├── helpers/
│   ├── __init__.py
│   └── lookups.py                  # Constants and controlled vocabularies
│
├── services/
│   ├── __init__.py
│   ├── file_service.py             # File handling operations
│   ├── azure_service.py            # Azure Blob Storage operations
│   └── excel_service.py            # Excel generation
│
├── validators/
│   ├── __init__.py
│   └── pricing_validator.py       # Form validation logic
│
├── templates/
│   ├── uploads.html                # Bulk upload interface
│   └── single_product.html         # Single product form
│
├── static/
│   └── fgi_logo.png                # Company branding
│
├── uploads/                        # Local temporary storage
│   ├── [vendor_name]/
│   │   ├── opticat/               # OptiCat vendor files
│   │   ├── non_opticat/           # Non-OptiCat vendor files
│   │   └── manual_assets/         # Single product assets
│   └── single_product_batches/    # Generated Excel files
│
├── data/
│   └── templates/
│       └── standard_template.xlsx  # Downloadable template
│
├── .env                            # Environment variables
└── requirements.txt                # Python dependencies
```

### 3.2 Module Breakdown

#### **app.py** (Main Application)
- Flask initialization and configuration
- Route definitions
- Request handling and response generation
- Session management
- Orchestration of services

#### **helpers/lookups.py**
- Centralized constants and controlled vocabularies
- Vendor lists (OptiCat and Non-OptiCat)
- Units of measure (UOM)
- Pricing methods
- Product statuses and flags
- Change types (Add, Modify, Delete)

#### **services/file_service.py**
- File validation (allowed extensions)
- Secure file saving with vendor-specific paths
- SHA-256 hash computation for change detection

#### **services/azure_service.py**
- Blob service client management
- File upload to Azure Blob Storage
- Asset hash comparison for smart uploads
- Old asset ZIP cleanup
- Manifest generation and storage
- Separate handlers for OptiCat and Non-OptiCat workflows

#### **services/excel_service.py**
- Multi-sheet Excel workbook generation
- Eight standardized tabs:
  - Item_Master
  - Descriptions
  - Extended_Info
  - Attributes
  - Part_Interchange
  - Packages
  - Digital_Assets
  - Pricing

#### **validators/pricing_validator.py**
- Comprehensive form validation
- Field-level validation (SKU, UNSPSC, barcodes)
- Multi-level pricing validation
- Method-specific validation logic
- Date validation and business rules

---

## 4. Module Documentation

### 4.1 Helper Module: lookups.py

**Purpose:** Centralized repository for all constants and controlled vocabularies.

**Key Constants:**

```python
# Vendor Classification
VENDOR_LIST = [
    # OptiCat vendors (XML + Pricing XLSX)
    "Dayton Parts", "Grote Lighting", "Neapco",
    "Truck Lite", "Baldwin Filters", "Stemco", 
    "High Bar Brands",
    
    # Non-OptiCat vendors (Unified XLSX)
    "Ride Air", "Tetran", "SAF Holland",
    "Consolidated Metco", "Tiger Tool", 
    "J.W Speaker", "Rigid Industries"
]

# Change Management
CHANGE_TYPES = ["A", "M", "D"]  # Add, Modify, Delete

# Units of Measure
QUANTITY_UOM = ["EA", "PC", "BOX", "CS", "PK", 
                "SET", "RL", "BG", "BT", "DZ"]
WEIGHT_UOM = ["LB", "KG", "G", "OZ"]
DIMENSION_UOM = ["IN", "CM", "MM"]

# Product Management
PRODUCT_STATUS = ["Active", "Inactive", "Obsolete"]

# Pricing Methods
PRICING_METHODS = {
    "net_cost": "Net Cost Provided",
    "price_levels": "Price Levels Provided",
    "discount_based": "Discount-Based Pricing",
    "ehc_based": "EHC-Based Pricing",
    "promo_pricing": "Promo Pricing",
    "quote_pricing": "Quote Pricing",
    "tender_pricing": "Tender Pricing",
    "core_pricing": "Core Pricing"
}
```

**Benefits:**
- Single source of truth for business rules
- Easy to maintain and update
- Prevents hardcoded values throughout codebase
- Enables consistent validation across the application

---

### 4.2 Service Module: file_service.py

**Purpose:** Handle all file-related operations with security and validation.

**Key Functions:**

#### `allowed_file(filename) -> bool`
Validates file extensions against allowed list.

**Parameters:**
- `filename` (str): Name of the uploaded file

**Returns:**
- `bool`: True if extension is allowed (.xml, .xlsx)

**Example:**
```python
if allowed_file(request.files['product_file'].filename):
    # Process file
```

#### `save_file(file, vendor_name, subfolder, upload_folder) -> str`
Securely saves uploaded files to vendor-specific directories.

**Parameters:**
- `file`: Flask FileStorage object
- `vendor_name` (str): Vendor identifier
- `subfolder` (str): Category (opticat, non_opticat, manual_assets)
- `upload_folder` (str): Base upload directory

**Returns:**
- `str`: Full filepath where file was saved

**Directory Structure Created:**
```
uploads/
└── [vendor_name]/
    ├── opticat/
    │   ├── product.xml
    │   ├── pricing.xlsx
    │   └── manifest_2025-11-19_14-30-00.json
    ├── non_opticat/
    │   ├── unified.xlsx
    │   └── manifest_2025-11-19_14-30-00.json
    └── manual_assets/
        └── [sku]/
            ├── image1.jpg
            └── spec.pdf
```

#### `compute_file_hash(filepath) -> str`
Generates SHA-256 hash for file integrity and change detection.

**Parameters:**
- `filepath` (str): Path to file

**Returns:**
- `str`: Hexadecimal hash string

**Use Case:**
```python
new_hash = compute_file_hash(zip_path)
last_hash = get_latest_asset_hash(container_client, vendor_folder)

if new_hash == last_hash:
    print("Assets unchanged. Skipping upload.")
else:
    print("Assets changed. Uploading new ZIP...")
```

---

### 4.3 Service Module: azure_service.py

**Purpose:** Manage all Azure Blob Storage operations.

**Key Functions:**

#### `get_blob_service_client(connection_string) -> BlobServiceClient`
Creates authenticated Azure Blob Service client.

**Error Handling:**
- Raises `RuntimeError` if connection string is not configured

#### `upload_blob(local_path, blob_path, connection_string, container_name) -> str`
Uploads a single file to Azure Blob Storage.

**Parameters:**
- `local_path` (str): Local file path
- `blob_path` (str): Destination path in Azure
- `connection_string` (str): Azure connection string
- `container_name` (str): Target container (bronze)

**Returns:**
- `str`: Full blob path (container/blob_path)

#### `upload_to_azure_bronze_opticat(vendor, xml_local_path, pricing_local_path, zip_path, ...)`
Comprehensive upload handler for OptiCat vendors.

**Workflow:**
1. Upload product XML to `raw/vendor=X/product/timestamp_product.xml`
2. Upload pricing XLSX to `raw/vendor=X/pricing/timestamp_pricing.xlsx`
3. Check asset ZIP hash against last upload
4. Skip upload if hash unchanged (bandwidth optimization)
5. Upload new ZIP if changed to `raw/vendor=X/assets/timestamp_assets.zip`
6. Delete old asset ZIPs (keep only latest)
7. Generate manifest JSON with metadata
8. Upload manifest to `raw/vendor=X/logs/manifest_timestamp.json`

**Manifest Structure:**
```json
{
  "vendor": "Grote Lighting",
  "timestamp": "2025-11-19_14-30-00",
  "azure_xml_blob": "bronze/raw/vendor=Grote Lighting/product/2025-11-19_14-30-00_product.xml",
  "azure_pricing_blob": "bronze/raw/vendor=Grote Lighting/pricing/2025-11-19_14-30-00_pricing.xlsx",
  "azure_assets_blob": "bronze/raw/vendor=Grote Lighting/assets/2025-11-19_14-30-00_assets.zip",
  "assets_hash": "a3f5b8c9d2e4f7a1b3c5d7e9f1a3b5c7d9e1f3a5b7c9d1e3f5a7b9c1d3e5f7a9"
}
```

#### `upload_to_azure_bronze_non_opticat(vendor, unified_local_path, zip_path, ...)`
Similar workflow for Non-OptiCat vendors with unified XLSX format.

**Azure Blob Structure:**
```
bronze/
└── raw/
    └── vendor=[VendorName]/
        ├── product/              # OptiCat only
        │   └── timestamp_product.xml
        ├── pricing/              # OptiCat only
        │   └── timestamp_pricing.xlsx
        ├── unified/              # Non-OptiCat only
        │   └── timestamp_unified.xlsx
        ├── assets/
        │   └── timestamp_assets.zip (latest only)
        └── logs/
            └── manifest_timestamp.json
```

---

### 4.4 Service Module: excel_service.py

**Purpose:** Generate standardized Excel workbooks from single product submissions.

**Key Function:**

#### `create_multi_product_excel(item_rows, desc_rows, ext_rows, ...) -> str`

Generates a multi-sheet Excel workbook with batch product data.

**Parameters:**
- `item_rows` (list): Item master data
- `desc_rows` (list): Descriptions
- `ext_rows` (list): Extended info
- `attr_rows` (list): Attributes
- `interchange_rows` (list): Part interchange
- `package_rows` (list): Package dimensions
- `asset_rows` (list): Digital assets
- `price_rows` (list): Pricing levels
- `output_dir` (str): Output directory

**Returns:**
- `str`: Full path to generated Excel file

**Generated Sheets:**

1. **Item_Master:** Core product information
   - Vendor, Part Number, UNSPSC, Hazmat Flag
   - Product Status, Barcode Type/Number
   - Quantity UOM/Size, VMRS Code

2. **Descriptions:** Product descriptions
   - SKU, Change Type, Description Code
   - Description Value, Sequence, Language

3. **Extended_Info:** Additional product details
   - SKU, Change Type, Info Code
   - Info Value (COO, HSB, TAX, etc.)

4. **Attributes:** Product specifications
   - SKU, Change Type, Attribute Name
   - Attribute Value, Attribute Unit

5. **Part_Interchange:** Cross-reference data
   - SKU, Change Type, Brand Label
   - Part Number, Interchange Quality

6. **Packages:** Packaging dimensions
   - SKU, Package UOM, Quantity of Eaches
   - Weight (Value + UOM)
   - Dimensions (Merch/Ship Length/Width/Height)
   - Dimension UOM

7. **Digital_Assets:** Media files
   - SKU, Change Type, Media Type
   - FileName, FileLocalPath

8. **Pricing:** Multi-level pricing
   - Vendor, Part Number, Pricing Method
   - Currency, MOQ (Unit + Quantity)
   - Level Type (Each/Case/Pallet/Bulk)
   - List/Jobber/Net Prices
   - Discount %, Effective/Start/End Dates
   - EHC fees by province (AB/MB/SK, BC, NL, NS, NB/QC, PEI, YK)
   - Tier quantities, Core pricing, Notes

**Usage Example:**
```python
excel_path = create_multi_product_excel(
    item_rows=session.get("batch_item_rows", []),
    desc_rows=session.get("batch_desc_rows", []),
    ext_rows=session.get("batch_ext_rows", []),
    attr_rows=session.get("batch_attr_rows", []),
    interchange_rows=session.get("batch_interchange_rows", []),
    package_rows=session.get("batch_package_rows", []),
    asset_rows=session.get("batch_asset_rows", []),
    price_rows=session.get("batch_price_rows", []),
    output_dir=os.path.join(UPLOAD_FOLDER, "single_product_batches")
)
```

---

### 4.5 Validator Module: pricing_validator.py

**Purpose:** Comprehensive validation of single product form submissions.

**Key Function:**

#### `validate_single_product_new(form) -> tuple[bool, list[str]]`

Validates all sections of the single product form.

**Parameters:**
- `form` (dict): Flask request.form containing all form data

**Returns:**
- `tuple`: (is_valid: bool, errors: list[str])

**Validation Rules:**

**1. Item Master Validation:**
- Vendor: Required, must be in VENDOR_LIST
- Part Number (SKU): Required
- UNSPSC: If provided, must be exactly 8 numeric digits
- Hazmat Flag: Must be Y or N
- Product Status: Must be in PRODUCT_STATUS list
- Barcode Type: Must be UPC or EAN
- Barcode Number: Must be numeric
  - UPC: Exactly 12 digits
  - EAN: Exactly 14 digits
- Quantity UOM: Must be in QUANTITY_UOM list
- Quantity Size: Must be numeric if provided

**2. Description Validation:**
- DES/SHO codes: Maximum 40 characters

**3. Multi-Level Pricing Validation:**

For each pricing level:
- Level Type: Required (Each/Case/Pallet/Bulk)
- Pricing Method: Required, must be valid method
- Currency: Required (CAD/USD)
- MOQ: Must be numeric if provided

**Method-Specific Validation:**

**Net Cost:**
- List Price: Required, numeric
- Net Cost: Required, numeric
- Effective Date: Required, cannot be before today

**Price Levels:**
- List Price: Required, numeric
- Jobber Price: Required, numeric
- Net Cost: Required, numeric
- Effective Date: Required, cannot be before today

**Discount-Based:**
- Base Price: Required, numeric
- Discount %: Required, numeric, between 0.0 and 1.0
- Effective Date: Required, cannot be before today

**EHC-Based:**
- Base Price: Required, numeric
- All provincial fees: Numeric if provided
- Packaging details: Numeric if provided

**Promo/Quote/Tender:**
- Price: Required, numeric
- Start Date: Required
- End Date: Required
- End Date must be after Start Date

**Example Error Messages:**
```python
errors = [
    "Vendor is required.",
    "UNSPSC Code must be exactly 8 numeric digits if provided.",
    "UPC Barcode must be exactly 12 digits.",
    "Pricing Level 1 (Net Cost): Effective Date is required.",
    "Pricing Level 2 (Discount): Discount % must be between 0.0 and 1.0."
]
```

---

## 5. Data Flow

### 5.1 Bulk Upload Flow (OptiCat)

```
┌────────────────┐
│  Vendor Portal │
│  (uploads.html)│
└───────┬────────┘
        │
        │ 1. Vendor selects OptiCat vendor
        │ 2. Uploads XML + XLSX + ZIP path
        │
        ▼
┌────────────────────┐
│   app.py           │
│   /upload (POST)   │
└────────┬───────────┘
         │
         │ 3. Save files locally
         │    - save_file() for XML
         │    - save_file() for XLSX
         │
         ▼
┌─────────────────────────────┐
│  azure_service.py           │
│  upload_to_azure_bronze_    │
│  opticat()                  │
└────────┬────────────────────┘
         │
         │ 4. Upload XML → Azure
         │ 5. Upload XLSX → Azure
         │ 6. Check ZIP hash
         │ 7. Upload ZIP if changed
         │ 8. Delete old ZIPs
         │ 9. Create manifest
         │ 10. Upload manifest
         │
         ▼
┌─────────────────────┐
│  Azure Blob Storage │
│  (Bronze Container) │
└─────────────────────┘
```

### 5.2 Bulk Upload Flow (Non-OptiCat)

```
┌────────────────┐
│  Vendor Portal │
└───────┬────────┘
        │
        │ 1. Vendor selects Non-OptiCat vendor
        │ 2. Uploads Unified XLSX + ZIP path
        │
        ▼
┌────────────────────┐
│   app.py           │
└────────┬───────────┘
         │
         │ 3. Save unified XLSX locally
         │
         ▼
┌─────────────────────────────┐
│  azure_service.py           │
│  upload_to_azure_bronze_    │
│  non_opticat()              │
└────────┬────────────────────┘
         │
         │ 4. Upload unified XLSX → Azure
         │ 5. Check ZIP hash
         │ 6. Upload ZIP if changed
         │ 7. Delete old ZIPs
         │ 8. Create manifest
         │ 9. Upload manifest
         │
         ▼
┌─────────────────────┐
│  Azure Blob Storage │
└─────────────────────┘
```

### 5.3 Single Product Flow

```
┌────────────────────┐
│  Single Product    │
│  Form              │
│  (single_product.  │
│   html)            │
└────────┬───────────┘
         │
         │ 1. User fills 8-section form
         │    - Item Master
         │    - Descriptions
         │    - Extended Info
         │    - Attributes
         │    - Part Interchange
         │    - Packages
         │    - Digital Assets
         │    - Pricing (multi-level)
         │
         ▼
┌──────────────────────┐
│  pricing_validator.py│
│  validate_single_    │
│  product_new()       │
└────────┬─────────────┘
         │
         │ 2. Validate all fields
         │ 3. Return errors if any
         │
         ▼
┌──────────────────────┐
│  app.py              │
│  submit_single_      │
│  product()           │
└────────┬─────────────┘
         │
         │ 4. Parse form data
         │ 5. Store in session
         │    - batch_item_rows
         │    - batch_desc_rows
         │    - batch_ext_rows
         │    - batch_attr_rows
         │    - batch_interchange_rows
         │    - batch_package_rows
         │    - batch_asset_rows
         │    - batch_price_rows
         │
         │ 6. Action: "add" or "generate"?
         │
         ├─────────────┬──────────────┐
         │ ADD         │ GENERATE     │
         │             │              │
         ▼             ▼              │
   ┌─────────┐  ┌──────────────┐    │
   │ Redirect│  │ excel_service│    │
   │ to form │  │ .py          │    │
   │ (add    │  │              │    │
   │ more)   │  │ create_multi_│    │
   └─────────┘  │ product_     │    │
                │ excel()      │    │
                └──────┬───────┘    │
                       │            │
                       │ 7. Generate│
                       │    Excel   │
                       │    with 8  │
                       │    sheets  │
                       │            │
                       │ 8. Clear   │
                       │    session │
                       │            │
                       ▼            │
                ┌──────────────┐   │
                │ Download     │   │
                │ Excel File   │   │
                └──────────────┘   │
                                   │
```

---

## 6. Key Features

### 6.1 Dual Vendor Support

**OptiCat Vendors:**
- XML product file (standardized OptiCat format)
- XLSX pricing file
- Optional asset ZIP

**Non-OptiCat Vendors:**
- Single unified XLSX file (all data)
- Optional asset ZIP

**Implementation:**
```javascript
// uploads.html - Dynamic form switching
document.getElementById('vendorSelect').addEventListener('change', function () {
    const type = this.options[this.selectedIndex].getAttribute('data-type');
    
    if (type === 'opticat') {
        opticatSection.style.display = 'block';
        nonOpticatSection.style.display = 'none';
    } else {
        opticatSection.style.display = 'none';
        nonOpticatSection.style.display = 'block';
    }
});
```

### 6.2 Intelligent Asset Management

**Problem:** Large asset ZIPs waste bandwidth if unchanged.

**Solution:** Hash-based change detection
1. Compute SHA-256 hash of new ZIP
2. Retrieve hash from last uploaded manifest
3. Compare hashes
4. Skip upload if identical
5. Upload and cleanup if different

**Benefits:**
- Reduced bandwidth usage
- Faster processing
- Storage optimization (only latest ZIP kept)

**Code:**
```python
new_hash = compute_file_hash(zip_path)
last_hash = get_latest_asset_hash(container_client, vendor_folder)

if last_hash == new_hash:
    print("🟡 Assets unchanged. Skipping ZIP upload.")
else:
    print("🟢 Assets changed. Uploading new ZIP...")
    upload_blob(zip_path, assets_blob_path, ...)
    delete_old_asset_zips(container_client, vendor_folder)
```

### 6.3 Multi-Level Pricing Support

Supports 8 pricing methods with method-specific fields:

1. **Net Cost:** List + Net prices with effective date
2. **Price Levels:** List, Jobber, Net prices
3. **Discount-Based:** Base price + discount percentage
4. **EHC-Based:** Provincial environmental handling charges
5. **Promo Pricing:** Promotional prices with date range
6. **Quote Pricing:** Customer-specific quotes
7. **Tender Pricing:** Tender-based pricing
8. **Core Pricing:** Core charge for returnable items

**Dynamic Form:**
```javascript
// single_product.html
methodSelect.addEventListener('change', () => {
    const val = methodSelect.value;
    methodSections.forEach(sec => sec.classList.add('d-none'));
    
    const target = block.querySelector('.method-' + val);
    if (target) target.classList.remove('d-none');
});
```

### 6.4 Batch Product Entry

**Workflow:**
1. User enters first product → Click "Add to Batch"
2. Data stored in Flask session
3. Form resets, vendor pre-filled
4. User enters second product → Click "Add to Batch"
5. Repeat as needed
6. Click "Generate Excel" → Creates consolidated workbook
7. Session cleared, ready for new batch

**Session Management:**
```python
session['batch_item_rows'] = item_rows
session['batch_desc_rows'] = desc_rows
session['batch_ext_rows'] = ext_rows
# ... 8 total arrays
session['pending_products'] = pending  # Summary for UI table
```

### 6.5 Comprehensive Validation

**Multi-layer validation:**
- Client-side: HTML5 required fields
- Server-side: Python validation with detailed error messages
- Business rules: UPC/EAN length, UNSPSC format, date logic
- Method-specific: Pricing-specific field requirements

**Error Display:**
```html
{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    {% for category, message in messages %}
      <div class="alert alert-{{ category }}">
        {{ message }}
      </div>
    {% endfor %}
  {% endif %}
{% endwith %}
```

### 6.6 Manifest Tracking

Every upload generates a manifest for auditability:

```json
{
  "vendor": "Grote Lighting",
  "timestamp": "2025-11-19_14-30-00",
  "azure_xml_blob": "bronze/raw/vendor=Grote/product/...",
  "azure_pricing_blob": "bronze/raw/vendor=Grote/pricing/...",
  "azure_assets_blob": "bronze/raw/vendor=Grote/assets/...",
  "assets_hash": "a3f5b8c9..."
}
```

**Use Cases:**
- Track upload history
- Verify asset changes
- Audit trail
- Rollback support (future)

---

## 7. Azure Integration

### 7.1 Connection Setup

**Environment Variable:**
```bash
AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net"
```

**Initialization:**
```python
from dotenv import load_dotenv
load_dotenv()

AZURE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
AZURE_CONTAINER_NAME = "bronze"
```

### 7.2 Bronze Layer Structure

All vendor files land in the **Bronze** container:

```
bronze/
└── raw/
    ├── vendor=Dayton Parts/
    │   ├── product/
    │   │   ├── 2025-11-19_14-30-00_product.xml
    │   │   └── 2025-11-20_09-15-00_product.xml
    │   ├── pricing/
    │   │   ├── 2025-11-19_14-30-00_pricing.xlsx
    │   │   └── 2025-11-20_09-15-00_pricing.xlsx
    │   ├── assets/
    │   │   └── 2025-11-20_09-15-00_assets.zip  # Latest only
    │   └── logs/
    │       ├── manifest_2025-11-19_14-30-00.json
    │       └── manifest_2025-11-20_09-15-00.json
    │
    ├── vendor=Grote Lighting/
    │   ├── product/
    │   ├── pricing/
    │   ├── assets/
    │   └── logs/
    │
    ├── vendor=Tiger Tool/  # Non-OptiCat
    │   ├── unified/
    │   │   ├── 2025-11-19_14-30-00_unified.xlsx
    │   │   └── 2025-11-20_09-15-00_unified.xlsx
    │   ├── assets/
    │   │   └── 2025-11-20_09-15-00_assets.zip
    │   └── logs/
    │       └── manifest_2025-11-20_09-15-00.json
    │
    └── vendor=SAF Holland/  # Non-OptiCat
        ├── unified/
        ├── assets/
        └── logs/
```

### 7.3 Upload Strategies
**Always Upload:**
- Product XML files (Opticat)
- Product XLSX files (Non-opticat)
- Unified XLSX files (Non-opticat)
- Manifests (every upload creates new emanifest)
**Conditional UPload:**
- Asset ZIPs (only if hash changed)
**Automatic Cleanup**
- Old asset ZIPs (keep only latest)
**Upload Flow Logic:**
For every upload
1. Save files locally
2. Upload product/pricing/unified to Azure (always)
3. Check if asset ZIP is provided
4. IF ZIP exists:
    a. Compute new hash
    b. Get last uploaded hash from manifest
    c. Compare hashes
    d. If diiffent:
    - Upload new ZIP
    - Detlete old ZIPs
    e. IF same:
    - Skip Upload
5. Create manifest with meta data
6. Upload manifest to Azure

### Error Handling
Connection Errors:
```python
    try:
        upload_to_azure_bronze_opticat (
            vendor_name,
            product_path,
            zip_path,
            AZURE_CONNECTION_STRING,
            AZURE_CONTAINER_NAME,
            app.config['Upload Folder']
        )
        flash(f'Files for {vendor_name} uploaded successfully.', 'success')
    except Exception as e:
        print(f"X Azure upload failed: {e}")
        flash(f'Azure upload failed: {e}', 'danger')
        # Files remain in local storage for retry
```
### Network Timeout
```python
 # Configure timeout in azure_service.py
blob_client = container.client.get_blob_client(blob_path)
blob_client.upload_blob(
    data, overwrite = True, timeout = 300
)
# 5 minutes
```

### Monnitoring Azure Operations
**Sucess Indicators**

- Uploaded to Azure: raw/vendor=Grote/product/2025-11-19_14-30-00_product.xml
- Uploaded to Azure: raw/vendor=Grote/pricing/2025-11-19_14-30-00_pricing.xlsx
- Assets changed. Uploading new zip . . .
- Uploaded to Azure: raw/vendor=Grote/assets/2025-11-19_14-30-00_assets.zip
[Cleanup] Deleted old asset ZIP: raw/vendor=Grote/assets/2025-11-18_10-00-00_assets.zip
Manifests created and uploaded: raw/vendor=Grote/logs/manifest_2025-11-19_14-30-00.json

**Skip Indicators**
Assets unchanged. Skipping ZIP upload.
No ZIP path provided or file does not exist. Skipping assets upload. 

## 8. Security Considerations
**Secure Filename Handling**
Risk: Path traversal attacks, malicious filenames
Solution: Werkzeug's secure_filename()
```python
from werkzeug.utils import secure_filename
#Dangerous filename
dangerous = "../../etc/passwd"
safe = secure_filename(dangerous)
#Result: "etc_passwd"

#Unicode characters
unicode_name = "Прайс-лист.xlsx"
safe = secure_filename(unicode_name)
#Result: "-.xlsx" (non-ASCII removed)

#Implementation
filename = secure_filename(file.filename)
filepath = os.path.join(vendor_folder, filename)
file.save(filepath)

```
What secure_filename() does:
1. Remove path separators (/)
2. Remove leading/trailing dots
3. ASCII normalizes Unicode characters
4. Replaces spaces with underscores
5. Removes special characters

### File Type Validation
Whitelist Approach:
```python
ALLOWED_EXTENSIONS = {'xml','xlsx'}
def allowed_file(filename):
    """
    Only allow specific file extensions. 
    Prevents execution of malicious scripts.
    """
    return '.' in filename and \ 
        filename.rsplit ('.',1) [1].lower() in ALLOWED_EXTENSIONS

#Usage in routes
if not allowed_file(product_file.filename):
    flash('Invalid file type. Only XML and XLSX allowed. ', 'danger')
    return redirect (url for ('upload_page'))
```
Why not blacklist?
- Blacklists are incomplete (too many dangerous extensions)
- Easy to bypass (.php5, phtml,etc.)
- Whitelists are safer and explicit

### Azure Credentials Management
#### Environment Variables
**Development Setup**
```python
# .env file (never commit to Git)
AZURE_STORAGE_CONNECTION_STRING="DefalutEndpointsProtocol=https;AccountName=fgivendordata;AccountKey=XXXXX;EndpointSuffix=core.windows.net"
SECRET_KEY = "your-secret-key-here"
FLASK_ENV = "development"
```
**Loading Environment Variables:**
```python 
from dotenv import load_dotenv
import os
load_dotenv() # Load .env file
AZURE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
if not AZURE_CONNECTION_STRING:
    print("Warning: AZURE_CONNECTION_STRING is not set.")
    print("Azure uploads will fail until you configure it. ")
```




