# mapping_engine/xml_mapper.py

from __future__ import annotations

from typing import Dict, Any, List
import pandas as pd
from lxml import etree


def normalize_ns(root: etree._Element) -> Dict[str, str]:
    """
    Convert default namespace into 'ns' prefix.
    Required for all PIES XML.
    """
    nsmap = root.nsmap.copy() if root.nsmap else {}

    if None in nsmap:
        nsmap["ns"] = nsmap.pop(None)

    return nsmap


def _extract_xpath_single(node: etree._Element, expr: str, nsmap: Dict[str, str]) -> Any:
    """
    Evaluate an XPath against a node and return a single scalar value (or None).
    """
    res = node.xpath(expr, namespaces=nsmap)
    if not res:
        return None
    val = res[0]
    if hasattr(val, "text"):
        return val.text
    return str(val)


class XMLMapper:
    """
    XML mapping helper for OptiCat / PIES-style XML.

    Responsibilities:
      - Normalize namespaces
      - Map Item Master
      - Map generic XML sections (Descriptions, Extended Info, etc.)
    """

    def __init__(self, root: etree._Element):
        self.root = root
        self.nsmap = normalize_ns(root)

    # -------------------------------
    # ITEM MASTER MAPPING
    # -------------------------------
    def map_item_master(self, section_cfg: Dict[str, Any]) -> pd.DataFrame:
        items = self.root.xpath(".//ns:Items/ns:Item", namespaces=self.nsmap)
        rows: List[Dict[str, Any]] = []

        for item in items:
            row: Dict[str, Any] = {}

            for col, expr in section_cfg.items():
                if expr is None:
                    row[col] = None
                    continue

                if not isinstance(expr, str):
                    row[col] = None
                    continue

                if expr.startswith("@"):
                    row[col] = item.get(expr[1:])

                elif ":" not in expr and "/" not in expr:
                    node = item.find(f"ns:{expr}", namespaces=self.nsmap)
                    row[col] = node.text if node is not None else None

                else:
                    row[col] = _extract_xpath_single(item, expr, self.nsmap)
            
            # --------------------------------------------------
            # 🔑 IDENTIFIER NORMALIZATION (CRITICAL)
            # --------------------------------------------------
            if "Part Number" in row and row["Part Number"] is not None:
                row["Part Number"] = str(row["Part Number"]).strip()


            # --------------------------------------------------
            # QUANTITY FALLBACK (PIES / OPTICAT SAFE)
            # --------------------------------------------------
            if not row.get("Quantity Size"):
                row["Quantity Size"] = row.get("Minimum Order Quantity")

            if not row.get("Quantity UOM"):
                row["Quantity UOM"] = row.get("Minimum Order Quantity UOM")

            # Normalize
            if row.get("Quantity Size") is not None:
                try:
                    row["Quantity Size"] = float(row["Quantity Size"])
                except ValueError:
                    pass

            if row.get("Quantity UOM"):
                row["Quantity UOM"] = str(row["Quantity UOM"]).strip()

            rows.append(row)

        return pd.DataFrame(rows)

    # -------------------------------
    # GENERIC SECTION MAPPER
    # -------------------------------
    def map_xml_section(self, path: str, mappings: Dict[str, Any]) -> pd.DataFrame:
        """
        Generic mapper for sections like:
          - Descriptions
          - Extended Info
          - Attributes
          - Part InterChange
          - Packages
          - Digital Assets

        path is a relative XPath like:
            "ns:Descriptions/ns:Description"

        mappings is YAML dict:
            { "Part Number": "ancestor::ns:Item/ns:PartNumber", ... }
        """
        nodes = self.root.xpath(".//" + path, namespaces=self.nsmap)
        rows: List[Dict[str, Any]] = []

        for node in nodes:
            row: Dict[str, Any] = {}

            for col, expr in mappings.items():
                if expr is None:
                    row[col] = None
                    continue

                if expr == "text":
                    row[col] = node.text
                elif isinstance(expr, str) and expr.startswith("@"):
                    row[col] = node.get(expr[1:])
                else:
                    row[col] = _extract_xpath_single(node, expr, self.nsmap)

            rows.append(row)

        return pd.DataFrame(rows)
