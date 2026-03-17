# vendor_adapters/base_adapter.py

from __future__ import annotations
from typing import Dict, Any
import pandas as pd


class BaseVendorAdapter:
    """
    Abstract adapter used by all vendor-specific implementations.
    """

    def __init__(self, vendor: str, mapping: dict, submission_id: str, workflow: str):
        self.vendor = vendor
        self.mapping = mapping
        self.pm = mapping.get("product_mapping", {}) or {}
        self.submission_id = submission_id
        self.workflow = workflow

    # -----------------------------------------------------------------
    # PRODUCT MAPPING (XML or Excel). Child classes override this.
    # -----------------------------------------------------------------
    def load_product(self) -> Dict[str, pd.DataFrame]:
        raise NotImplementedError("load_product() must be implemented by subclasses.")

    # -----------------------------------------------------------------
    # PRICING MAPPING (XML or Excel pricing). Child classes override.
    # -----------------------------------------------------------------
    def load_pricing(self) -> Dict[str, pd.DataFrame]:
        raise NotImplementedError("load_pricing() must be implemented by subclasses.")

    # -----------------------------------------------------------------
    # OPTIONAL VENDOR-SPECIFIC FIXES
    # -----------------------------------------------------------------
    def post_process(self, combined: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """
        Vendors like Grote or Rigid can override this to:
        - Add special cleanup logic
        - Enrich fields
        - Apply fixes
        """
        return combined

    # -----------------------------------------------------------------
    # FULL PIPELINE FOR A SINGLE VENDOR
    # -----------------------------------------------------------------
    def process(self) -> Dict[str, pd.DataFrame]:
        from mapping_validation.scripts.pre_etl_ingest_mapping import log_ingestion_error

        try:
            product = self.load_product()
        except Exception as e:
            log_ingestion_error(
                vendor=self.vendor,
                stage="PRODUCT_LOAD_ERROR",
                file=None,
                error=e,
                submission_id=self.submission_id
            )
            raise RuntimeError("PRODUCT_LOAD_ERROR")

        try:
            pricing = None

            if self.workflow == "pricing":
                pricing = self.load_pricing()

        except Exception as e:
            log_ingestion_error(
                vendor=self.vendor,
                stage="PRICING_LOAD_ERROR",
                file=None,
                error=e,
                submission_id=self.submission_id
            )
            raise RuntimeError("PRICING_LOAD_ERROR")

        combined = {**(product or {}), **(pricing or {})}

        return self.post_process(combined)


