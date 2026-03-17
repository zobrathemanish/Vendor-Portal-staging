# vendor_adapters/rigid_adapter.py

from __future__ import annotations
from typing import Dict, Any
from io import BytesIO
import pandas as pd

from mapping_validation.mapping_engine.excel_mapper import ExcelMapper
from mapping_validation.vendor_adapters.base_adapter import BaseVendorAdapter

from mapping_validation.helpers.file_ingestion import list_vendor_files, download_blob_bytes



class RigidAdapter(BaseVendorAdapter):
    """Adapter for Excel-based unified vendors like Rigid Industries."""

    def load_product(self) -> Dict[str, pd.DataFrame]:
        sections = {}

        product_files = list_vendor_files(self.vendor, "unified")
        if not product_files:
            return {}

        blob_path = product_files[0]
        xbytes = download_blob_bytes(blob_path)

        # Load all sheets
        sheets = pd.read_excel(BytesIO(xbytes), sheet_name=None)

        excel_mapper = ExcelMapper()

        for sec_name, sec_cfg in self.pm.items():
            if sec_name == "Pricing":
                continue

            mappings = sec_cfg.get("mappings")
            sheet_name = sec_cfg.get("sheet")
            is_explode = sec_cfg.get("explode", False)

            if sheet_name is None or mappings is None:
                continue

            df_sheet = sheets.get(sheet_name)
            if df_sheet is None:
                continue

            # Handle explode (Descriptions or Extended Info)
            if is_explode:
                df = excel_mapper.explode_wide_to_long(df_sheet, mappings, code_field=mappings.get("code_field", "Description Code"))
            else:
                df = excel_mapper.map_excel_section(df_sheet, mappings)

            sections[sec_name] = df

        return sections

    def load_pricing(self) -> Dict[str, pd.DataFrame]:
        pricing_cfg = self.pm.get("Pricing")
        if not pricing_cfg:
            return {}

        # For Excel vendors, pricing is inside the unified file
        product_files = list_vendor_files(self.vendor, "unified")
        xbytes = download_blob_bytes(product_files[0])

        # 🔥 Load ALL sheets (critical for lookups)
        sheets = pd.read_excel(BytesIO(xbytes), sheet_name=None, dtype = str, keep_default_na=False)

        sheet = pricing_cfg.get("sheet")
        header_row = pricing_cfg.get("header_row", 1)

        # Load the actual pricing sheet
        df_raw = pd.read_excel(BytesIO(xbytes), sheet_name=sheet, header=header_row - 1, dtype = str, keep_default_na=False)

        excel_mapper = ExcelMapper()
        pricing = excel_mapper.map_pricing_xlsx(
            df_raw,
            pricing_cfg,
            product_sheets=sheets    # <-- NOW sheets exists!
        )

        return {"Pricing": pricing}

