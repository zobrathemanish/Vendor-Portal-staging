# vendor_adapters/dayton_adapter.py

from __future__ import annotations
from typing import Dict, Any, List
from io import BytesIO

import pandas as pd
from lxml import etree

from mapping_validation.vendor_adapters.base_adapter import BaseVendorAdapter
from mapping_validation.mapping_engine.excel_mapper import ExcelMapper
from mapping_validation.helpers.file_ingestion import list_vendor_files, download_blob_bytes
from openpyxl import load_workbook
import re



class DaytonAdapter(BaseVendorAdapter):
    """
    Adapter for Dayton Parts (OptiCat XML + custom Excel pricing).

    Handles:
      - Flattening multi-row header ([9,10])
      - Extracting Effective Date from a free text cell (e.g., K5)
      - Cleaning inconsistent Dayton column names
      - Passing final DataFrame → ExcelMapper.map_pricing_xlsx()
    """

    # -------------------------------------------------------------
    # PRODUCT (XML) — identical to GroteAdapter logic
    # -------------------------------------------------------------
    def load_product(self) -> Dict[str, pd.DataFrame]:
        """
        Dayton is an OptiCat vendor → XML mapping works the same as Grote.
        We simply reuse GroteAdapter logic through BaseVendorAdapter
        by relying on XMLMapper from the factory.
        """
        # Import here to avoid circular dependency
        from mapping_validation.vendor_adapters.grote_adapter import GroteAdapter

        grote = GroteAdapter(
            vendor=self.vendor,
            mapping=self.mapping,
            submission_id=self.submission_id
        )

        return grote.load_product()

    # -------------------------------------------------------------
    # PRICING (XLSX)
    # -------------------------------------------------------------
    def load_pricing(self) -> Dict[str, pd.DataFrame]:
        vendor_name = self.vendor
        pricing_cfg = self.pm.get("Pricing")
        if not pricing_cfg:
            return {}

        sheet_name = pricing_cfg.get("sheet")
        header_rows = pricing_cfg.get("header_row", None)  # e.g., [8, 9]

        # -------------------------
        # Load Pricing Excel File
        # -------------------------
        pricing_files = list_vendor_files(
            vendor_name,
            "pricing",
            self.submission_id
        )
        if not pricing_files:
            return {}

        raw_bytes = download_blob_bytes(pricing_files[0])
        xl = pd.ExcelFile(BytesIO(raw_bytes))

        if sheet_name not in xl.sheet_names:
            raise ValueError(
                f"DaytonAdapter: YAML sheet '{sheet_name}' not found. "
                f"Available: {xl.sheet_names}"
            )

        # -----------------------------------------------------
        # Extract Effective Date from cell K4 (Dayton-specific)
        # -----------------------------------------------------
        from openpyxl import load_workbook
        import re

        wb = load_workbook(filename=BytesIO(raw_bytes), data_only=True)
        ws = wb[sheet_name]

        raw_effective = ws["K4"].value or ""   # e.g., "Effective: 2025-09-26"
        match = re.search(r"\d{4}-\d{2}-\d{2}", str(raw_effective))
        effective_date = match.group(0) if match else None

        # ----------------------------------------------
        # Parse sheet with multi-row headers if defined
        # ----------------------------------------------
        if isinstance(header_rows, list) and len(header_rows) > 1:
            df_raw = xl.parse(sheet_name, header=header_rows, dtype = str, keep_default_na=False)
            print("\n🔥 Dayton Raw Columns:", list(df_raw.columns))
        else:
            df_raw = xl.parse(sheet_name,dtype=str, keep_default_na=False)

        # ----------------------------------------------
        # Clean flattened header
        # e.g., ('Sugg List', 'List') → 'Sugg List List'
        # ----------------------------------------------
        df_raw.columns = [
            " ".join([str(x).strip() for x in col if str(x) != "nan"]).strip()
            if isinstance(col, tuple)
            else str(col).strip()
            for col in df_raw.columns
        ]

        # ----------------------------------------------
        # Pass cleaned sheet → ExcelMapper
        # ----------------------------------------------
        excel_mapper = ExcelMapper()
        df_price = excel_mapper.map_pricing_xlsx(df_raw, pricing_cfg)

        # ----------------------------------------------
        # Apply Effective Date AFTER mapping
        # ----------------------------------------------
        df_price["Effective Date"] = effective_date

        # Ensure output has all defined columns even if empty
        if df_price.empty:
            df_price = pd.DataFrame(columns=list(pricing_cfg.get("mappings", {}).keys()))

        return {"Pricing": df_price}



    # -------------------------------------------------------------
    # OPTIONAL: Vendor-specific enrichments
    # -------------------------------------------------------------
    def post_process(self, combined: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """
        For Dayton:
          - You may want to merge additional pricing fields into Item Master later.
          - Currently no special behavior added.
        """
        return combined

