from io import BytesIO
import os
import pandas as pd
import pyarrow.parquet as pq
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

# -----------------------------------------------------
# Load environment variables (same pattern as app)
# -----------------------------------------------------
load_dotenv()

CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = "silver"

if not CONNECTION_STRING:
    raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING not set")

# -----------------------------------------------------
# 🔧 EDIT THESE
# -----------------------------------------------------
VENDOR = "Grote Lighting"
PART = "00211"

# -----------------------------------------------------
# Azure Connect
# -----------------------------------------------------
blob_service = BlobServiceClient.from_connection_string(CONNECTION_STRING)
container = blob_service.get_container_client(CONTAINER_NAME)

def load_parquet(blob_path):
    print(f"\n📦 Loading: {blob_path}")
    blob = container.get_blob_client(blob_path)
    raw = blob.download_blob().readall()
    return pq.read_table(BytesIO(raw)).to_pandas()

# -----------------------------------------------------
# File Paths
# -----------------------------------------------------
baseline_path = f"approved/current_state/vendor={VENDOR}/etl_mapped.parquet"
ready_path = f"category_queue/vendor={VENDOR}/active/etl_mapped.parquet"

# -----------------------------------------------------
# Load Files
# -----------------------------------------------------
df_baseline = load_parquet(baseline_path)
df_ready = load_parquet(ready_path)

df_baseline["Part Number"] = df_baseline["Part Number"].astype(str)
df_ready["Part Number"] = df_ready["Part Number"].astype(str)

df_baseline_part = df_baseline[df_baseline["Part Number"] == PART]
df_ready_part = df_ready[df_ready["Part Number"] == PART]

# -----------------------------------------------------
# Display Raw Rows
# -----------------------------------------------------
print("\n================ BASELINE PART ROWS ================")
print(df_baseline_part[["Part Number", "__Section"]])
print(df_baseline_part)

print("\n================ READY PART ROWS ===================")
print(df_ready_part[["Part Number", "__Section"]])
print(df_ready_part)

# -----------------------------------------------------
# Section-Level Comparison
# -----------------------------------------------------
print("\n================ SECTION COMPARISON ================")

sections = set(
    df_baseline_part["__Section"].dropna().tolist()
).union(
    df_ready_part["__Section"].dropna().tolist()
)

def normalize(v):
    if pd.isna(v):
        return ""
    return str(v).strip()

for section in sections:

    print(f"\n--- SECTION: {section} ---")

    base_sec = df_baseline_part[df_baseline_part["__Section"] == section]
    ready_sec = df_ready_part[df_ready_part["__Section"] == section]

    if base_sec.empty:
        print("⚠️ Baseline missing this section")
        continue

    if ready_sec.empty:
        print("⚠️ Ready submission missing this section")
        continue

    base_row = base_sec.iloc[0]
    ready_row = ready_sec.iloc[0]

    all_cols = set(base_sec.columns).union(set(ready_sec.columns))

    for col in sorted(all_cols):
        if col in ["__Section", "_delta_type"]:
            continue

        before = normalize(base_row.get(col))
        after = normalize(ready_row.get(col))

        if before != after:
            print(f"{col}:")
            print(f"    BEFORE: {before}")
            print(f"    AFTER : {after}")
