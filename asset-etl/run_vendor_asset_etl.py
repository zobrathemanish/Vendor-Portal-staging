import subprocess
import sys
import os
import argparse
import json

STEPS = [
    ("Asset Extraction & Validation", "asset_extraction_validation.py"),
    ("Asset Canonicalization", "asset_canonicalize.py"),
    ("Asset Transformations", "asset_transformations.py"),
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_status_path(vendor):

    log_dir = os.path.join(BASE_DIR, "logs", f"vendor={vendor}")
    os.makedirs(log_dir, exist_ok=True)

    return os.path.join(log_dir, "asset_etl_status.json")


def load_status(vendor):

    path = get_status_path(vendor)

    if not os.path.exists(path):
        return {"vendor": vendor, "completed_steps": []}

    with open(path, "r") as f:
        return json.load(f)


def save_status(vendor, status):

    path = get_status_path(vendor)

    with open(path, "w") as f:
        json.dump(status, f, indent=2)


def run_step(name, script, vendor, submission_type):

    print(f"\n▶️ {name}")

    script_path = os.path.join(BASE_DIR, script)

    cmd = [
        sys.executable,
        script_path,
        "--vendor",
        vendor,
        "--submission-type",
        submission_type
    ]

    result = subprocess.run(cmd)

    if result.returncode != 0:
        print(f"\n❌ Failed at step: {name}")
        sys.exit(1)


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", required=True)
    parser.add_argument("--submission-type", required=True)

    args = parser.parse_args()

    vendor = args.vendor
    submission_type = args.submission_type

    print("\n====================================")
    print("🚀 RUNNING ASSET ETL")
    print("Vendor:", vendor)
    print("====================================")

    status = {"vendor": vendor, "completed_steps": []}
    completed = status["completed_steps"]

    for name, script in STEPS:

        if name in completed:
            print(f"⏭ Skipping already completed step: {name}")
            continue

        run_step(name, script, vendor, submission_type)

        completed.append(name)
        save_status(vendor, status)

    print("\n====================================")
    print("✅ Asset ETL completed successfully")
    print("Vendor:", vendor)
    print("====================================")


if __name__ == "__main__":
    main()