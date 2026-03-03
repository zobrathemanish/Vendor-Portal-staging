#upload_routes.py

from flask import Blueprint, render_template, session, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from datetime import datetime
import os
from extensions import logger
from services.file_service import save_file
from services.azure_service import (
    upload_blob,
    upload_json_blob,
    upload_to_azure_bronze_opticat,
    upload_to_azure_bronze_non_opticat,
    write_status_to_azure,
)
from services.submission_service import (
    move_staging_assets_to_submission,
    trigger_etl,
    get_latest_submission_files
)

from services.azure_service import *
upload_bp = Blueprint("upload", __name__)

from flask import current_app

@upload_bp.route("/upload/", methods=["GET"])
@login_required
def upload_page():

    if "active_submission_id" not in session:
        session["active_submission_id"] = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        session["active_submission_vendor"] = current_user.vendor
        session.modified = True

    # ✅ one-shot autostart for status polling
    autostart = session.pop("force_autostart", "0")
    session.modified = True

    return render_template(
            "uploads.html",
            submission_id=session.get("last_submission_id"),   # 👈 ONLY poll this
            submission_vendor=session.get("last_submission_vendor"),
            active_submission_id=session.get("active_submission_id"),
            active_submission_vendor=session.get("active_submission_vendor"),
            autostart=autostart,
        )


