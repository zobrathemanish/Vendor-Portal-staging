import argparse
import subprocess
import sys
import os

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))

# -------------------------------------
# SCRIPT PATHS
# -------------------------------------

mapping_script = os.path.join(
    PROJECT_ROOT,
    "mapping_validation",
    "scripts",
    "pre_etl_ingest_mapping.py"
)

validation_script = os.path.join(
    PROJECT_ROOT,
    "mapping_validation",
    "scripts",
    "pre_etl_validation.py"
)

# -------------------------------------
# ENTRYPOINT
# -------------------------------------

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument("--vendor", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--submission-type", required=True)
    parser.add_argument("--blob-path", required=True)

    args = parser.parse_args()

    vendor = args.vendor
    workflow = args.workflow
    submission_id = args.submission_id
    submission_type = args.submission_type
    blob_path = args.blob_path

    print("===================================")
    print("RUNNING MAPPING")
    print("===================================")

    result = subprocess.run(
        [
            sys.executable,
            mapping_script,
            "--vendor", vendor,
            "--workflow", workflow,
            "--submission-id", submission_id,
            "--submission-type", submission_type
        ],
        capture_output=True,
        text=True
    )

    print("=== INNER MAPPING STDOUT ===")
    print(result.stdout)

    print("=== INNER MAPPING STDERR ===")
    print(result.stderr)

    if result.returncode != 0:
        print("Mapping failed")
        sys.exit(1)

    print("===================================")
    print("RUNNING VALIDATION")
    print("===================================")

    result = subprocess.run([
        sys.executable,
        validation_script,
        "--vendor", vendor,
        "--workflow", workflow,
        "--submission-id", submission_id,
        "--submission-type", submission_type
    ])

    if result.returncode != 0:
        print("Validation failed")
        sys.exit(1)

    print(" Mapping + Validation complete")


if __name__ == "__main__":
    main()