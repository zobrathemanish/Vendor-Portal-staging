# vendor_adapters/grote_adapter.py (this adapter is used by all opticat vendors although named grote_adapter)

from __future__ import annotations
from typing import Dict
from io import BytesIO

import pandas as pd
from lxml import etree

from mapping_validation.mapping_engine.xml_mapper import XMLMapper
from mapping_validation.mapping_engine.excel_mapper import ExcelMapper
from mapping_validation.vendor_adapters.base_adapter import BaseVendorAdapter

from mapping_validation.helpers.file_ingestion import download_blob_bytes


class GroteAdapter(BaseVendorAdapter):
    """
    Adapter for OptiCat XML vendors (Grote, TruckLite, Haldex, etc.)
    Supports workflow-based ingestion:

        raw/vendor=<vendor>/<workflow>/submission=<submission_id>/
    """
    def __init__(self, vendor, mapping, workflow, submission_type, submission_id):
        super().__init__(vendor, mapping, workflow, submission_type, submission_id)

    # ------------------------------------------------------------
    # PRODUCT LOADING
    # ------------------------------------------------------------
    # ------------------------------------------------------------
    # PRODUCT LOADING
    # ------------------------------------------------------------
    def load_product(self) -> Dict[str, pd.DataFrame]:

        from mapping_validation.scripts.pre_etl_ingest_mapping import log_ingestion_error  # local import to avoid circular dependency

        vendor_name = self.vendor

        from azure.storage.blob import BlobServiceClient
        import os

        conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        blob_service = BlobServiceClient.from_connection_string(conn)
        container = blob_service.get_container_client("bronze")

        base_prefix = (
            f"raw/vendor={self.vendor}/"
            f"workflow={self.workflow}/"
            f"submission_type={self.submission_type}/"
            f"submission={self.submission_id}/"
            f"original_xml/"
        )

        print("[DEBUG PREFIX]", base_prefix)

        product_files = [
            blob.name
            for blob in container.list_blobs(name_starts_with=base_prefix)
            if blob.name.lower().endswith(".xml")
        ]

        print("[DEBUG FILES]", product_files)

        # ------------------------------------------------------------
        # HANDLE NO FILES
        # ------------------------------------------------------------
        if not product_files:

            sections = {}

            for sec_name, sec_cfg in self.pm.items():

                if sec_name == "Pricing":
                    continue

                if isinstance(sec_cfg, dict):

                    if sec_name == "Item Master":
                        sections[sec_name] = pd.DataFrame(columns=list(sec_cfg.keys()))
                    else:
                        sections[sec_name] = pd.DataFrame(columns=list(sec_cfg.get("mappings", {}).keys()))

            raise RuntimeError(
                f"No product files found for vendor={vendor_name}, submission={self.submission_id}"
            )

        # ------------------------------------------------------------
        # MULTI-FILE PROCESSING
        # ------------------------------------------------------------
        all_section_frames = {
            sec_name: []
            for sec_name in self.pm.keys()
            if sec_name != "Pricing"
        }

        for xml_path in product_files:

            print(f"[PROCESSING XML] {xml_path}")

            try:
                xml_bytes = download_blob_bytes(xml_path)

                parser = etree.XMLParser(recover=True)
                root = etree.fromstring(xml_bytes, parser)

            except Exception as e:

                log_ingestion_error(
                    vendor=vendor_name,
                    stage="XML_PARSE",
                    file=xml_path,
                    error=e,
                    submission_id=self.submission_id
                )

                continue  # skip bad file

            xml_mapper = XMLMapper(root)

            for sec_name, sec_cfg in self.pm.items():

                if sec_name == "Pricing":
                    continue

                if not isinstance(sec_cfg, dict):
                    continue

                # -------------------------------
                # ITEM MASTER
                # -------------------------------
                if sec_name == "Item Master":

                    df = xml_mapper.map_item_master(sec_cfg)

                    if not df.empty:
                        all_section_frames[sec_name].append(df)

                    continue

                # -------------------------------
                # OTHER SECTIONS
                # -------------------------------
                path = sec_cfg.get("path")
                mappings = sec_cfg.get("mappings")

                if not path or not mappings:
                    continue

                df = xml_mapper.map_xml_section(path, mappings)

                if not df.empty:
                    all_section_frames[sec_name].append(df)

        # ------------------------------------------------------------
        # MERGE ALL FILES
        # ------------------------------------------------------------
        sections = {}

        for sec_name, df_list in all_section_frames.items():

            sec_cfg = self.pm.get(sec_name, {})

            if not df_list:

                # Empty fallback
                if sec_name == "Item Master":
                    sections[sec_name] = pd.DataFrame(columns=list(sec_cfg.keys()))
                else:
                    sections[sec_name] = pd.DataFrame(columns=list(sec_cfg.get("mappings", {}).keys()))

                continue

            merged_df = pd.concat(df_list, ignore_index=True)

            # Safe deduplication
            merged_df.drop_duplicates(inplace=True)

            sections[sec_name] = merged_df

            print(f"[MERGED] {sec_name}: rows={len(merged_df)}")

        return sections

    # ------------------------------------------------------------
    # PRICING LOADING
    # ------------------------------------------------------------
    def load_pricing(self) -> Dict[str, pd.DataFrame]:

        vendor_name = self.vendor
        pricing_cfg = self.pm.get("Pricing")

        if not pricing_cfg:
            return {}

        from azure.storage.blob import BlobServiceClient
        import os

        conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        blob_service = BlobServiceClient.from_connection_string(conn)
        container = blob_service.get_container_client("bronze")

        base_prefix = (
            f"raw/vendor={self.vendor}/"
            f"workflow={self.workflow}/"
            f"submission_type={self.submission_type}/"
            f"submission={self.submission_id}/"
            f"original_excel/"
        )

        print("[DEBUG PRICING PREFIX]", base_prefix)

        pricing_files = [
            blob.name
            for blob in container.list_blobs(name_starts_with=base_prefix)
            if blob.name.lower().endswith((".xlsx", ".xls"))
        ]

        print("[DEBUG PRICING FILES]", pricing_files)

        if not pricing_files:

            return {
                "Pricing": pd.DataFrame(
                    columns=list(pricing_cfg.get("mappings", {}).keys())
                )
            }

        xbytes = download_blob_bytes(pricing_files[0])

        xls = pd.ExcelFile(BytesIO(xbytes))

        yaml_sheet = pricing_cfg.get("sheet")

        if yaml_sheet:

            print(" YAML requested sheet:", yaml_sheet)
            print(" Sheets available:", xls.sheet_names)

            if yaml_sheet not in xls.sheet_names:

                raise ValueError(
                    f"YAML sheet '{yaml_sheet}' not found. "
                    f"Available sheets: {xls.sheet_names}"
                )

            df_raw = xls.parse(
                yaml_sheet,
                dtype=str,
                keep_default_na=False
            )

            df_raw.columns = df_raw.columns.astype(str).str.strip()

        else:

            required_cols = set(pricing_cfg.get("mappings", {}).values())

            df_raw = None

            for sheet in xls.sheet_names:

                tmp = xls.parse(
                    sheet,
                    dtype=str,
                    keep_default_na=False
                )

                tmp.columns = tmp.columns.astype(str).str.strip()

                if required_cols.intersection(tmp.columns):

                    print(f"✔ Auto-detected pricing sheet: {sheet}")
                    df_raw = tmp
                    break

            if df_raw is None:

                raise ValueError(
                    f"Could not detect sheet containing pricing columns: {required_cols}"
                )

        excel_mapper = ExcelMapper()

        df_price = excel_mapper.map_pricing_xlsx(
            df_raw,
            pricing_cfg
        )

        if df_price.empty:

            df_price = pd.DataFrame(
                columns=list(pricing_cfg.get("mappings", {}).keys())
            )

        return {"Pricing": df_price}

    # ------------------------------------------------------------
    # POST PROCESS
    # ------------------------------------------------------------
    def post_process(self, combined: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:

        if "Item Master" in combined and "Pricing" in combined:

            df_item = combined["Item Master"].copy()
            df_price = combined["Pricing"].copy()

            df_item.rename(columns={"Part Number": "PN"}, inplace=True)
            df_price.rename(columns={"Part Number": "PN"}, inplace=True)

            if "Category" in df_price.columns:

                def normalize_pn(s: pd.Series):

                    return (
                        s.astype(str)
                        .str.strip()
                        .str.lstrip("0")
                    )

                df_item["PN_norm"] = normalize_pn(df_item["PN"])
                df_price["PN_norm"] = normalize_pn(df_price["PN"])

                df_item = df_item.merge(
                    df_price[["PN_norm", "Category"]],
                    on="PN_norm",
                    how="left"
                )

                df_item.drop(columns=["PN_norm"], inplace=True)

            df_item.rename(columns={"PN": "Part Number"}, inplace=True)

            combined["Item Master"] = df_item

        return combined