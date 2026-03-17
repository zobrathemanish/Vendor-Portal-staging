# mapping_engine/excel_mapper.py

from __future__ import annotations

from typing import Dict, Any, List
import pandas as pd


class ExcelMapper:
    """
    A reusable Excel mapping engine for non-OptiCat vendors (e.g., Rigid Industries)
    and for pricing sheets for XML vendors.

    Responsibilities:
      - map_excel_section(): YAML → DF mapping from Excel sheet
      - map_pricing_xlsx(): pricing mapping, lookups, header parsing
      - explode_wide_to_long(): convert wide description/extended-info Excel sheets
    """

    # ------------------------------------------------
    # SECTION MAPPING (generic)
    # ------------------------------------------------
    def map_excel_section(self, df: pd.DataFrame, mappings: Dict[str, Any]) -> pd.DataFrame:
        df = df.copy()
        df.columns = df.columns.astype(str).str.strip()

        out = pd.DataFrame(index=df.index)

        for col, src in mappings.items():

            # CASE 1: null → empty
            if src is None:
                out[col] = None

            # CASE 2: list of columns (Rigid-style feature aggregation)
            elif isinstance(src, list):
                existing_cols = [c for c in src if c in df.columns]

                if not existing_cols:
                    out[col] = None
                else:
                    out[col] = df[existing_cols].astype(str).apply(
                        lambda row: ", ".join(
                            v for v in row if v not in ["", "nan", "None"]
                        ),
                        axis=1
                    )

            # CASE 3: direct Excel column
            elif isinstance(src, str) and src in df.columns:
                out[col] = df[src]

            # CASE 4: constant string
            else:
                out[col] = src

        return out

    # ------------------------------------------------
    # PRICING MAPPER
    # ------------------------------------------------
    def map_pricing_xlsx(
        self,
        df: pd.DataFrame,
        pricing_cfg: Dict[str, Any],
        product_sheets: Dict[str, pd.DataFrame] | None = None
    ) -> pd.DataFrame:
        """
        Map pricing Excel data using YAML:
          - mappings: regular Excel column → logical column mapping
          - lookups: bring additional columns from product_xlsx
          - @lookup syntax: @lookup.DATASHEET.MOQ Unit
        """
        src_map = pricing_cfg.get("mappings", {})
        lookups = pricing_cfg.get("lookups", {})

        df = df.copy()
        df.columns = df.columns.astype(str).str.strip()

        if "Part Number" in df.columns:
            df["Part Number"] = df["Part Number"].astype(str).str.strip()

        # ------------------------------------------------
        # APPLY LOOKUPS (Excel vendor files use "product_sheets")
        # ------------------------------------------------
        if lookups and product_sheets is not None:
            for lookup_name, cfg in lookups.items():

                key_col = cfg["key"]     # join key (e.g., PN or Catalogue Number)
                col_map = cfg.get("columns", {})

                # Default sheet name → lookup_name
                sheet_name = cfg.get("sheet", lookup_name)

                if sheet_name not in product_sheets:
                    print(f"⚠️ Lookup sheet '{sheet_name}' not found.")
                    continue

                lk_df = product_sheets[sheet_name].copy()
                lk_df.columns = lk_df.columns.astype(str).str.strip()

                # Validate required columns
                needed_cols = [key_col] + list(col_map.values())
                missing = [c for c in needed_cols if c not in lk_df.columns]
                if missing:
                    print(f"⚠️ Lookup '{lookup_name}' missing columns: {missing}")
                    continue

                lk_df = lk_df[needed_cols]

                # Rename columns: source → logical
                rename_map = {src_col: out_col for out_col, src_col in col_map.items()}
                lk_df = lk_df.rename(columns=rename_map)

                # Merge into pricing df
                if key_col not in df.columns:
                    print(f"⚠️ Pricing missing join key '{key_col}' for lookup '{lookup_name}'.")
                    continue

                df = df.merge(lk_df, how="left", on=key_col)

        # ------------------------------------------------
        # BUILD OUTPUT DF USING mappings
        # ------------------------------------------------
        out = pd.DataFrame(index=df.index)

        for col, src in src_map.items():

            # null → empty
            if src is None:
                out[col] = None

            # special @lookup syntax
            elif isinstance(src, str) and src.startswith("@lookup."):
                # format: @lookup.DATASHEET.MOQ Unit
                try:
                    _, rest = src.split(".", 1)         # DATASHEET.MOQ Unit
                    _, col_name = rest.split(".", 1)    # MOQ Unit
                except ValueError:
                    col_name = src

                col_name = col_name.strip()
                out[col] = df[col_name] if col_name in df.columns else None

            # direct Excel column
            elif isinstance(src, str) and src in df.columns:
                out[col] = df[src]

            # constant
            else:
                out[col] = src

        return out

    # ------------------------------------------------
    # EXPLODE (wide → long)
    # ------------------------------------------------
    def explode_wide_to_long(
        self,
        df: pd.DataFrame,
        mappings: Dict[str, Any],
        code_field: str
    ) -> pd.DataFrame:
        """
        Convert wide columns into long rows, used for Rigid-style Excel Descriptions.
        Example:
            Description Code: ["ASC", "MKT", "LONG"]
            Each row spreads values from multiple columns → multiple long-format rows.
        """

        df = df.copy()
        df.columns = df.columns.astype(str).str.strip()

        codes = mappings.get(code_field, [])
        part_col = mappings.get("Part Number")

        rows: List[Dict[str, Any]] = []

        for _, r in df.iterrows():
            pn = r.get(part_col)

            for code in codes:
                excel_col = mappings.get(code)
                if not excel_col or excel_col not in df.columns:
                    continue

                value = r.get(excel_col)

                if pd.isna(value) or value in ["", "nan", "None"]:
                    continue

                # DESCRIPTIONS
                if code_field == "Description Code":
                    rows.append({
                        "Part Number": pn,
                        "Description Code": code,
                        "Description Value": value,
                        "Sequence": None,
                        "Description Change Type": None
                    })

                # EXTENDED INFO
                elif code_field == "Extended Info Code":
                    rows.append({
                        "Part Number": pn,
                        "Extended Info Code": code,
                        "Extended Info Value": value,
                        "Extended Info Change Type": None
                    })

        return pd.DataFrame(rows)
