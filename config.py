import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    AZURE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    AZURE_CONTAINER_NAME = "bronze"
    ETL_TRIGGER_URL = os.getenv("ETL_TRIGGER_URL")
    SECRET_KEY = "fgi_vendor_portal_secret"
    SESSION_TYPE = "filesystem"
    BASE_DIR = os.getcwd()
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
    TEMPLATE_FOLDER = os.path.join(BASE_DIR, "data", "templates")

