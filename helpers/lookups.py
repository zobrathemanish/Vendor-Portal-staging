"""
Lookup constants and controlled vocabularies for FGI Vendor Portal
"""

# Vendor Lists
VENDOR_LIST = [
    # OptiCat vendors
    "Dayton Parts", "Grote Lighting", "Neapco",
    "Truck Lite", "Baldwin Filters", "Stemco", "High Bar Brands",
    # Non-OptiCat vendors
    "Ride Air", "Tetran", "SAF Holland",
    "Consolidated Metco", "Tiger Tool", "J.W Speaker", "Rigid Industries"
]

# Change Types
CHANGE_TYPES = ["A", "M", "D"]  # Add, Modify, Delete

# Units of Measure
QUANTITY_UOM = ["EA", "PC", "BOX", "CS", "PK", "SET", "RL", "BG", "BT", "DZ"]

PACKAGING_TYPES = [
    "BOX", "CASE", "PALLET", "BAG", "BOTTLE", "CAN", 
    "CARTON", "WRAP", "PACK", "CRATE", "TUBE"
]

PACKAGE_UOM = ["EA", "BX", "CS", "PL"]

WEIGHT_UOM = ["LB", "KG", "G", "OZ"]

DIMENSION_UOM = ["IN", "CM", "MM"]

# Product Statuses
LIFECYCLE_STATUS = ["Active", "Inactive", "Obsolete"]
PRODUCT_STATUS = ["Active", "Inactive", "Obsolete"]

# Flags
HAZMAT_OPTIONS = ["Y", "N"]
APPLICATION_FLAG = ["Y", "N"]

# Barcode Types
GTIN_TYPES = ["UPC", "EAN"]

# Description Types
DESCRIPTION_TYPES = [
    "Short", "Long", "Marketing", "Web", "Extended", "Technical"
]

# Languages
LANGUAGES = ["EN", "FR", "ES"]

# Media and Digital Assets
MEDIA_TYPES = [
    "MainImage", "AngleImage", "PDF", "SpecSheet", 
    "Logo", "Thumbnail", "InstallationGuide"
]

FILE_FORMATS = ["JPEG", "PNG", "GIF", "PDF", "WEBP"]

COLOR_MODES = ["RGB", "CMYK"]

MEDIA_DATE_TYPES = ["Created", "Updated"]

# Pricing
CURRENCIES = ["CAD", "USD"]

ALT_UOM = ["EA", "PC", "SET", "BOX"]

PRICING_METHODS = {
    "net_cost": "Net Cost Provided",
    "price_levels": "Price Levels Provided",
    "discount_based": "Discount-Based Pricing",
    "ehc_based": "EHC-Based Pricing",
    "promo_pricing": "Promo Pricing",
    "quote_pricing": "Quote Pricing",
    "tender_pricing": "Tender Pricing",
    "core_pricing" : "Core Pricing"
}