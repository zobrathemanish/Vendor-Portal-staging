import os
from azure.storage.blob import BlobServiceClient
from io import BytesIO
import pyarrow.parquet as pq
import pandas as pd

# 🔥 SAME AS YOUR APP
AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = os.getenv("SILVER_CONTAINER", "silver")

vendor = "Grote Lighting"

blob_path = f"category_queue/vendor={vendor}/active/delta_mapped.parquet"

# ----------------------------------------
# CONNECT
# ----------------------------------------
svc = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
container = svc.get_container_client(SILVER_CONTAINER)

# ----------------------------------------
# DOWNLOAD
# ----------------------------------------
print("Downloading:", blob_path)
blob = container.get_blob_client(blob_path)
data = blob.download_blob().readall()

print("Downloaded bytes:", len(data))

# ----------------------------------------
# READ PARQUET SAFELY
# ----------------------------------------
table = pq.read_table(BytesIO(data))
df = table.to_pandas()

print("Total rows:", len(df))
print("Columns:", df.columns.tolist())

# ----------------------------------------
# 🔍 CHECK YOUR CASE
# ----------------------------------------
df["Part Number"] = df["Part Number"].astype(str).str.strip()

df_00210 = df[df["Part Number"] == "00211"]

print("\n===== 00210 CHECK =====")
print(df_00210[["Part Number", "__Section", "_delta_type"]])

print("\nUnique delta types for 00210:")
print(df_00210["_delta_type"].unique())