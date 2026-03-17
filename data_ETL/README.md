📦 Data ETL – Milestone 2 (ETL & Review)
Overview

This module implements the Data ETL pipeline for vendor product data at FGI, transforming vendor-submitted structured data into review-ready, canonical Silver outputs.

The pipeline is identity-first, non-destructive, and diagnostic by design, ensuring data quality issues are identified, normalized safely, and structurally assembled without premature rejection.

Assets (media) are handled separately and joined via the same identity contract.

🎯 Objectives

Establish a single, authoritative product identity

Identify data quality issues without rejecting data

Apply safe, deterministic autofixes

Assemble canonical product datasets

Perform cross-entity integrity checks

Prepare data for promotion to Approved / downstream systems

🧠 Identity Contract (Critical)

All data entities are joined using:

_entity_id = sha256(<vendor> | <Part Number>)


Rules:

_entity_id is authoritative

Defined once in data_profiling

Never modified downstream

Used consistently across:

data

pricing

attributes

media

integrity checks

Part Number is treated as business metadata, not a join key.

🧱 Pipeline Stages
1️⃣ data_profiling.py — Identify

Purpose

First touch of structured data

Add lineage + identity

Identify issues (missing values, outliers, normalization candidates)

Reads

silver/in_review/vendor=<vendor>/mapped/mapped.parquet


Writes

silver/in_review/vendor=<vendor>/profiling/
 ├── data_profile.parquet
 ├── data_issues.parquet
 ├── data_profile.xlsx
 └── profile_summary.json


Key guarantees

_entity_id is always overwritten authoritatively

_row_id is deterministic

No rows dropped

No validation or rejection

2️⃣ data_autofix.py — Normalize (Safely)

Purpose

Apply only safe, deterministic fixes

Operates strictly on allowlisted issues

Fix examples

Currency normalization

String normalization (Brand, Category)

Reads

profiling/data_profile.parquet
profiling/data_issues.parquet


Writes

silver/in_review/vendor=<vendor>/autofix/
 ├── data_autofixed.parquet
 ├── issues_resolved.parquet
 ├── issues_remaining.parquet
 ├── autofix_report.xlsx
 └── autofix_summary.json


Design rules

Never modifies _entity_id or lineage

No row deletion

No business enforcement

3️⃣ data_canonicalize.py — Assemble

Purpose

Convert row-level data into product-level canonical structures

Reads

autofix/data_autofixed.parquet


Writes

silver/in_review/vendor=<vendor>/canonical/
 ├── item_master_canonical.parquet
 ├── pricing_canonical.parquet
 ├── attributes_canonical.parquet
 ├── data_canonical.xlsx
 └── canonical_summary.json


Canonical rules

Product collapse is done only here

One row per _entity_id in item_master

Hard failure if:

_entity_id missing

_entity_id null

product count mismatch

4️⃣ data_integrity_checks.py — Diagnose Cross-Entity Issues

Purpose

Identify inconsistencies across canonical datasets

No mutation, no rejection

Checks

Pricing without item (BLOCKING)

Duplicate UPC (BLOCKING)

Item without pricing (WARNING)

Item without assets (WARNING)

Asset without item (WARNING)

Reads

canonical/item_master_canonical.parquet
canonical/pricing_canonical.parquet
canonical/attributes_canonical.parquet
canonical/media_canonical.parquet (optional)


Writes

silver/in_review/vendor=<vendor>/integrity/
 ├── integrity_issues.parquet
 ├── integrity_report.xlsx
 └── integrity_summary.json


⚠️ Note
“Item without assets” means no assets matched on _entity_id, not necessarily that assets do not exist (e.g., formatting or leading-zero mismatches).

5️⃣ Promotion (Next Step)

After integrity review:

Data is eligible for promotion to Approved

Promotion logic is intentionally separated from ETL

Blocking issues must be resolved before promotion

📂 Folder Structure (Silver)
silver/
└── in_review/
    └── vendor=<vendor>/
        ├── mapped/
        ├── profiling/
        ├── autofix/
        ├── canonical/
        ├── integrity/
        └── approved/   (next step)

🧪 Testing Strategy

Jupyter notebooks used for:

Stage-by-stage validation

Row count reconciliation

Identity verification

Each stage produces:

machine-readable parquet

human-readable Excel

JSON summaries for automation

✅ Design Principles

Identity-first

Deterministic

Non-destructive

Diagnostics over enforcement

Safe defaults

Vendor-agnostic

📌 Status

Data ETL pipeline implemented and validated

Identity drift resolved

Media + data aligned via _entity_id

Ready for promotion and Milestone 2 wrap-up