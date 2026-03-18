# vendor_adapters/vendor_factory.py

from mapping_validation.vendor_adapters.grote_adapter import GroteAdapter
from mapping_validation.vendor_adapters.rigid_adapter import RigidAdapter
from mapping_validation.vendor_adapters.dayton_adapter import DaytonAdapter


class VendorAdapterFactory:

    @staticmethod
    def create(
        vendor_name: str,
        mapping: dict,
        workflow: str,
        submission_type: str,
        submission_id: str,
    ):

        # ------------------------------------
        # RULE 0: PRICING WORKFLOW (force path)
        # ------------------------------------
        if workflow == "pricing":
            return GroteAdapter(
                vendor=vendor_name,
                mapping=mapping,
                workflow=workflow,
                submission_type=submission_type,
                submission_id=submission_id
            )

        # ------------------------------------
        # PRODUCT WORKFLOW (existing logic)
        # ------------------------------------
        pm = mapping.get("product_mapping", {})

        item_master_cfg = pm.get("Item Master", {})
        src_val = str(item_master_cfg.get("source", "")).lower()

        if "xlsx" in src_val or "excel" in src_val:
            return RigidAdapter(
                vendor=vendor_name,
                mapping=mapping,
                workflow=workflow,
                submission_type=submission_type,
                submission_id=submission_id
            )

        if vendor_name.lower().startswith("dayton"):
            return DaytonAdapter(
                vendor=vendor_name,
                mapping=mapping,
                workflow=workflow,
                submission_type=submission_type,
                submission_id=submission_id
            )

        return GroteAdapter(
            vendor=vendor_name,
            mapping=mapping,
            workflow=workflow,
            submission_type=submission_type,
            submission_id=submission_id
        )