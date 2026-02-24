from flask import Blueprint, jsonify, render_template
from flask_login import login_required, current_user
from io import BytesIO
import pandas as pd
import os

from azure.storage.blob import BlobServiceClient

admin_bp = Blueprint("admin", __name__)

AZURE_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
SILVER_CONTAINER = os.getenv("SILVER_CONTAINER", "silver")

blob_service = BlobServiceClient.from_connection_string(AZURE_CONN_STR)
container = blob_service.get_container_client(SILVER_CONTAINER)


# =========================================================
# ADMIN DASHBOARD PAGE
# =========================================================

@admin_bp.route("/admin/dashboard")
@login_required
def admin_dashboard():
    if current_user.role != "admin":
        return "Unauthorized", 403
    return render_template("admin_dashboard.html")


# =========================================================
# ADMIN SUMMARY API
# =========================================================

@admin_bp.route("/api/admin/summary")
@login_required
def get_admin_summary():
    if current_user.role != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    try:
        blob = container.get_blob_client("admin/vendor_summary.parquet")
        data = blob.download_blob().readall()
        df = pd.read_parquet(BytesIO(data))

        # 🔥 Force safe JSON conversion
        records = df.to_dict(orient="records")

        # Replace NaN manually (bulletproof)
        import math
        for row in records:
            for k, v in row.items():
                if isinstance(v, float) and math.isnan(v):
                    row[k] = None

        return jsonify(records)

    except Exception as e:
        return jsonify({"error": str(e)}), 500