import os
import sys
import time

# =========================================================
# PATH SETUP (must run BEFORE other imports)
# =========================================================

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, ".."))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
# =========================================================
# IMPORTS
# =========================================================

import subprocess
import argparse
import json
from datetime import datetime

from common.terminal_logger import TerminalLogger


# =========================================================
# CONFIG
# =========================================================

STEP_PROGRESS = {
    "Asset Extraction & Validation": {"stage":"checking","progress":25,"message":"Checking uploaded files"},
    "Asset Canonicalization": {"stage":"validating","progress":50,"message":"Validating assets"},
    "Asset Transformations": {"stage":"transforming","progress":90,"message":"Transforming images"}
}

STEPS = [
    ("Asset Extraction & Validation", "asset_extraction_validation.py"),
    ("Asset Canonicalization", "asset_canonicalize.py"),
    ("Asset Transformations", "asset_transformations.py"),
]


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# =========================================================
# STATUS FILE HELPERS
# =========================================================

def get_status_path(vendor, submission_id):

    log_dir = os.path.join(
        BASE_DIR,
        "logs",
        f"vendor={vendor}",
        "assets",
        f"submission={submission_id}"
    )

    os.makedirs(log_dir, exist_ok=True)

    return os.path.join(log_dir, "asset_etl_status.json")


def load_status(vendor, submission_id):

    path = get_status_path(vendor, submission_id)

    if not os.path.exists(path):
        return None

    with open(path, "r") as f:
        return json.load(f)


def save_status(vendor, status, submission_id):

    path = get_status_path(vendor, submission_id)

    with open(path, "w") as f:
        json.dump(status, f, indent=2)


# =========================================================
# STEP EXECUTION
# =========================================================

def run_step(name, script, vendor, submission_type, submission_id):

    print(f"\n▶️ {name}")

    script_path = os.path.join(BASE_DIR, script)

    cmd = [
        sys.executable,
        script_path,
        "--vendor", vendor,
        "--submission-type", submission_type,
        "--submission-id", submission_id
    ]

    result = subprocess.run(cmd)

    if result.returncode != 0:
        raise RuntimeError(f"Step '{name}' failed.")
    
def update_manifest_step(vendor, submission_id, step, started_at, finished_at):

    import os
    from services.azure_service import read_json_blob_from_azure, upload_json_blob

    container = "bronze"

    blob_path = (
        f"raw/vendor={vendor}/assets/"
        f"submission={submission_id}/manifest.json"
    )

    manifest = read_json_blob_from_azure(blob_path, container)

    duration = (finished_at - started_at).total_seconds()

    manifest["etl_steps"].append({
        "step": step,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round(duration, 2)
    })

    upload_json_blob(
        manifest,
        blob_path,
        os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
        container
    )
# =========================================================
# MAIN
# =========================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-type", required=True)
    parser.add_argument("--submission-id", required=True)

    args = parser.parse_args()

    vendor = args.vendor
    submission_type = args.submission_type
    submission_id = args.submission_id

    # -----------------------------------------------------
    # Initialize terminal logging
    # -----------------------------------------------------

    sys.stdout = TerminalLogger(BASE_DIR, vendor, "assets", submission_id)
    sys.stderr = sys.stdout

    print("\n====================================")
    print("🚀 RUNNING ASSET ETL")
    print(f"Vendor: {vendor}")
    print(f"Submission: {submission_id}")
    print("====================================")

    # -----------------------------------------------------
    # Load or create status file
    # -----------------------------------------------------

    status = load_status(vendor, submission_id)

    if status is None:

        status = {
            "vendor": vendor,
            "submission_id": submission_id,
            "submission_type": submission_type,
            "started_at": datetime.utcnow().isoformat(),
            "completed_steps": [],
            "status": "running"
        }

        save_status(vendor, status, submission_id)

    completed = status["completed_steps"]

    # -----------------------------------------------------
    # Run pipeline steps
    # -----------------------------------------------------

    try:

        for name, script in STEPS:

            stage_info = STEP_PROGRESS.get(name)

            if stage_info:
                status["stage"] = stage_info["stage"]
                status["progress"] = stage_info["progress"]
                status["message"] = stage_info["message"]
                save_status(vendor, status, submission_id)

            if name in completed:
                print(f"⏭ Skipping already completed step: {name}")
                continue

            start_time = datetime.utcnow()
            run_step(name, script, vendor, submission_type, submission_id)
            end_time = datetime.utcnow()
            update_manifest_step(vendor, submission_id, name, start_time, end_time)

            completed.append(name)
            status["completed_steps"] = completed
            save_status(vendor, status, submission_id)

    except Exception as e:

        print(f"\n❌ Pipeline failed: {e}")

        status["status"] = "failed"
        status["stage"] = "failed"
        status["progress"] = 100
        status["message"] = str(e)
        status["failed_at"] = name
        status["finished_at"] = datetime.utcnow().isoformat()

        save_status(vendor, status, submission_id)
        sys.exit(1)
    # -----------------------------------------------------
    # Mark completion
    # -----------------------------------------------------

    status["status"] = "completed"
    status["stage"] = "complete"
    status["progress"] = 100
    status["message"] = "Processing complete"
    status["finished_at"] = datetime.utcnow().isoformat()

    save_status(vendor, status, submission_id)

    print("\n====================================")
    print("✅ Asset ETL completed successfully")
    print(f"Vendor: {vendor}")
    print("====================================")

    


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    main()