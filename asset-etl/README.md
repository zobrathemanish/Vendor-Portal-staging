# Image Asset ETL — Silver Review Layer

## Overview

This project implements the **Silver-layer Image Asset ETL** for vendor-submitted product images stored in **Azure Blob Storage**.

The purpose of this ETL is to **validate, reconcile, and summarize image assets** after ingestion and unzip, without applying business interpretation or irreversible transformations.

This ETL produces **audit-ready, receipt-style artifacts** that describe the outcome of each run.

---

## Scope & Responsibilities

### What this ETL does

For each vendor found under:

silver/in_review/vendor=<vendor>/

the pipeline:

1. Discovers vendors dynamically from Azure blob paths  
2. Enumerates all image files under the vendor’s `assets/` folder  
3. Validates image binaries:
   - Supported formats (.jpg, .jpeg, .png, .webp)
   - File size limits
   - Minimum resolution
   - Corrupt image detection
4. Reconciles declared vs delivered assets:
   - Compares filenames declared in mapped product data
   - Identifies missing and extra assets
5. Generates run-level artifacts:
   - Lean manifest (receipt)
   - Validation log (failures only)

---

### What this ETL does NOT do

This pipeline intentionally does NOT:

- Interpret business meaning (primary vs secondary images)
- Perform SKU / Part Number level readiness decisions
- Rename, resize, or transform images
- Block promotion or enforce business rules
- Publish to Gold / downstream systems
- Modify or overwrite original vendor submissions

All of the above belong to later pipeline stages.

---

## Output Artifacts

### 1. Asset Manifest (Receipt)

**Location**
silver/logs/assets/vendor=<vendor>/

**File**
asset_manifest_silver_<timestamp>.json

**Purpose**

Summarizes the factual outcome of the ETL run.

**Example**
```json
{
  "vendor": "Grote Lighting",
  "run_timestamp": "2025-12-29T18:40:00Z",
  "summary": {
    "total_assets": 120,
    "missing_declared": 7,
    "extra_assets": 12,
    "failed": 3
  },
  "missing_assets": [
    "00230.jpg",
    "00240.jpg"
  ],
  "extra_assets": [
    "random_test.jpg"
  ]
}
```

---

### 2. Asset Validation Log

**File**
asset_validation_log_<timestamp>.json

**Purpose**

Records only failed assets and their reasons.

---

## Why a Manifest Is Required

Folders and files alone cannot explain:
- What was expected
- What was missing
- What failed validation
- When the decision was made

The manifest provides a **time-bound, immutable summary** of the ETL run — similar to a receipt.

---

## Design Principles

- Azure Blob Storage is the source of truth
- Manifests record deviations, not raw inputs
- Logs capture problems, not successes
- Silver layer is factual, not interpretive
- Everything must be reproducible

---

## Runtime Requirements

- Python 3.10+
- Azure Storage access
- Required libraries:
  - azure-storage-blob
  - pandas
  - pyarrow
  - Pillow
  - python-dotenv

---

## Configuration

Environment variable required:

AZURE_STORAGE_CONNECTION_STRING

---

## How to Run

python asset_etl_silver.py

The script:
- Discovers vendors automatically
- Processes each vendor independently
- Writes logs under each vendor’s namespace

---

## Versioning

This implementation represents:

**Image Asset ETL — Silver Layer v1**

Minimal, auditable, and intentionally scoped.
