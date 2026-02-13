import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    AZURE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    AZURE_CONTAINER_NAME = "bronze"
    ETL_TRIGGER_URL = os.getenv("ETL_TRIGGER_URL")
