"""
Validation logic for single product submissions in FGI Vendor Portal
"""
from datetime import datetime
from helpers.lookups import (
    VENDOR_LIST, PRODUCT_STATUS, QUANTITY_UOM, PRICING_METHODS
)


def validate_single_product_new(form: dict) -> tuple[bool, list[str]]:
    """
    Validate single product form submission with multi-level pricing support.
    
    Args:
        form (dict): Flask request.form containing all form data
        
    Returns:
        tuple: (is_valid: bool, errors: list[str])
    """
    errors = []

    vendor = form.get("vendor_name", "").strip()
    sku = form.get("sku", "").strip()
    unspsc = form.get("unspsc_code", "").strip()
    hazmat = form.get("hazmat_flag", "").strip()
    status = form.get("product_status", "").strip()
    barcode_type = form.get("barcode_type", "").strip()
    barcode_number = form.get("barcode_number", "").strip()
    quantity_uom = form.get("quantity_uom", "").strip()
    quantity_size = form.get("quantity_size", "").strip()
    vmrs = form.get("vmrs_code", "").strip()

    # Required basic
    if not vendor:
        errors.append("Vendor is required.")
    elif vendor not in VENDOR_LIST:
        errors.append(f"Vendor '{vendor}' is not in the allowed list.")

    if not sku:
        errors.append("Part Number (SKU) is required.")

    # UNSPSC
    if unspsc:
        if not unspsc.isdigit() or len(unspsc) != 8:
            errors.append("UNSPSC Code must be exactly 8 numeric digits if provided.")

    # Hazmat
    if hazmat and hazmat not in ["Y", "N"]:
        errors.append("Hazardous Material must be Y or N.")

    # Status
    if status and status not in PRODUCT_STATUS:
        errors.append("Product Status must be one of: " + ", ".join(PRODUCT_STATUS))

    # Barcode
    if barcode_number:
        if not barcode_number.isdigit():
            errors.append("Barcode Number must be numeric.")
        else:
            if barcode_type == "UPC" and len(barcode_number) != 12:
                errors.append("UPC Barcode must be exactly 12 digits.")
            elif barcode_type == "EAN" and len(barcode_number) != 14:
                errors.append("EAN Barcode must be exactly 14 digits.")
            elif not barcode_type and len(barcode_number) not in (12, 14):
                errors.append("Barcode must be 12-digit UPC or 14-digit EAN.")

    if barcode_type and barcode_type not in ["UPC", "EAN"]:
        errors.append("Barcode Type must be UPC or EAN.")

    # Quantity / UOM
    if quantity_uom and quantity_uom not in QUANTITY_UOM:
        errors.append("Quantity Size Unit must be one of: " + ", ".join(QUANTITY_UOM))

    if quantity_size:
        try:
            float(quantity_size)
        except ValueError:
            errors.append("Quantity Size must be numeric.")

    # --- Descriptions: check lengths for DES/SHO ---
    desc_codes = form.getlist("desc_code[]")
    desc_values = form.getlist("desc_value[]")

    for code, value in zip(desc_codes, desc_values):
        code = (code or "").strip()
        value = (value or "").strip()
        if code in ("DES", "SHO") and value and len(value) > 40:
            errors.append(f"Description with code {code} must be <= 40 characters.")

    # --- MULTI-LEVEL PRICING VALIDATION ---
    level_types = form.getlist("level_type[]")
    level_price_change_types = form.getlist("level_price_change_type[]")
    level_moq_uoms = form.getlist("level_moq_uom[]")
    level_moq_qtys = form.getlist("level_moq_qty[]")
    level_currencies = form.getlist("level_currency[]")
    level_methods = form.getlist("level_pricing_method[]")

    # Method-specific arrays
    net_list = form.getlist("level_net_list_price[]")
    net_costs = form.getlist("level_net_net_cost[]")
    net_effective_dates = form.getlist("level_net_effective_date[]")

    pl_list = form.getlist("level_pl_list_price[]")
    pl_jobber = form.getlist("level_pl_jobber_price[]")
    pl_net = form.getlist("level_pl_net_cost[]")
    pl_effective_dates = form.getlist("level_pl_effective_date[]")

    db_base = form.getlist("level_db_base_price[]")
    db_discount = form.getlist("level_db_discount_pct[]")
    db_list_opt = form.getlist("level_db_list_price_opt[]")
    db_effective_dates = form.getlist("level_db_effective_date[]")

    ehc_base = form.getlist("level_ehc_base_price[]")
    ehc_cb = form.getlist("level_ehc_canadian_blue[]")
    ehc_qty_case = form.getlist("level_ehc_qty_case[]")
    ehc_upc_each = form.getlist("level_ehc_upc_each[]")
    ehc_upc_case = form.getlist("level_ehc_upc_case[]")
    ehc_moq = form.getlist("level_ehc_moq[]")

    ehc_abmbsk_each = form.getlist("level_ehc_abmbsk_each[]")
    ehc_abmbsk_case = form.getlist("level_ehc_abmbsk_case[]")
    ehc_bc_each = form.getlist("level_ehc_bc_each[]")
    ehc_bc_case = form.getlist("level_ehc_bc_case[]")
    ehc_nl_each = form.getlist("level_ehc_nl_each[]")
    ehc_nl_case = form.getlist("level_ehc_nl_case[]")
    ehc_ns_each = form.getlist("level_ehc_ns_each[]")
    ehc_ns_case = form.getlist("level_ehc_ns_case[]")
    ehc_nbqc_each = form.getlist("level_ehc_nbqc_each[]")
    ehc_nbqc_case = form.getlist("level_ehc_nbqc_case[]")
    ehc_pei_each = form.getlist("level_ehc_pei_each[]")
    ehc_pei_case = form.getlist("level_ehc_pei_case[]")
    ehc_yk_each = form.getlist("level_ehc_yk_each[]")
    ehc_yk_case = form.getlist("level_ehc_yk_case[]")

    pr_price = form.getlist("level_pr_promo_price[]")
    pr_start = form.getlist("level_pr_start_date[]")
    pr_end = form.getlist("level_pr_end_date[]")

    qt_price = form.getlist("level_qt_price[]")
    qt_number = form.getlist("level_qt_number[]")
    qt_start = form.getlist("level_qt_start_date[]")
    qt_end = form.getlist("level_qt_end_date[]")

    td_price = form.getlist("level_td_price[]")
    td_number = form.getlist("level_td_number[]")
    td_start = form.getlist("level_td_start_date[]")
    td_end = form.getlist("level_td_end_date[]")

    level_tier_min_qtys = form.getlist("level_tier_min_qty[]")
    level_tier_max_qtys = form.getlist("level_tier_max_qty[]")

    n_levels = len(level_types)

    if n_levels == 0:
        errors.append("At least one pricing level is required.")
        return (len(errors) == 0, errors)

    def pad(lst):
        return lst + [""] * (n_levels - len(lst))

    # Make sure all arrays have same length to avoid IndexError
    level_price_change_types = pad(level_price_change_types)
    level_moq_uoms = pad(level_moq_uoms)
    level_moq_qtys = pad(level_moq_qtys)
    level_currencies = pad(level_currencies)
    level_methods = pad(level_methods)
    net_list = pad(net_list)
    net_costs = pad(net_costs)
    net_effective_dates = pad(net_effective_dates)
    pl_list = pad(pl_list)
    pl_jobber = pad(pl_jobber)
    pl_net = pad(pl_net)
    pl_effective_dates = pad(pl_effective_dates)
    db_base = pad(db_base)
    db_discount = pad(db_discount)
    db_list_opt = pad(db_list_opt)
    db_effective_dates = pad(db_effective_dates)
    ehc_base = pad(ehc_base)
    ehc_cb = pad(ehc_cb)
    ehc_qty_case = pad(ehc_qty_case)
    ehc_upc_each = pad(ehc_upc_each)
    ehc_upc_case = pad(ehc_upc_case)
    ehc_moq = pad(ehc_moq)
    ehc_abmbsk_each = pad(ehc_abmbsk_each)
    ehc_abmbsk_case = pad(ehc_abmbsk_case)
    ehc_bc_each = pad(ehc_bc_each)
    ehc_bc_case = pad(ehc_bc_case)
    ehc_nl_each = pad(ehc_nl_each)
    ehc_nl_case = pad(ehc_nl_case)
    ehc_ns_each = pad(ehc_ns_each)
    ehc_ns_case = pad(ehc_ns_case)
    ehc_nbqc_each = pad(ehc_nbqc_each)
    ehc_nbqc_case = pad(ehc_nbqc_case)
    ehc_pei_each = pad(ehc_pei_each)
    ehc_pei_case = pad(ehc_pei_case)
    ehc_yk_each = pad(ehc_yk_each)
    ehc_yk_case = pad(ehc_yk_case)
    pr_price = pad(pr_price)
    pr_start = pad(pr_start)
    pr_end = pad(pr_end)
    qt_price = pad(qt_price)
    qt_number = pad(qt_number)
    qt_start = pad(qt_start)
    qt_end = pad(qt_end)
    td_price = pad(td_price)
    td_number = pad(td_number)
    td_start = pad(td_start)
    td_end = pad(td_end)
    level_tier_min_qtys = pad(level_tier_min_qtys)
    level_tier_max_qtys = pad(level_tier_max_qtys)

    any_level_used = False
    today_str = datetime.now().strftime("%Y-%m-%d")

    for i in range(n_levels):
        lvl_type = (level_types[i] or "").strip()
        lvl_method = (level_methods[i] or "").strip()
        lvl_cur = (level_currencies[i] or "").strip()
        lvl_moq_uom = (level_moq_uoms[i] or "").strip()
        lvl_moq_qty = (level_moq_qtys[i] or "").strip()
        lvl_change = (level_price_change_types[i] or "").strip() or "A"
        row_label = f"Pricing Level {i+1}"

        # Check if row completely blank
        if not (
            lvl_type or lvl_method or lvl_cur or lvl_moq_uom or lvl_moq_qty
            or net_list[i] or net_costs[i] or net_effective_dates[i]
            or pl_list[i] or pl_jobber[i] or pl_net[i] or pl_effective_dates[i]
            or db_base[i] or db_discount[i] or db_effective_dates[i]
            or ehc_base[i] or pr_price[i] or qt_price[i] or td_price[i]
        ):
            continue

        any_level_used = True

        # Level Type
        if not lvl_type:
            errors.append(f"{row_label}: Level Type is required.")
        elif lvl_type not in ["Each", "Case", "Pallet", "Bulk"]:
            errors.append(f"{row_label}: Level Type must be Each, Case, Pallet, or Bulk.")

        # Pricing Method
        if not lvl_method:
            errors.append(f"{row_label}: Pricing Method is required.")
        elif lvl_method not in PRICING_METHODS.keys():
            errors.append(f"{row_label}: Invalid Pricing Method selected.")

        # Currency
        if not lvl_cur:
            errors.append(f"{row_label}: Currency is required.")
        elif lvl_cur not in ["CAD", "USD"]:
            errors.append(f"{row_label}: Currency must be CAD or USD.")

        # MOQ
        if lvl_moq_qty:
            try:
                float(lvl_moq_qty)
            except ValueError:
                errors.append(f"{row_label}: MOQ must be numeric.")
        if lvl_moq_uom and lvl_moq_uom not in QUANTITY_UOM:
            errors.append(f"{row_label}: MOQ Unit must be a valid unit ({', '.join(QUANTITY_UOM)}).")

        # Tier Min / Max
        tmin = (level_tier_min_qtys[i] or "").strip()
        tmax = (level_tier_max_qtys[i] or "").strip()
        for val, label_val in [(tmin, "Tier Min Qty"), (tmax, "Tier Max Qty")]:
            if val:
                try:
                    float(val)
                except ValueError:
                    errors.append(f"{row_label}: {label_val} must be numeric.")

        # Method-specific checks
        if lvl_method == "net_cost":
            nl = (net_list[i] or "").strip()
            nc = (net_costs[i] or "").strip()
            ne = (net_effective_dates[i] or "").strip()

            if not nl:
                errors.append(f"{row_label} (Net Cost): List Price is required.")
            else:
                try:
                    float(nl)
                except ValueError:
                    errors.append(f"{row_label} (Net Cost): List Price must be numeric.")

            if not nc:
                errors.append(f"{row_label} (Net Cost): Net Cost is required.")
            else:
                try:
                    float(nc)
                except ValueError:
                    errors.append(f"{row_label} (Net Cost): Net Cost must be numeric.")

            if not ne:
                errors.append(f"{row_label} (Net Cost): Effective Date is required.")
            elif ne < today_str:
                errors.append(f"{row_label} (Net Cost): Effective Date cannot be before today.")

        elif lvl_method == "price_levels":
            ll = (pl_list[i] or "").strip()
            lj = (pl_jobber[i] or "").strip()
            ln = (pl_net[i] or "").strip()
            le = (pl_effective_dates[i] or "").strip()

            for val, name in [(ll, "List Price"), (lj, "Jobber Price"), (ln, "Net Cost")]:
                if not val:
                    errors.append(f"{row_label} (Price Levels): {name} is required.")
                else:
                    try:
                        float(val)
                    except ValueError:
                        errors.append(f"{row_label} (Price Levels): {name} must be numeric.")

            if not le:
                errors.append(f"{row_label} (Price Levels): Effective Date is required.")
            elif le < today_str:
                errors.append(f"{row_label} (Price Levels): Effective Date cannot be before today.")

        elif lvl_method == "discount_based":
            dbb = (db_base[i] or "").strip()
            dbd = (db_discount[i] or "").strip()
            dbe = (db_effective_dates[i] or "").strip()

            if not dbb:
                errors.append(f"{row_label} (Discount): Base Price is required.")
            else:
                try:
                    float(dbb)
                except ValueError:
                    errors.append(f"{row_label} (Discount): Base Price must be numeric.")

            if not dbd:
                errors.append(f"{row_label} (Discount): Discount % is required.")
            else:
                try:
                    val = float(dbd)
                    if val < 0 or val > 1:
                        errors.append(f"{row_label} (Discount): Discount % must be between 0.0 and 1.0.")
                except ValueError:
                    errors.append(f"{row_label} (Discount): Discount % must be numeric.")

            if not dbe:
                errors.append(f"{row_label} (Discount): Effective Date is required.")
            elif dbe < today_str:
                errors.append(f"{row_label} (Discount): Effective Date cannot be before today.")

        elif lvl_method == "ehc_based":
            eb = (ehc_base[i] or "").strip()
            if not eb:
                errors.append(f"{row_label} (EHC): Base Price is required.")
            else:
                try:
                    float(eb)
                except ValueError:
                    errors.append(f"{row_label} (EHC): Base Price must be numeric.")

            # Numeric checks for EHC fee columns + packaging numbers
            numeric_lists = [
                (ehc_cb[i], "Canadian Blue"),
                (ehc_qty_case[i], "Qty/Case"),
                (ehc_moq[i], "Packaging MOQ"),
                (ehc_abmbsk_each[i], "AB/MB/SK Each"),
                (ehc_abmbsk_case[i], "AB/MB/SK Case"),
                (ehc_bc_each[i], "BC Each"),
                (ehc_bc_case[i], "BC Case"),
                (ehc_nl_each[i], "NL Each"),
                (ehc_nl_case[i], "NL Case"),
                (ehc_ns_each[i], "NS Each"),
                (ehc_ns_case[i], "NS Case"),
                (ehc_nbqc_each[i], "NB/QC Each"),
                (ehc_nbqc_case[i], "NB/QC Case"),
                (ehc_pei_each[i], "PEI Each"),
                (ehc_pei_case[i], "PEI Case"),
                (ehc_yk_each[i], "YK Each"),
                (ehc_yk_case[i], "YK Case"),
            ]
            for val, label_val in numeric_lists:
                v = (val or "").strip()
                if v:
                    try:
                        float(v)
                    except ValueError:
                        errors.append(f"{row_label} (EHC): {label_val} must be numeric.")

        elif lvl_method == "promo_pricing":
            pp = (pr_price[i] or "").strip()
            ps = (pr_start[i] or "").strip()
            pe = (pr_end[i] or "").strip()

            if not pp:
                errors.append(f"{row_label} (Promo): Promo Price is required.")
            else:
                try:
                    float(pp)
                except ValueError:
                    errors.append(f"{row_label} (Promo): Promo Price must be numeric.")

            if not ps:
                errors.append(f"{row_label} (Promo): Start Date is required.")
            if not pe:
                errors.append(f"{row_label} (Promo): End Date is required.")
            if ps and pe and ps > pe:
                errors.append(f"{row_label} (Promo): End Date cannot be before Start Date.")

        elif lvl_method == "quote_pricing":
            qp = (qt_price[i] or "").strip()
            qs = (qt_start[i] or "").strip()
            qe = (qt_end[i] or "").strip()

            if not qp:
                errors.append(f"{row_label} (Quote): Quote Price is required.")
            else:
                try:
                    float(qp)
                except ValueError:
                    errors.append(f"{row_label} (Quote): Quote Price must be numeric.")

            if not qs:
                errors.append(f"{row_label} (Quote): Start Date is required.")
            if not qe:
                errors.append(f"{row_label} (Quote): End Date is required.")
            if qs and qe and qs > qe:
                errors.append(f"{row_label} (Quote): End Date cannot be before Start Date.")

        elif lvl_method == "tender_pricing":
            tp = (td_price[i] or "").strip()
            ts = (td_start[i] or "").strip()
            te = (td_end[i] or "").strip()

            if not tp:
                errors.append(f"{row_label} (Tender): Tender Price is required.")
            else:
                try:
                    float(tp)
                except ValueError:
                    errors.append(f"{row_label} (Tender): Tender Price must be numeric.")

            if not ts:
                errors.append(f"{row_label} (Tender): Start Date is required.")
            if not te:
                errors.append(f"{row_label} (Tender): End Date is required.")
            if ts and te and ts > te:
                errors.append(f"{row_label} (Tender): End Date cannot be before Start Date.")

    if not any_level_used:
        errors.append("At least one valid pricing level must be entered.")

    return (len(errors) == 0, errors)