@upload_bp.route('/upload/', methods=['POST'])
@login_required
def upload_files():

    logger.info("Submission received")

    submission_type = request.form.get("submission_type")
    vendor_type = request.form.get("vendor_type")

    # Resolve vendor safely
    if current_user.role == "vendor":
        vendor_name = current_user.vendor
    else:
        vendor_name = request.form.get("vendor_name")

    # -----------------------------------------------------
    # PRICING REVIEW FLOW (ISOLATED FROM VENDOR FLOW)
    # -----------------------------------------------------
    
    if submission_type == "pricing_review":

        submission_id = get_latest_vendor_submission_id(vendor_name)
        if not submission_id:
            flash("No existing submission found for this vendor.", "danger")
            return redirect(url_for("upload.upload_page"))

        session["last_submission_id"] = submission_id
        session["last_submission_vendor"] = vendor_name
        session.modified = True

        logger.info(f"Pricing review submitted for vendor={vendor_name}")
        approved_file = request.files.get("approved_pricing_file")

        if not vendor_name or not approved_file:
            flash("Vendor and pricing file are required.", "danger")
            return redirect(url_for("upload.upload_page"))

        local_path = save_file(
            approved_file,
            vendor_name,
            "pricing_review",
            current_app.config["UPLOAD_FOLDER"]
        )

        try:
            blob_path = (
                f"post_pricing_review/vendor={vendor_name}/"
                f"submission={submission_id}/"
                f"mapped/mapped.xlsx"
            )

            upload_blob(
                local_path=local_path,
                blob_path=blob_path,
                connection_string=current_app.config["AZURE_CONNECTION_STRING"],
                container_name="silver"
            )

            # =====================================================
            # 🔥 RESET STATUS BEFORE STARTING NEW POST-REVIEW RUN
            # =====================================================

            write_status_to_azure(
                vendor=vendor_name,
                submission_id=submission_id,
                stage="POST_REVIEW",
                status="QUEUED",
                message="Pricing review submitted (reset lifecycle)"
            )


            # ---------------------------------------
            # CLEAR OLD PRICING LOCK (ALLOW RE-RUN)
            # ---------------------------------------

            blob_service = BlobServiceClient.from_connection_string(
                current_app.config["AZURE_CONNECTION_STRING"]
            )
            silver = blob_service.get_container_client("silver")

            lock_blob = (
                f"logs/vendor={vendor_name}/"
                f"submission={submission_id}/"
                f"lock_PRICING_REVIEW.json"
            )

            try:
                silver.delete_blob(lock_blob)
                logger.info("Old PRICING_REVIEW lock cleared")
            except Exception:
                # lock may not exist — that's fine
                pass

            marker_payload = {
                "schema_version": "2.0",
                "marker_type": "PRICING_REVIEW",
                "vendor": vendor_name,
                "submission_id": submission_id,
                "submission_type": "pricing_review",
                "actions": {
                    "notify": True,
                    "run_etl": True
                },
                "uploaded_file": approved_file.filename,
                "created_at": datetime.utcnow().isoformat() + "Z"
            }

            #added so that pricing upload is always triggered
            event_ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")

            marker_name = (
                f"raw/notifymarker/"
                f"{vendor_name}_{submission_id}_PRICING_{event_ts}.json"
            )

            upload_json_blob(
                data=marker_payload,
                blob_path=marker_name,
                connection_string=current_app.config["AZURE_CONNECTION_STRING"],
                container_name="bronze"
            )

            flash(f"Pricing review submitted for {vendor_name}.", "success")

        except Exception as e:
            flash(f"Pricing upload failed: {e}", "danger")
        
        # ✅ Force UI polling on next page load
        session["force_autostart"] = "1"
        session.modified = True

        return redirect(url_for("upload.upload_page"))

    # -----------------------------------------------------
    # VENDOR SUBMISSION FLOW (UNCHANGED LOGIC)
    # -----------------------------------------------------
    if submission_type == "vendor":

        # Finalize submission
        submission_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

        session["last_submission_id"] = submission_id
        session["last_submission_vendor"] = vendor_name
        session["active_submission_id"] = None

        session.modified = True

        if not vendor_name:
            flash("Please select a vendor before submitting.", "danger")
            return redirect(url_for("upload.upload_page"))

    # Shared
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # -------------------------------
    # PROCESS OPTICAT VENDORS
    # -------------------------------
    if vendor_type == "opticat":
        logger.info(f"OptiCat submission for vendor={vendor_name}")
        product_file = request.files.get('product_file')
        pricing_file = request.files.get('pricing_file')

        if not product_file and not pricing_file:
            flash('At least one file (XML or Pricing) must be uploaded.', 'danger')
            return redirect(url_for('upload.upload_page'))

        latest_files = get_latest_submission_files(
            vendor_name,
            current_app.config["AZURE_CONNECTION_STRING"]
        )

        # Save locally first
        product_path = save_file(product_file, vendor_name, "opticat", current_app.config['UPLOAD_FOLDER'])
        pricing_path = save_file(pricing_file, vendor_name, "opticat", current_app.config['UPLOAD_FOLDER'])

         # 🚚 MOVE ASSETS
        move_staging_assets_to_submission(
            vendor=vendor_name,
            final_submission_id=submission_id
        )


        try:
            upload_to_azure_bronze_opticat(
                vendor=vendor_name,
                submission_id=submission_id,
                xml_local_path=product_path,
                pricing_local_path=pricing_path,
                connection_string=current_app.config["AZURE_CONNECTION_STRING"],
                container_name=current_app.config["AZURE_CONTAINER_NAME"],
                upload_folder=current_app.config['UPLOAD_FOLDER']
            )
            asset_blob_paths = request.form.getlist("asset_blob_path[]")

            if asset_blob_paths:
                latest_asset = asset_blob_paths[-1]

                blob_service_client = BlobServiceClient.from_connection_string(current_app.config["AZURE_CONNECTION_STRING"])
                container_client = blob_service_client.get_container_client(current_app.config["AZURE_CONNECTION_STRING"])

            # ---------------------------------------
            # CREATE NOTIFY MARKER (Vendor Submission)
            # ---------------------------------------
            marker_payload = {
                # --------------------
                # Marker governance
                # --------------------
                "schema_version": "2.0",
                "marker_type": "VENDOR_SUBMISSION",

                # --------------------
                # Identity
                # --------------------
                "vendor": vendor_name,
                "submission_id": submission_id,

                # --------------------
                # Submission metadata
                # --------------------
                "submission_type": "vendor_submission",
                "vendor_type": "opticat",

                # --------------------
                # Intent flags
                # --------------------
                "actions": {
                    "notify": True,     # Logic App email
                    "run_etl": True     # ETL watcher allowed
                },

                # --------------------
                # Context (optional but useful)
                # --------------------
                "uploaded_files": [
                    product_file.filename,
                    pricing_file.filename
                ],
                "raw_vendor_path": f"raw/vendor={vendor_name}",

                "created_at": datetime.utcnow().isoformat() + "Z"
            }


            marker_name = (
                f"raw/notifymarker/"
                f"{vendor_name}_{submission_id}.json"
            )

            upload_json_blob(
                data=marker_payload,
                blob_path=marker_name,
                connection_string=current_app.config["AZURE_CONNECTION_STRING"],
                container_name="bronze"
            )

            session["last_submission_id"] = submission_id
            session["last_submission_vendor"] = vendor_name
            session.modified = True

            flash(f'OptiCat files for {vendor_name} uploaded successfully.', 'success')

            # trigger_etl(vendor_name, submission_id)

        except Exception as e:
            flash(f'Azure upload failed: {e}', 'danger')
        
        logger.info(f"Submitting ETL Request. . . .")

        return redirect(url_for('upload.upload_page'))

    # -------------------------------
    # PROCESS NON-OPTICAT VENDORS
    # -------------------------------
    elif vendor_type == "non-opticat":
        logger.info(f"Non-OptiCat submission for vendor={vendor_name}")
        unified_file = request.files.get('non_opticat_file')

        if not unified_file:
            flash('A unified XLSX file is required for Non-OptiCat vendors.', 'danger')
            return redirect(url_for('upload.upload_page'))

        # Save unified vendor file
        unified_path = save_file(unified_file, vendor_name, "non_opticat", current_app.config['UPLOAD_FOLDER'])

            # 🚚 MOVE ASSETS
        move_staging_assets_to_submission(
            vendor=vendor_name,
            final_submission_id=submission_id
        )

        try:
            upload_to_azure_bronze_non_opticat(
                vendor=vendor_name,
                submission_id=submission_id,
                unified_local_path=unified_path,
                connection_string=current_app.config["AZURE_CONNECTION_STRING"],
                container_name=current_app.config["AZURE_CONTAINER_NAME"],
                upload_folder=current_app.config['UPLOAD_FOLDER']
            )
            asset_blob_paths = request.form.getlist("asset_blob_path[]")

            if asset_blob_paths:
                latest_asset = asset_blob_paths[-1]

                blob_service_client = BlobServiceClient.from_connection_string(current_app.config["AZURE_CONNECTION_STRING"])
                container_client = blob_service_client.get_container_client(current_app.config["AZURE_CONNECTION_STRING"])

            # ---------------------------------------
            # CREATE NOTIFY MARKER (Vendor Submission)
            # ---------------------------------------
            marker_payload = {
                # --------------------
                # Marker governance
                # --------------------
                "schema_version": "2.0",
                "marker_type": "VENDOR_SUBMISSION",

                # --------------------
                # Identity
                # --------------------
                "vendor": vendor_name,
                "submission_id": submission_id,

                # --------------------
                # Submission metadata
                # --------------------
                "submission_type": "vendor_submission",
                "vendor_type": "non-opticat",

                # --------------------
                # Intent flags
                # --------------------
                "actions": {
                    "notify": True,
                    "run_etl": True
                },

                # --------------------
                # Context
                # --------------------
                "uploaded_files": [
                    unified_file.filename
                ],
                "raw_vendor_path": f"raw/vendor={vendor_name}",

                "created_at": datetime.utcnow().isoformat() + "Z"
            }


            marker_name = (
                f"raw/notifymarker/"
                f"{vendor_name}_{submission_id}.json"
            )

            upload_json_blob(
                data=marker_payload,
                blob_path=marker_name,
                connection_string=current_app.config["AZURE_CONNECTION_STRING"],
                container_name="bronze"
            )
            flash(f'Unified file for {vendor_name} uploaded successfully.', 'success')

            session["last_submission_id"] = submission_id
            session["last_submission_vendor"] = vendor_name
            session.modified = True

            write_status_to_azure(
                vendor=vendor_name,
                submission_id=submission_id,
                stage="UPLOAD",
                status="DONE",
                message="Files uploaded to bronze"
            )

            trigger_etl(vendor_name, submission_id)

        except Exception as e:
            flash(f'Azure upload failed: {e}', 'danger')

        return redirect(url_for('upload.upload_page'))

    else:
        flash("Unknown vendor type.", "danger")
        return redirect(url_for("upload.upload_page"))
    