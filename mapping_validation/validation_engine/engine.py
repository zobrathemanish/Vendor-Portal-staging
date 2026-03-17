# validation_engine/engine.py

from __future__ import annotations
from typing import Dict, Any, List, Tuple
import os
import re
import pandas as pd


ErrorList = List[Dict[str, Any]]


def _norm_str(val) -> str:
    """
    Normalize values to a trimmed string.
    Treat None, NaN, 'nan', 'None' as empty.
    """
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in ("none", "nan"):
        return ""
    return s


def _is_blank(val) -> bool:
    return _norm_str(val) == ""


def _to_float_or_none(val):
    if _is_blank(val):
        return None
    try:
        return float(val)
    except Exception:
        return None


def _clean_price_generic(val):
    """
    Cleans any vendor price format into a float.
    Works for: $1,200.50 | CAD 1200 | 1200 USD | Net: $2500 | ' 1,200 ' etc.
    Returns float or None.
    """
    if val is None:
        return None

    text = str(val)

    # Remove currency codes & symbols, keep digits . , -
    text = re.sub(r"[^\d.,-]", "", text)
    text = text.replace(",", "")

    if text.strip() == "":
        return None

    try:
        return float(text)
    except Exception:
        return None


class ValidationEngine:
    """
    Flexible validation engine.

    Expects:
      - vendor_name: e.g. 'Grote Lighting'
      - cfg: validation config dict (the 'validation:' block for that vendor)
      - mapped_root: folder where mapped/vendor=<vendor>/ lives
    """

    def __init__(self, vendor_name: str, cfg: Dict[str, Any], mapped_root: str = "mapped"):
        self.vendor = vendor_name
        self.cfg = cfg
        self.mapped_root = mapped_root

    # ---------------------------------------------------------
    # Helpers to load parquets
    # ---------------------------------------------------------
    def _mapped_dir(self) -> str:
        return os.path.join(self.mapped_root, f"vendor={self.vendor}")

    def _load_section_parquet(self, safe_name: str) -> pd.DataFrame | None:
        """
        safe_name: e.g. 'Item_Master', 'Descriptions', 'Extended_Info', etc.
        """
        path = os.path.join(self._mapped_dir(), f"{safe_name}.parquet")
        if not os.path.exists(path):
            return None
        return pd.read_parquet(path)

    # ---------------------------------------------------------
    # Public entrypoint
    # ---------------------------------------------------------
    def run(self) -> Tuple[pd.DataFrame, ErrorList]:
        """
        Run full validation pipeline for vendor.
        Returns:
          - df_flags: per-SKU summary flags (Item Master with extra columns)
          - errors: list of row-level error dicts
        """
        mapped_dir = self._mapped_dir()
        if not os.path.exists(mapped_dir):
            raise FileNotFoundError(f"Mapped folder not found: {mapped_dir}")

        # Load sections
        df_item = self._load_section_parquet("Item_Master")
        if df_item is None or df_item.empty:
            raise ValueError("Missing or empty Item_Master.parquet")

        df_desc = self._load_section_parquet("Descriptions")
        df_ext  = self._load_section_parquet("Extended_Info")
        df_attr = self._load_section_parquet("Attributes")
        df_pi   = self._load_section_parquet("Part_InterChange")
        df_pkg  = self._load_section_parquet("Packages")
        df_price = self._load_section_parquet("Pricing")
        df_assets = self._load_section_parquet("Digital_Assets")

        errors: ErrorList = []

        # ITEM MASTER
        item_cfg = self.cfg.get("item_master", {})
        item_valid = self._validate_item_master(df_item, item_cfg, errors)

        # DESCRIPTIONS
        desc_cfg = self.cfg.get("descriptions", {})
        if df_desc is not None and not df_desc.empty and desc_cfg:
            desc_row_valid = self._validate_descriptions(df_item, df_desc, desc_cfg, errors)
        else:
            desc_row_valid = None

        # EXTENDED INFO
        ext_cfg = self.cfg.get("extended_info", {})
        if df_ext is not None and not df_ext.empty and ext_cfg:
            ext_row_valid = self._validate_extended_info(df_item, df_ext, ext_cfg, errors)
        else:
            ext_row_valid = None

        # ATTRIBUTES
        attr_cfg = self.cfg.get("attributes", {})
        if df_attr is not None and not df_attr.empty and attr_cfg:
            attr_row_valid = self._validate_attributes(df_attr, attr_cfg, errors)
        else:
            attr_row_valid = None

        # PART INTERCHANGE
        pi_cfg = self.cfg.get("part_interchange", {})
        if df_pi is not None and not df_pi.empty and pi_cfg:
            pi_row_valid = self._validate_part_interchange(df_pi, pi_cfg, errors)
        else:
            pi_row_valid = None

        # PACKAGES
        pkg_cfg = self.cfg.get("packages", {})
        if df_pkg is not None and not df_pkg.empty and pkg_cfg:
            pkg_row_valid = self._validate_packages(df_item, df_pkg, pkg_cfg, errors)
        else:
            # still need to check "require_at_least_one_row"
            self._validate_packages_empty(df_item, df_pkg, pkg_cfg, errors)
            pkg_row_valid = None

        # PRICING
        price_cfg = self.cfg.get("pricing", {})
        if df_price is not None and not df_price.empty and price_cfg:
            df_price_clean = self._clean_pricing(df_price, price_cfg)
            price_row_valid = self._validate_pricing(df_item, df_price_clean, price_cfg, errors)
        else:
            df_price_clean = df_price
            price_row_valid = None

        # ASSETS
        assets_cfg = self.cfg.get("assets", {})
        if df_assets is not None and not df_assets.empty and assets_cfg:
            assets_row_valid = self._validate_assets(df_item, df_assets, assets_cfg, errors)
        else:
            self._validate_assets_empty(df_item, df_assets, assets_cfg, errors)
            assets_row_valid = None

        # -------------------------------------------------
        # Build SKU-level summary flags
        # -------------------------------------------------
        sku_col = "Part Number"
        df_flags = df_item.copy()
        df_flags["is_item_master_valid"] = item_valid

        # Descriptions
        if df_desc is not None and desc_row_valid is not None:
            tmp = df_desc.assign(_v=desc_row_valid)
            sku_valid_desc = tmp.groupby(sku_col)["_v"].any()
            df_flags["has_valid_descriptions"] = df_flags[sku_col].map(sku_valid_desc).fillna(False)
        else:
            df_flags["has_valid_descriptions"] = False

        # Pricing
        if df_price_clean is not None and price_row_valid is not None:
            tmp = df_price_clean.assign(_v=price_row_valid)
            sku_valid_price = tmp.groupby(sku_col)["_v"].any()
            df_flags["has_pricing"] = df_flags[sku_col].isin(df_price_clean[sku_col].dropna().unique())
            df_flags["has_valid_pricing"] = df_flags[sku_col].map(sku_valid_price).fillna(False)
        else:
            df_flags["has_pricing"] = False
            df_flags["has_valid_pricing"] = False

        # Assets
        if df_assets is not None and assets_row_valid is not None:
            sku_has_assets = df_assets.groupby(sku_col).size() > 0
            df_flags["has_assets"] = df_flags[sku_col].map(sku_has_assets).fillna(False)
        else:
            df_flags["has_assets"] = False

        # Overall validity (you can tune this later)
        df_flags["is_overall_valid"] = (
            df_flags["is_item_master_valid"]
            & df_flags["has_valid_descriptions"]
            & df_flags["has_valid_pricing"]
            & df_flags["has_assets"]
        )

        return df_flags, errors

    # =====================================================
    # ITEM MASTER VALIDATION
    # =====================================================
    def _validate_item_master(
        self,
        df: pd.DataFrame,
        cfg: Dict[str, Any],
        errors: ErrorList
    ) -> pd.Series:
        required = cfg.get("required_fields", [])
        unique_key = cfg.get("unique_key", "Part Number")

        barcode_type_field = cfg.get("barcode_type_field")
        barcode_number_field = cfg.get("barcode_number_field")

        upc_field = cfg.get("upc_field")
        en_field = cfg.get("en_field")
        unspsc_field = cfg.get("unspsc_field")

        allowed_statuses = cfg.get("allowed_statuses", [])

        hazmat_field = cfg.get("hazmat_field")
        allowed_hazmat_values = [v.lower() for v in cfg.get("allowed_hazmat_values", [])]
        hazmat_truthy_values = [v.lower() for v in cfg.get("hazmat_truthy_values", [])]
        un_number_field = cfg.get("un_number_field")

        is_valid = pd.Series(True, index=df.index)

        # Ensure Part Number column exists
        if "Part Number" not in df.columns:
            raise KeyError("Item Master must contain 'Part Number' column")

        # Required fields
        for field in required:
            if field not in df.columns:
                # Missing column entirely
                for idx, row in df.iterrows():
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Item Master",
                        "Part Number": row.get("Part Number"),
                        "Field": field,
                        "ErrorCode": "MISSING_COLUMN",
                        "Message": f"Required column '{field}' missing in Item Master."
                    })
                is_valid[:] = False
                continue

            missing_mask = df[field].apply(_is_blank)
            for idx in df[missing_mask].index:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Item Master",
                    "Part Number": df.at[idx, "Part Number"],
                    "Field": field,
                    "ErrorCode": "REQUIRED_FIELD_EMPTY",
                    "Message": f"Required field '{field}' is empty."
                })
            is_valid[missing_mask] = False

        # Unique key
        if unique_key in df.columns:
            dup_mask = df[unique_key].astype(str).duplicated(keep=False)
            for idx in df[dup_mask].index:
                pn = df.at[idx, unique_key]
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Item Master",
                    "Part Number": pn,
                    "Field": unique_key,
                    "ErrorCode": "DUPLICATE_KEY",
                    "Message": f"Duplicate {unique_key} '{pn}'."
                })
            is_valid[dup_mask] = False

        # Barcode cross-field rule
        if barcode_type_field and barcode_number_field:
            for idx, row in df.iterrows():
                bt = _norm_str(row.get(barcode_type_field))
                bn = _norm_str(row.get(barcode_number_field))
                pn = row.get("Part Number")
                # If either is provided, both must be
                if (bt and not bn) or (bn and not bt):
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Item Master",
                        "Part Number": pn,
                        "Field": f"{barcode_type_field}/{barcode_number_field}",
                        "ErrorCode": "BARCODE_PAIR",
                        "Message": "Barcode Type and Barcode Number must both be provided when either is present."
                    })
                    is_valid.at[idx] = False

        # Length checks for UPC, EN, UNSPSC (only if non-blank)
        if upc_field and upc_field in df.columns:
            for idx, val in df[upc_field].items():
                s = _norm_str(val)
                if s and len(s) != 12:
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Item Master",
                        "Part Number": df.at[idx, "Part Number"],
                        "Field": upc_field,
                        "ErrorCode": "UPC_LENGTH",
                        "Message": "UPC must be 12 characters when provided."
                    })
                    is_valid.at[idx] = False

        if en_field and en_field in df.columns:
            for idx, val in df[en_field].items():
                s = _norm_str(val)
                if s and len(s) != 14:
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Item Master",
                        "Part Number": df.at[idx, "Part Number"],
                        "Field": en_field,
                        "ErrorCode": "EN_LENGTH",
                        "Message": "EN must be 14 characters when provided."
                    })
                    is_valid.at[idx] = False

        if unspsc_field and unspsc_field in df.columns:
            for idx, val in df[unspsc_field].items():
                s = _norm_str(val)
                if s and len(s) != 8:
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Item Master",
                        "Part Number": df.at[idx, "Part Number"],
                        "Field": unspsc_field,
                        "ErrorCode": "UNSPSC_LENGTH",
                        "Message": "UNSPSC must be 8 characters when provided."
                    })
                    is_valid.at[idx] = False

        # Product Status validation
        if allowed_statuses:
            # Use case-insensitive comparison
            allowed_lc = [s.lower() for s in allowed_statuses]
            if "Product Status" in df.columns:
                for idx, val in df["Product Status"].items():
                    s = _norm_str(val)
                    if not s:
                        # Product Status is required if in required_fields; already handled
                        continue
                    if s.lower() not in allowed_lc:
                        errors.append({
                            "Vendor": self.vendor,
                            "Section": "Item Master",
                            "Part Number": df.at[idx, "Part Number"],
                            "Field": "Product Status",
                            "ErrorCode": "BAD_STATUS",
                            "Message": f"Product Status '{s}' is not in allowed list."
                        })
                        is_valid.at[idx] = False

        # HazmatFlag
        if hazmat_field and hazmat_field in df.columns:
            for idx, val in df[hazmat_field].items():
                raw = val
                s = _norm_str(val)
                pn = df.at[idx, "Part Number"]

                if not s:
                    # Treat blank / None / NaN / 'None' as allowed
                    continue

                if s.lower() not in allowed_hazmat_values:
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Item Master",
                        "Part Number": pn,
                        "Field": hazmat_field,
                        "ErrorCode": "BAD_HAZMAT_FLAG",
                        "Message": f"HazmatFlag '{raw}' is not allowed."
                    })
                    is_valid.at[idx] = False
                    continue

                # If Hazmat is truthy (Y/Yes) → UN number required
                if s.lower() in hazmat_truthy_values and un_number_field and un_number_field in df.columns:
                    un_val = _norm_str(df.at[idx, un_number_field])
                    if not un_val:
                        errors.append({
                            "Vendor": self.vendor,
                            "Section": "Item Master",
                            "Part Number": pn,
                            "Field": un_number_field,
                            "ErrorCode": "MISSING_UN_NUMBER",
                            "Message": "UN Number is required when HazmatFlag is set."
                        })
                        is_valid.at[idx] = False

        return is_valid

    # =====================================================
    # DESCRIPTIONS
    # =====================================================
    def _validate_descriptions(
        self,
        item_master: pd.DataFrame,
        df_desc: pd.DataFrame,
        cfg: Dict[str, Any],
        errors: ErrorList
    ) -> pd.Series:

        code_field = cfg.get("code_field", "Description Code")
        value_field = cfg.get("value_field", "Description Value")
        seq_field = cfg.get("sequence_field", "Sequence")

        max_length_codes = cfg.get("max_length_codes", {})
        primary_codes = cfg.get("primary_codes", [])
        must_xor = cfg.get("must_have_exactly_one_primary", False)
        require_des_for_all = cfg.get("require_des_for_all_skus", False)
        allow_other_codes = cfg.get("allow_other_codes", True)

        is_valid = pd.Series(True, index=df_desc.index)

        # Per-row: description value required if code is given
        for idx, row in df_desc.iterrows():
            code = _norm_str(row.get(code_field))
            val = _norm_str(row.get(value_field))
            pn = row.get("Part Number")

            if code and not val:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Descriptions",
                    "Part Number": pn,
                    "Field": value_field,
                    "ErrorCode": "DESC_VALUE_REQUIRED",
                    "Message": "Description Value is required when Description Code is provided."
                })
                is_valid.at[idx] = False

            # Length check for specific codes
            if code in max_length_codes and val:
                max_len = int(max_length_codes[code])
                if len(val) > max_len:
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Descriptions",
                        "Part Number": pn,
                        "Field": value_field,
                        "ErrorCode": "DESC_TOO_LONG",
                        "Message": f"{code} exceeds {max_len} characters."
                    })
                    is_valid.at[idx] = False

        # Sequence numeric check (optional but numeric when present)
        if seq_field and seq_field in df_desc.columns:
            for idx, val in df_desc[seq_field].items():
                s = _norm_str(val)
                if not s:
                    continue  # blank allowed
                if not s.isdigit():
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Descriptions",
                        "Part Number": df_desc.at[idx, "Part Number"],
                        "Field": seq_field,
                        "ErrorCode": "SEQ_NOT_NUMERIC",
                        "Message": "Sequence must be numeric when provided."
                    })
                    is_valid.at[idx] = False

        # Per-SKU: XOR logic on primary codes
        if primary_codes and must_xor:
            sku_groups = df_desc.groupby("Part Number")
            primary_lc = [p.lower() for p in primary_codes]

            for pn, grp in sku_groups:
                codes = grp[code_field].dropna().astype(str).str.strip()
                codes_lc = codes.str.lower()

                count_primary = codes_lc.isin(primary_lc).sum()
                if count_primary != 1:
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Descriptions",
                        "Part Number": pn,
                        "Field": code_field,
                        "ErrorCode": "PRIMARY_DESC_XOR",
                        "Message": f"Exactly one of {primary_codes} must be present for each SKU."
                    })
                    # mark all rows for that PN as invalid primary
                    idxs = grp.index
                    is_valid.loc[idxs] = False

        # DES required flag (classic DES code)
        if require_des_for_all:
            item_skus = set(item_master["Part Number"].dropna().unique())
            des_skus = set(
                df_desc.loc[df_desc[code_field].astype(str).str.upper() == "DES", "Part Number"]
                .dropna()
                .unique()
            )
            missing = item_skus - des_skus
            for pn in missing:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Descriptions",
                    "Part Number": pn,
                    "Field": value_field,
                    "ErrorCode": "MISSING_DES",
                    "Message": "Missing DES description."
                })

        return is_valid

    # =====================================================
    # EXTENDED INFO
    # =====================================================
    def _validate_extended_info(
        self,
        item_master: pd.DataFrame,
        df_ext: pd.DataFrame,
        cfg: Dict[str, Any],
        errors: ErrorList
    ) -> pd.Series:

        code_field = cfg.get("code_field", "Extended Info Code")
        value_field = cfg.get("value_field", "Extended Info Value")

        required_pairs = cfg.get("required_pairs", {})
        required_aliases = cfg.get("required_aliases", {})

        is_valid = pd.Series(True, index=df_ext.index)

        # Row-level: if a code is written, value is required and vice versa
        for idx, row in df_ext.iterrows():
            code = _norm_str(row.get(code_field))
            val = _norm_str(row.get(value_field))
            pn = row.get("Part Number")

            if code and not val:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Extended Info",
                    "Part Number": pn,
                    "Field": value_field,
                    "ErrorCode": "EXT_VALUE_REQUIRED",
                    "Message": f"Extended Info Value is required for code '{code}'."
                })
                is_valid.at[idx] = False
            elif val and not code:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Extended Info",
                    "Part Number": pn,
                    "Field": code_field,
                    "ErrorCode": "EXT_CODE_REQUIRED",
                    "Message": "Extended Info Code is required when a value is provided."
                })
                is_valid.at[idx] = False

        # Build alias map: alias → canonical
        alias_to_canonical: Dict[str, str] = {}
        for canonical, aliases in required_aliases.items():
            for a in aliases:
                alias_to_canonical[a.upper()] = canonical

        # Per-SKU check: required canonical codes must exist
        item_skus = set(item_master["Part Number"].dropna().unique())
        sku_groups = df_ext.groupby("Part Number")

        for pn in item_skus:
            if pn not in sku_groups.groups:
                # We'll handle missing required canonical codes below
                present_canonicals = set()
            else:
                grp = sku_groups.get_group(pn)
                present_canonicals = set()
                for code in grp[code_field].dropna().astype(str):
                    code_u = code.strip().upper()
                    canon = alias_to_canonical.get(code_u, code_u)
                    # Only count if value is non-blank
                    val = grp.loc[grp[code_field] == code, value_field].iloc[0]
                    if not _is_blank(val):
                        present_canonicals.add(canon)

            for canonical, req in required_pairs.items():
                if req == "required" and canonical.upper() not in {c.upper() for c in present_canonicals}:
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Extended Info",
                        "Part Number": pn,
                        "Field": code_field,
                        "ErrorCode": "MISSING_REQUIRED_EXT",
                        "Message": f"Missing required Extended Info code '{canonical}' (or its aliases)."
                    })

        return is_valid

    # =====================================================
    # ATTRIBUTES
    # =====================================================
    def _validate_attributes(
        self,
        df_attr: pd.DataFrame,
        cfg: Dict[str, Any],
        errors: ErrorList
    ) -> pd.Series:

        name_field = cfg.get("name_field", "Attribute Name")
        value_field = cfg.get("value_field", "Attribute Value")

        is_valid = pd.Series(True, index=df_attr.index)

        for idx, row in df_attr.iterrows():
            name = _norm_str(row.get(name_field))
            val = _norm_str(row.get(value_field))
            pn = row.get("Part Number")

            if name and not val:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Attributes",
                    "Part Number": pn,
                    "Field": value_field,
                    "ErrorCode": "ATTR_VALUE_REQUIRED",
                    "Message": "Attribute Value is required when Attribute Name is provided."
                })
                is_valid.at[idx] = False
            elif val and not name:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Attributes",
                    "Part Number": pn,
                    "Field": name_field,
                    "ErrorCode": "ATTR_NAME_REQUIRED",
                    "Message": "Attribute Name is required when Attribute Value is provided."
                })
                is_valid.at[idx] = False

        return is_valid

    # =====================================================
    # PART INTERCHANGE
    # =====================================================
    def _validate_part_interchange(
        self,
        df_pi: pd.DataFrame,
        cfg: Dict[str, Any],
        errors: ErrorList
    ) -> pd.Series:

        brand_field = cfg.get("brand_field", "Brand Label")
        part_field  = cfg.get("part_field", "Interchange Part Number")

        is_valid = pd.Series(True, index=df_pi.index)

        for idx, row in df_pi.iterrows():
            brand = _norm_str(row.get(brand_field))
            part  = _norm_str(row.get(part_field))
            pn    = row.get("Part Number")

            if brand and not part:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Part InterChange",
                    "Part Number": pn,
                    "Field": part_field,
                    "ErrorCode": "INTERCHANGE_PART_REQUIRED",
                    "Message": "Interchange Part Number is required when Brand Label is provided."
                })
                is_valid.at[idx] = False
            elif part and not brand:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Part InterChange",
                    "Part Number": pn,
                    "Field": brand_field,
                    "ErrorCode": "INTERCHANGE_BRAND_REQUIRED",
                    "Message": "Brand Label is required when Interchange Part Number is provided."
                })
                is_valid.at[idx] = False

        return is_valid

    # =====================================================
    # PACKAGES
    # =====================================================
    def _validate_packages_empty(
        self,
        item_master: pd.DataFrame,
        df_pkg: pd.DataFrame | None,
        cfg: Dict[str, Any],
        errors: ErrorList
    ):
        """
        Handle the case where df_pkg is None or empty.
        """
        if not cfg:
            return

        require_at_least_one = cfg.get("require_at_least_one_row", False)
        if require_at_least_one and (df_pkg is None or df_pkg.empty):
            # For now, treat as per-SKU requirement
            for pn in item_master["Part Number"].dropna().unique():
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Packages",
                    "Part Number": pn,
                    "Field": "Package UOM",
                    "ErrorCode": "MISSING_PACKAGE",
                    "Message": "At least one package row is required for this SKU."
                })

    def _validate_packages(
        self,
        item_master: pd.DataFrame,
        df_pkg: pd.DataFrame,
        cfg: Dict[str, Any],
        errors: ErrorList
    ) -> pd.Series:

        uom_field = cfg.get("uom_field", "Package UOM")
        allowed_uom = [u.lower() for u in cfg.get("allowed_uom", [])]
        qty_field = cfg.get("qty_field", "Package Quantity of Eaches")
        weight_uom_field = cfg.get("weight_uom_field", "Weight UOM")
        weight_field = cfg.get("weight_field", "Weight")
        dim_uom_field = cfg.get("dim_uom_field", "Dimension UOM")
        merch_len_field = cfg.get("merch_len_field", "Merch Length")
        merch_wid_field = cfg.get("merch_wid_field", "Merch Width")
        merch_hgt_field = cfg.get("merch_hgt_field", "Merch Height")
        content_field = cfg.get("content_field")  # may be None (optional)

        is_valid = pd.Series(True, index=df_pkg.index)

        for idx, row in df_pkg.iterrows():
            pn = row.get("Part Number")
            uom = _norm_str(row.get(uom_field))

            if not uom:
                # no package UOM → skip all other rules
                continue

            # UOM validity (case insensitive)
            if allowed_uom and uom.lower() not in allowed_uom:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Packages",
                    "Part Number": pn,
                    "Field": uom_field,
                    "ErrorCode": "BAD_PACKAGE_UOM",
                    "Message": f"Package UOM '{uom}' is not in allowed list."
                })
                is_valid.at[idx] = False

            # If UOM provided, these fields must be present
            for f in [qty_field, weight_uom_field, weight_field, dim_uom_field,
                      merch_len_field, merch_wid_field, merch_hgt_field]:
                if f and f in df_pkg.columns:
                    val = _norm_str(row.get(f))
                    if not val:
                        errors.append({
                            "Vendor": self.vendor,
                            "Section": "Packages",
                            "Part Number": pn,
                            "Field": f,
                            "ErrorCode": "PACKAGE_FIELD_REQUIRED",
                            "Message": f"Field '{f}' is required."
                        })
                        is_valid.at[idx] = False

            # Package content (optional by config)
            if content_field:
                val = _norm_str(row.get(content_field))
                if not val:
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Packages",
                        "Part Number": pn,
                        "Field": content_field,
                        "ErrorCode": "PACKAGE_CONTENT_REQUIRED",
                        "Message": "Package Content is required."
                    })
                    is_valid.at[idx] = False

        return is_valid

    # =====================================================
    # PRICING
    # =====================================================
    def _clean_pricing(self, df: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
        if df is None or df.empty:
            return df

        df = df.copy()

        # Clean numeric price-like fields
        # We'll infer from cfg: numeric_fields + numeric_positive_fields
        num_fields = set(cfg.get("numeric_fields", [])) | set(cfg.get("numeric_positive_fields", []))
        for col in num_fields:
            if col in df.columns:
                df[col] = df[col].apply(_clean_price_generic)

        # Also clean Discount % if present
        if "Discount %" in df.columns:
            df["Discount %"] = (
                df["Discount %"]
                .astype(str)
                .str.extract(r"(\d+)", expand=False)
                .apply(_to_float_or_none)
            )

        return df

    def _validate_pricing(
        self,
        item_master: pd.DataFrame,
        df_price: pd.DataFrame,
        cfg: Dict[str, Any],
        errors: ErrorList
    ) -> pd.Series:

        required_fields = cfg.get("required_fields", [])
        numeric_positive_fields = cfg.get("numeric_positive_fields", [])
        numeric_fields = cfg.get("numeric_fields", [])

        require_pricing_for_all_skus = cfg.get("require_pricing_for_all_skus", False)

        net_cost_field = cfg.get("net_cost_field", "Net Price")
        effective_date_field = cfg.get("effective_date_field", "Effective Date")

        moq_unit_field = cfg.get("moq_unit_field", "MOQ Unit")
        moq_field = cfg.get("moq_field", "MOQ")
        currency_field = cfg.get("currency_field", "Currency")

        promo_fields = cfg.get("promo_fields", {})
        quote_fields = cfg.get("quote_fields", {})
        tender_fields = cfg.get("tender_fields", {})
        core_fields = cfg.get("core_fields", {})

        is_valid = pd.Series(True, index=df_price.index)

        # Ensure Part Number exists
        if "Part Number" not in df_price.columns:
            raise KeyError("Pricing must contain 'Part Number' column")

        # Required fields
        for field in required_fields:
            if field not in df_price.columns:
                # Column missing entirely
                for idx, row in df_price.iterrows():
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Pricing",
                        "Part Number": row.get("Part Number"),
                        "Field": field,
                        "ErrorCode": "MISSING_COLUMN",
                        "Message": f"Required pricing column '{field}' missing."
                    })
                is_valid[:] = False
                continue

            missing = df_price[field].apply(_is_blank)
            for idx in df_price[missing].index:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Pricing",
                    "Part Number": df_price.at[idx, "Part Number"],
                    "Field": field,
                    "ErrorCode": "REQUIRED_FIELD_EMPTY",
                    "Message": f"Missing required pricing field '{field}'."
                })
            is_valid[missing] = False

        # Numeric positive fields
        for field in numeric_positive_fields:
            if field not in df_price.columns:
                continue
            vals = df_price[field].apply(_to_float_or_none)
            invalid = vals.isna() | (vals <= 0)
            for idx in df_price[invalid].index:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Pricing",
                    "Part Number": df_price.at[idx, "Part Number"],
                    "Field": field,
                    "ErrorCode": "NON_POSITIVE_VALUE",
                    "Message": f"Field '{field}' must be > 0."
                })
                is_valid.at[idx] = False

        # Numeric fields generic
        for field in numeric_fields:
            if field not in df_price.columns:
                continue
            vals = df_price[field].apply(_to_float_or_none)
            nonnum = vals.isna() & df_price[field].notna() & ~df_price[field].astype(str).str.strip().eq("")
            for idx in df_price[nonnum].index:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Pricing",
                    "Part Number": df_price.at[idx, "Part Number"],
                    "Field": field,
                    "ErrorCode": "NOT_NUMERIC",
                    "Message": f"Field '{field}' must be numeric."
                })
                is_valid.at[idx] = False

        # Require pricing for all SKUs (if enabled)
        if require_pricing_for_all_skus:
            item_skus = set(item_master["Part Number"].dropna().unique())
            price_skus = set(df_price["Part Number"].dropna().unique())
            missing_skus = item_skus - price_skus
            for pn in missing_skus:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Pricing",
                    "Part Number": pn,
                    "Field": "Part Number",
                    "ErrorCode": "MISSING_PRICING",
                    "Message": "Missing pricing record for this SKU."
                })

        # Net Cost + Effective Date required per SKU
        for idx, row in df_price.iterrows():
            pn = row.get("Part Number")

            net_val = _norm_str(row.get(net_cost_field)) if net_cost_field in df_price.columns else ""
            eff_val = _norm_str(row.get(effective_date_field)) if effective_date_field in df_price.columns else ""

            if net_cost_field and not net_val:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Pricing",
                    "Part Number": pn,
                    "Field": net_cost_field,
                    "ErrorCode": "NET_COST_REQUIRED",
                    "Message": "Net Cost is required for each SKU."
                })
                is_valid.at[idx] = False

            # MOQ Unit + MOQ
            if moq_unit_field:
                mu = _norm_str(row.get(moq_unit_field))
                if not mu:
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Pricing",
                        "Part Number": pn,
                        "Field": moq_unit_field,
                        "ErrorCode": "MOQ_UNIT_REQUIRED",
                        "Message": "Minimum Order Quantity Unit is required."
                    })
                    is_valid.at[idx] = False

            if moq_field:
                mv = _norm_str(row.get(moq_field))
                if not mv:
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Pricing",
                        "Part Number": pn,
                        "Field": moq_field,
                        "ErrorCode": "MOQ_REQUIRED",
                        "Message": "Minimum Order Quantity is required."
                    })
                    is_valid.at[idx] = False

            # Currency required
            if currency_field:
                cur = _norm_str(row.get(currency_field))
                if not cur:
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Pricing",
                        "Part Number": pn,
                        "Field": currency_field,
                        "ErrorCode": "CURRENCY_REQUIRED",
                        "Message": "Currency is required."
                    })
                    is_valid.at[idx] = False

            # Promo / Quote / Tender fields
            for label, fset in [
                ("PROMO", promo_fields),
                ("QUOTE", quote_fields),
                ("TENDER", tender_fields),
            ]:
                if not fset:
                    continue
                price_col = fset.get("price")
                start_col = fset.get("start")
                end_col   = fset.get("end")

                price_val = _norm_str(row.get(price_col)) if price_col else ""
                start_val = _norm_str(row.get(start_col)) if start_col else ""
                end_val   = _norm_str(row.get(end_col)) if end_col else ""

                # If any of them is given, all are required
                any_given = bool(price_val or start_val or end_val)
                if any_given:
                    if not price_val:
                        errors.append({
                            "Vendor": self.vendor,
                            "Section": "Pricing",
                            "Part Number": pn,
                            "Field": price_col,
                            "ErrorCode": f"{label}_PRICE_REQUIRED",
                            "Message": f"{label} Price is required when its date range is provided."
                        })
                        is_valid.at[idx] = False
                    if not start_val:
                        errors.append({
                            "Vendor": self.vendor,
                            "Section": "Pricing",
                            "Part Number": pn,
                            "Field": start_col,
                            "ErrorCode": f"{label}_START_REQUIRED",
                            "Message": f"{label} Start Date is required when {label} pricing is provided."
                        })
                        is_valid.at[idx] = False
                    if not end_val:
                        errors.append({
                            "Vendor": self.vendor,
                            "Section": "Pricing",
                            "Part Number": pn,
                            "Field": end_col,
                            "ErrorCode": f"{label}_END_REQUIRED",
                            "Message": f"{label} End Date is required when {label} pricing is provided."
                        })
                        is_valid.at[idx] = False

            # Core Price logic
            if core_fields:
                core_price_col = core_fields.get("price")
                core_part_col  = core_fields.get("part")
                core_cost_col  = core_fields.get("cost")

                cp = _norm_str(row.get(core_price_col)) if core_price_col else ""
                cpn = _norm_str(row.get(core_part_col)) if core_part_col else ""
                cc = _norm_str(row.get(core_cost_col)) if core_cost_col else ""

                any_core = bool(cp or cpn or cc)
                if any_core:
                    if not cp:
                        errors.append({
                            "Vendor": self.vendor,
                            "Section": "Pricing",
                            "Part Number": pn,
                            "Field": core_price_col,
                            "ErrorCode": "CORE_PRICE_REQUIRED",
                            "Message": "Core Price is required when any core information is provided."
                        })
                        is_valid.at[idx] = False
                    if not cpn:
                        errors.append({
                            "Vendor": self.vendor,
                            "Section": "Pricing",
                            "Part Number": pn,
                            "Field": core_part_col,
                            "ErrorCode": "CORE_PART_REQUIRED",
                            "Message": "Core Part Number is required when any core information is provided."
                        })
                        is_valid.at[idx] = False
                    if not cc:
                        errors.append({
                            "Vendor": self.vendor,
                            "Section": "Pricing",
                            "Part Number": pn,
                            "Field": core_cost_col,
                            "ErrorCode": "CORE_COST_REQUIRED",
                            "Message": "Core Cost is required when any core information is provided."
                        })
                        is_valid.at[idx] = False

        return is_valid

    # =====================================================
    # DIGITAL ASSETS
    # =====================================================
    def _validate_assets_empty(
        self,
        item_master: pd.DataFrame,
        df_assets: pd.DataFrame | None,
        cfg: Dict[str, Any],
        errors: ErrorList
    ):
        if not cfg:
            return
        require_all = cfg.get("require_asset_for_all_skus", False)
        if require_all and (df_assets is None or df_assets.empty):
            for pn in item_master["Part Number"].dropna().unique():
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Digital Assets",
                    "Part Number": pn,
                    "Field": "FileName",
                    "ErrorCode": "MISSING_ASSET",
                    "Message": "No digital assets found for this SKU."
                })

    def _validate_assets(
        self,
        item_master: pd.DataFrame,
        df_assets: pd.DataFrame,
        cfg: Dict[str, Any],
        errors: ErrorList
    ) -> pd.Series:

        require_all = cfg.get("require_asset_for_all_skus", False)
        file_field = cfg.get("file_field", "FileName")
        media_field = cfg.get("media_field", "MediaType")
        acceptable_media = [m.upper() for m in cfg.get("acceptable_primary_media", [])]

        is_valid = pd.Series(True, index=df_assets.index)

        # Per-row filename required
        for idx, row in df_assets.iterrows():
            pn = row.get("Part Number")
            fn = _norm_str(row.get(file_field))
            if not fn:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Digital Assets",
                    "Part Number": pn,
                    "Field": file_field,
                    "ErrorCode": "FILENAME_REQUIRED",
                    "Message": "Filename is required for each asset."
                })
                is_valid.at[idx] = False

        # Per-SKU: At least one acceptable primary media must exist
        if media_field in df_assets.columns and acceptable_media:
            sku_groups = df_assets.groupby("Part Number")
            for pn, grp in sku_groups:
                media_types = (
                    grp[media_field]
                    .dropna()
                    .astype(str)
                    .str.strip()
                    .str.upper()
                    .unique()
                )

                media_set = set(media_types)

                # Check if SKU contains ANY acceptable primary media
                if not any(m in media_set for m in acceptable_media):
                    errors.append({
                        "Vendor": self.vendor,
                        "Section": "Digital Assets",
                        "Part Number": pn,
                        "Field": media_field,
                        "ErrorCode": "MISSING_PRIMARY_MEDIA",
                        "Message": (
                            f"SKU is missing all acceptable primary media types: "
                            f"{', '.join(acceptable_media)}."
                        )
                    })

        # Assets required for all SKUs?
        if require_all:
            item_skus = set(item_master["Part Number"].dropna().unique())
            asset_skus = set(df_assets["Part Number"].dropna().unique())
            missing_skus = item_skus - asset_skus
            for pn in missing_skus:
                errors.append({
                    "Vendor": self.vendor,
                    "Section": "Digital Assets",
                    "Part Number": pn,
                    "Field": file_field,
                    "ErrorCode": "MISSING_ASSET",
                    "Message": "No digital assets found for this SKU."
                })

        return is_valid
