# vendor_adapters/vendor_factory.py

from mapping_validation.vendor_adapters.grote_adapter import GroteAdapter
from mapping_validation.vendor_adapters.rigid_adapter import RigidAdapter
from mapping_validation.vendor_adapters.dayton_adapter import DaytonAdapter


class VendorAdapterFactory:

    @staticmethod
    def create(
        vendor_name: str,
        mapping: dict,
        submission_id: str,
        workflow: str
    ):

        pm = mapping.get("product_mapping", {})

        item_master_cfg = pm.get("Item Master", {})
        src_val = str(item_master_cfg.get("source", "")).lower()

        # ------------------------------------
        # RULE 1: Excel Vendor (non-OptiCat)
        # ------------------------------------
        if "xlsx" in src_val or "excel" in src_val:
            return RigidAdapter(
                vendor=vendor_name,
                mapping=mapping,
                submission_id=submission_id,
                workflow=workflow
            )

        if vendor_name.lower().startswith("dayton"):
            return DaytonAdapter(
                vendor=vendor_name,
                mapping=mapping,
                submission_id=submission_id,
                workflow=workflow
            )

        return GroteAdapter(
            vendor=vendor_name,
            mapping=mapping,
            submission_id=submission_id,
            workflow=workflow
        )