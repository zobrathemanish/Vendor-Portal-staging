"""
Non-OptiCat Schema Matcher MVP
------------------------------
Goal:
- Keep FGI schema static.
- Read a random vendor Excel/CSV.
- Detect available vendor columns.
- Suggest likely mappings into FGI schema using simple Bag-of-Words + synonyms + value patterns.
- Generate a normalized FGI workbook with all target columns present and unmapped fields blank.

Usage:
python non_opticat_schema_matcher.py \
  --schema mapped.xlsx \
  --vendor-file "F6161A_2025-JUL-01 revised.xlsx" \
  --out-dir outputs/non_opticat_test
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from difflib import SequenceMatcher
from typing import Dict, List, Tuple, Any, Optional

import pandas as pd
from openpyxl import Workbook


# Pricing was not present in the uploaded mapped.xlsx sample, but it is part of the FGI pipeline schema.
# If your mapped.xlsx later includes Pricing, this manual fallback will not override it.
DEFAULT_EXTRA_SCHEMA = {
    "Pricing": [
        "Part Number",
        "Pricing Change Type",
        "Currency",
        "Pricing Type",
        "List Price",
        "Jobber Price",
        "Dealer Price",
        "Net Price",
        "Discount %",
        "MOQ Unit",
        "Minimum Order Quantity",
        "POP Code",
        "Effective Date",
        "Notes",
    ]
}

# Strong domain synonyms. This is where we encode FGI/vendor language.
FIELD_SYNONYMS = {
    "Part Number": ["part number", "part no", "part", "sku", "item", "item no", "formatted item", "compressed item", "product id"],
    "Brand Label": ["brand", "brand label", "manufacturer", "mfr", "line"],
    "Description Value": ["description", "item description", "product description", "desc", "short description", "long description", "marketing copy"],
    "Description Code": ["description code", "desc code"],
    "Currency": ["currency", "curr"],
    "List Price": ["list price", "list", "msrp"],
    "Dealer Price": ["dealer price", "distributor net", "distributor price", "dealer net"],
    "Net Price": ["net price", "customer unit price", "customer price", "unit price", "cost"],
    "Jobber Price": ["jobber", "jobber price"],
    "POP Code": ["pop code", "pop"],
    "Minimum Order Quantity": ["minimum order quantity", "moq", "qty break", "quantity break"],
    "MOQ Unit": ["moq unit", "minimum order quantity unit", "unit"],
    "Package Quantity of Eaches": ["unit pack", "pack qty", "package qty", "quantity of eaches", "case qty"],
    "Weight": ["weight", "wt", "gross weight", "package weight"],
    "Weight UOM": ["weight uom", "weight unit", "wt uom"],
    "Barcode Number": ["upc", "upc code", "barcode", "barcode number", "gtin", "ean"],
    "Barcode Type": ["barcode type", "upc type", "gtin type"],
    "Product Status": ["status", "product status", "active", "eligible for return"],
    "Extended Info Value": ["country", "country of origin", "coo", "origin", "superseding part", "supersedes", "core charge", "core part", "core group"],
    "Extended Info Code": ["extended info code", "info code"],
    "Attribute Name": ["attribute", "attribute name", "feature", "spec name"],
    "Attribute Value": ["attribute value", "feature value", "spec value"],
    "Package UOM": ["package uom", "package unit", "uom", "unit of measure"],
    "Dimension UOM": ["dimension uom", "dimension unit", "dim uom"],
    "Merch Length": ["merch length", "length", "item length"],
    "Merch Width": ["merch width", "width", "item width"],
    "Merch Height": ["merch height", "height", "item height"],
    "Ship Length": ["ship length", "shipping length"],
    "Ship Width": ["ship width", "shipping width"],
    "Ship Height": ["ship height", "shipping height"],
    "FileName": ["filename", "file name", "image", "image name", "asset", "photo"],
    "FilePath": ["filepath", "file path", "url", "image url", "asset url", "photo url"],
    "MediaType": ["media type", "asset type", "image type"],
    "FileType": ["file type", "extension", "mime"],
}

# Some vendor columns should map to a specific section when the same target field exists in multiple sheets.
PREFERRED_SECTION_BY_VENDOR_COL = {
    "formatted item": "Item_Master",
    "compressed item": "Item_Master",
    "item description": "Descriptions",
    "brand": "Item_Master",
    "upc code": "Item_Master",
    "list price": "Pricing",
    "distributor net": "Pricing",
    "customer unit price": "Pricing",
    "currency": "Pricing",
    "unit pack": "Packages",
    "weight": "Packages",
    "pop code": "Pricing",
    "superseding part": "Extended_Info",
    "eligible for return": "Item_Master",
}

CONSTANT_DEFAULTS = {
    "Descriptions": {
        "Description Change Type": "A",
        "Description Code": "DES",
        "Sequence": 1,
    },
    "Extended_Info": {
        "Extended Info Change Type": "A",
    },
    "Attributes": {
        "Attribute Change Type": "A",
    },
    "Packages": {
        "Package Change Type": "A",
        "Package UOM": "EA",
        "Weight UOM": "LB",
    },
    "Pricing": {
        "Pricing Change Type": "A",
        "Pricing Type": "Standard",
    },
}

PART_NUMBER_FIELDS = {"Part Number"}


def normalize_text(value: Any) -> str:
    value = "" if value is None else str(value)
    value = value.replace("_", " ").replace("-", " ").replace("/", " ")
    value = re.sub(r"[^a-zA-Z0-9 ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def tokens(value: Any) -> set:
    return set(normalize_text(value).split())


def token_similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    seq = SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()
    return max(jaccard, seq * 0.85)


def read_schema(schema_path: Path) -> Dict[str, List[str]]:
    xl = pd.ExcelFile(schema_path)
    schema: Dict[str, List[str]] = {}
    for sheet in xl.sheet_names:
        df = pd.read_excel(schema_path, sheet_name=sheet, nrows=1)
        cols = [str(c).strip() for c in df.columns if str(c).strip() and not str(c).startswith("Unnamed")]
        if cols:
            schema[sheet] = cols

    for sheet, cols in DEFAULT_EXTRA_SCHEMA.items():
        if sheet not in schema:
            schema[sheet] = cols
    return schema


def read_vendor_file(vendor_path: Path) -> Dict[str, pd.DataFrame]:
    suffix = vendor_path.suffix.lower()
    if suffix in [".xlsx", ".xlsm", ".xls"]:
        xl = pd.ExcelFile(vendor_path)
        result = {}
        for sheet in xl.sheet_names:
            df = pd.read_excel(vendor_path, sheet_name=sheet)
            df = df.dropna(how="all")
            if len(df.columns) > 0 and not df.empty:
                result[sheet] = df
        return result
    if suffix == ".csv":
        return {vendor_path.stem: pd.read_csv(vendor_path)}
    raise ValueError(f"Unsupported file type: {suffix}")


def detect_value_pattern(series: pd.Series) -> List[str]:
    clean = series.dropna().astype(str).str.strip()
    clean = clean[clean != ""]
    sample = clean.head(100)
    patterns = []
    if sample.empty:
        return patterns

    digit_ratio = sample.str.fullmatch(r"\d+").mean()
    barcode_len_ratio = sample.str.fullmatch(r"\d{8}|\d{12}|\d{13}|\d{14}").mean()
    url_file_ratio = sample.str.contains(r"https?://|\.jpg|\.jpeg|\.png|\.webp|\.pdf", case=False, regex=True).mean()
    currency_ratio = sample.str.fullmatch(r"[A-Z]{3}").mean()
    yes_no_ratio = sample.str.upper().isin(["Y", "N", "YES", "NO", "TRUE", "FALSE"]).mean()
    numeric_ratio = pd.to_numeric(sample, errors="coerce").notna().mean()
    long_text_ratio = sample.str.len().gt(30).mean()

    if barcode_len_ratio >= 0.6:
        patterns.append("barcode_candidate")
    if url_file_ratio >= 0.4:
        patterns.append("asset_candidate")
    if currency_ratio >= 0.6:
        patterns.append("currency_candidate")
    if yes_no_ratio >= 0.7:
        patterns.append("flag_candidate")
    if numeric_ratio >= 0.8 and digit_ratio < 0.9:
        patterns.append("numeric_measure_or_price_candidate")
    if long_text_ratio >= 0.5:
        patterns.append("description_candidate")
    return patterns


def build_target_fields(schema: Dict[str, List[str]]) -> List[Dict[str, str]]:
    targets = []
    for section, cols in schema.items():
        for field in cols:
            targets.append({"section": section, "field": field, "key": f"{section}.{field}"})
    return targets


def score_vendor_to_target(vendor_col: str, target_section: str, target_field: str, patterns: List[str]) -> float:
    vendor_norm = normalize_text(vendor_col)
    field_norm = normalize_text(target_field)

    score = token_similarity(vendor_col, target_field)

    # Synonym boost
    for syn in FIELD_SYNONYMS.get(target_field, []):
        syn_score = token_similarity(vendor_norm, syn)
        if syn_score > score:
            score = syn_score
        if vendor_norm == normalize_text(syn):
            score = max(score, 0.98)

    # Pattern boosts
    if "barcode_candidate" in patterns and target_field == "Barcode Number":
        score = max(score, 0.95)
    if "currency_candidate" in patterns and target_field == "Currency":
        score = max(score, 0.95)
    if "description_candidate" in patterns and target_field == "Description Value":
        score = max(score, 0.82)
    if "asset_candidate" in patterns and target_field in ["FilePath", "FileName"]:
        score = max(score, 0.86)
    if "flag_candidate" in patterns and target_field in ["HazmatFlag", "Product Status"]:
        score = max(score, 0.60)

    # Preferred section boost/penalty to reduce bad duplicate matches.
    preferred_section = PREFERRED_SECTION_BY_VENDOR_COL.get(vendor_norm)
    if preferred_section:
        if target_section == preferred_section:
            score += 0.05
        else:
            score -= 0.10

    return max(0.0, min(1.0, score))


def suggest_mappings(schema: Dict[str, List[str]], vendor_sheets: Dict[str, pd.DataFrame], threshold: float = 0.55) -> List[Dict[str, Any]]:
    targets = build_target_fields(schema)
    suggestions = []

    for vendor_sheet, df in vendor_sheets.items():
        for vendor_col in df.columns:
            if str(vendor_col).startswith("Unnamed"):
                continue
            patterns = detect_value_pattern(df[vendor_col])
            candidates = []
            for target in targets:
                score = score_vendor_to_target(str(vendor_col), target["section"], target["field"], patterns)
                if score >= threshold:
                    candidates.append({**target, "confidence": round(score, 3)})

            candidates = sorted(candidates, key=lambda x: x["confidence"], reverse=True)[:5]
            best = candidates[0] if candidates else None
            suggestions.append({
                "vendor_sheet": vendor_sheet,
                "vendor_column": str(vendor_col),
                "detected_patterns": patterns,
                "best_match": best,
                "candidates": candidates,
                "status": "matched" if best else "unmapped",
            })
    return suggestions


def choose_primary_vendor_df(vendor_sheets: Dict[str, pd.DataFrame]) -> Tuple[str, pd.DataFrame]:
    # MVP: use the non-empty sheet with the most rows * columns.
    return max(vendor_sheets.items(), key=lambda item: item[1].shape[0] * max(1, item[1].shape[1]))


def build_field_map(suggestions: List[Dict[str, Any]]) -> Dict[Tuple[str, str], Tuple[str, float]]:
    """Return {(section, field): (vendor_column, confidence)} keeping highest confidence per target field."""
    fmap: Dict[Tuple[str, str], Tuple[str, float]] = {}
    for s in suggestions:
        best = s.get("best_match")
        if not best:
            continue
        key = (best["section"], best["field"])
        current = fmap.get(key)
        if current is None or best["confidence"] > current[1]:
            fmap[key] = (s["vendor_column"], best["confidence"])
    return fmap


def normalize_to_fgi_workbook(
    schema: Dict[str, List[str]],
    vendor_df: pd.DataFrame,
    suggestions: List[Dict[str, Any]],
    out_xlsx: Path,
    max_rows: Optional[int] = None,
) -> Dict[str, Any]:
    """Create normalized FGI workbook.

    Uses openpyxl write_only mode so large vendor files do not become painfully slow.
    """
    if max_rows is not None and max_rows > 0:
        vendor_df = vendor_df.head(max_rows).copy()

    field_map = build_field_map(suggestions)

    # Find a usable part number source once and reuse it across all sheets.
    part_number_source = None
    for section in schema:
        key = (section, "Part Number")
        if key in field_map:
            part_number_source = field_map[key][0]
            break
    if part_number_source is None:
        for c in vendor_df.columns:
            if normalize_text(c) in ["formatted item", "compressed item", "part number", "sku", "item"]:
                part_number_source = c
                break

    wb = Workbook(write_only=True)
    rows_written = {}

    for section, cols in schema.items():
        ws = wb.create_sheet(title=section[:31])
        ws.append(cols)
        count = 0

        for _, src_row in vendor_df.iterrows():
            out_row = {col: None for col in cols}

            # Constants first.
            for col, val in CONSTANT_DEFAULTS.get(section, {}).items():
                if col in out_row:
                    out_row[col] = val

            # Reuse part number across all sections.
            if "Part Number" in out_row and part_number_source is not None:
                out_row["Part Number"] = src_row.get(part_number_source)

            # Fill mapped fields.
            for col in cols:
                key = (section, col)
                if key in field_map:
                    source_col, _ = field_map[key]
                    if source_col in vendor_df.columns:
                        out_row[col] = src_row.get(source_col)

            # For non-anchor sheets, skip rows that have no useful business value.
            if section != "Item_Master":
                business_cols = [
                    c for c in cols
                    if c not in ["Part Number", "Sequence"]
                    and not c.endswith("Change Type")
                ]
                has_business_data = any(
                    pd.notna(out_row.get(c)) and str(out_row.get(c)).strip() != ""
                    for c in business_cols
                )
                if not has_business_data:
                    continue

            ws.append([clean_excel_value(out_row.get(col)) for col in cols])
            count += 1

        rows_written[section] = count

    wb.save(out_xlsx)

    return {
        "output_workbook": str(out_xlsx),
        "rows_written_by_sheet": rows_written,
        "part_number_source": str(part_number_source) if part_number_source is not None else None,
    }


def clean_excel_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    # Keep values Excel-safe.
    if isinstance(value, (list, dict, tuple, set)):
        return json.dumps(value)
    return value


def save_mapping_report(suggestions: List[Dict[str, Any]], out_json: Path, out_csv: Path) -> None:
    out_json.write_text(json.dumps(suggestions, indent=2), encoding="utf-8")

    rows = []
    for s in suggestions:
        best = s.get("best_match") or {}
        rows.append({
            "vendor_sheet": s.get("vendor_sheet"),
            "vendor_column": s.get("vendor_column"),
            "detected_patterns": ", ".join(s.get("detected_patterns", [])),
            "target_section": best.get("section"),
            "target_field": best.get("field"),
            "confidence": best.get("confidence"),
            "status": s.get("status"),
        })
    pd.DataFrame(rows).to_csv(out_csv, index=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", required=True, help="Path to mapped.xlsx / FGI static schema workbook")
    parser.add_argument("--vendor-file", required=True, help="Path to random vendor Excel/CSV file")
    parser.add_argument("--out-dir", default="outputs/non_opticat_match", help="Output folder")
    parser.add_argument("--threshold", type=float, default=0.55, help="Minimum confidence threshold")
    parser.add_argument("--max-rows", type=int, default=0, help="Optional row limit for testing. 0 means all rows.")
    args = parser.parse_args()

    schema_path = Path(args.schema)
    vendor_path = Path(args.vendor_file)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    schema = read_schema(schema_path)
    vendor_sheets = read_vendor_file(vendor_path)
    primary_sheet, primary_df = choose_primary_vendor_df(vendor_sheets)

    suggestions = suggest_mappings(schema, vendor_sheets, threshold=args.threshold)

    save_mapping_report(
        suggestions,
        out_dir / "mapping_suggestions.json",
        out_dir / "mapping_suggestions.csv",
    )

    summary = normalize_to_fgi_workbook(
        schema,
        primary_df,
        suggestions,
        out_dir / "normalized_fgi_output.xlsx",
        max_rows=args.max_rows if args.max_rows > 0 else None,
    )

    summary.update({
        "schema_sheets": list(schema.keys()),
        "vendor_file": str(vendor_path),
        "primary_vendor_sheet_used": primary_sheet,
        "mapping_suggestions_json": str(out_dir / "mapping_suggestions.json"),
        "mapping_suggestions_csv": str(out_dir / "mapping_suggestions.csv"),
    })

    (out_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
