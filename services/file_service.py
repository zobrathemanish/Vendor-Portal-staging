"""
File handling and validation services for FGI Vendor Portal
"""
import os
import hashlib
from werkzeug.utils import secure_filename


ALLOWED_EXTENSIONS = {'xml', 'xlsx'}


def allowed_file(filename):
    """
    Check if uploaded file has an allowed extension.
    
    Args:
        filename (str): Name of the file to check
        
    Returns:
        bool: True if file extension is allowed, False otherwise
    """
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_file(file, vendor_name, subfolder, upload_folder):
    """
    Save file locally to vendor-specific subfolder.
    
    Args:
        file: FileStorage object from Flask request
        vendor_name (str): Name of the vendor
        subfolder (str): Subfolder path (e.g., 'opticat', 'manual_assets')
        upload_folder (str): Base upload directory path
        
    Returns:
        str: Full filepath where file was saved
    """
    filename = secure_filename(file.filename)
    vendor_folder = os.path.join(upload_folder, vendor_name, subfolder)
    os.makedirs(vendor_folder, exist_ok=True)
    filepath = os.path.join(vendor_folder, filename)
    file.save(filepath)
    return filepath


def compute_file_hash(filepath):
    """
    Compute SHA-256 hash of a file without loading it entirely into RAM.
    Useful for detecting if asset ZIP has changed.
    
    Args:
        filepath (str): Path to the file
        
    Returns:
        str: Hexadecimal SHA-256 hash string
    """
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()