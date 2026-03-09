from flask import Blueprint, render_template, request, redirect, url_for, flash, request, current_app
from flask_login import login_required
import subprocess
import sys
import os

ingestion_bp = Blueprint(
    "ingestion",
    __name__,
    url_prefix="/ingestion"
)

# ---------------------------------------
# PRODUCT INGESTION
# ---------------------------------------

@ingestion_bp.route("/product")
@login_required
def ingest_product():
    return render_template("ingestion/ingest_product.html")


# ---------------------------------------
# PRICING INGESTION
# ---------------------------------------

@ingestion_bp.route("/pricing")
@login_required
def ingest_pricing():
    return render_template("ingestion/ingest_pricing.html")


# ---------------------------------------
# ASSET INGESTION
# ---------------------------------------

@ingestion_bp.route("/assets", methods=["GET", "POST"])
@login_required
def ingest_assets():

    if request.method == "POST":

        submission_type = request.form.get("submission_type")
        vendor = request.form.get("vendor_name")

        print(f"Starting asset ETL for vendor: {vendor}")

        etl_script = os.path.join(
            current_app.root_path,
            "asset-etl",
            "run_vendor_asset_etl.py"
        )

        subprocess.Popen([
            sys.executable,
            etl_script,
            "--vendor",vendor,
            "--submission-type", submission_type
        ])

        flash("Assets uploaded successfully. Asset ETL started.", "success")

        return redirect(url_for("ingestion.ingest_assets"))

    return render_template("ingestion/ingest_assets.html")