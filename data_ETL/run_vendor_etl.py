"""
run_vendor_etl.py

Purpose:
--------
Run the full vendor ETL pipeline end-to-end, including:
- Data health check
- Autofix
- Canonicalization
- Integrity checks
- Review build
- Vendor profiling
- Vendor scorecard

Supports:
- Local mode (--local)
"""

import subprocess
import sys

import os
import json


def write_status(vendor, submission_id, workflow, stage, message, progress, status=None):
    workflow_folder = "products" if workflow == "products" else "pricing"

    status_filename = (
        "product_etl_status.json"
        if workflow == "products"
        else "pricing_etl_status.json"
    )

    status_path = os.path.join(
        os.getcwd(),
        "product-etl",
        "logs",
        f"vendor={vendor}",
        workflow_folder,
        f"submission={submission_id}",
        status_filename
    )

    os.makedirs(os.path.dirname(status_path), exist_ok=True)

    data = {
        "stage": stage,
        "message": message,
        "progress": progress
    }

    if status:
        data["status"] = status

    with open(status_path, "w") as f:
        json.dump(data, f)

# ============================================================
# PIPELINE STEPS (ORDER MATTERS)
# ============================================================
STEPS = [
    ("Health Check", "data_health_check.py", ["--vendor"]),
    ("Autofix", "data_autofix.py", ["--vendor"]),
    ("Canonicalize", "data_canonicalize.py", ["--vendor"]),
    ("Build Review", "build_etl_mapped.py", ["--vendor"]),

    # ---- Analytics layer (vendor-level)
    ("Vendor Profiling", "vendor_profiling.py", ["--vendor"]),
    ("Vendor Scorecard", "vendor_scorecard.py", ["--vendor"]),
]


# ============================================================
# RUNNER
# ============================================================
def run_step(name, script, vendor, workflow, submission_type, submission_id, local: bool):
    print(f"\n▶️  {name}")

    # 🔥 map step → stage
    if "Health" in name or "Autofix" in name:
        stage = "validate"
        progress = 30
    elif "Canonicalize" in name or "Integrity" in name:
        stage = "transform"
        progress = 60
    elif "Build Review" in name:
        stage = "transform"
        progress = 80
    else:
        stage = "transform"
        progress = 70

    write_status(
        vendor,
        submission_id,
        workflow,
        stage=stage,
        message=f"{name} running...",
        progress=progress
    )

    module_name = f"data_ETL.{script.replace('.py','')}"
    cmd = [sys.executable, "-m", module_name]

    if "--vendor" in STEPS_BY_SCRIPT[script]:
        cmd += ["--vendor", vendor]

    cmd += ["--submission-id", submission_id]
    cmd += ["--workflow", workflow]
    cmd += ["--submission-type", submission_type]

    if local:
        cmd.append("--local")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.getcwd()

    result = subprocess.run(
        cmd,
        cwd=os.getcwd(),
        env=env
    )

    if result.returncode != 0:
        write_status(
            vendor,
            submission_id,
            workflow,
            stage="failed",
            message=f"{name} failed",
            progress=progress,
            status="failed"
        )
        print(f"\n❌ Failed at step: {name}")
        sys.exit(1)


# ============================================================
# MAIN
# ============================================================
def main():
    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument("--vendor")
    parser.add_argument("--submission-id", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--submission-type", dest="submission_type", required=True)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--local", action="store_true")

    args = parser.parse_args()

    submission_id = args.submission_id
    submission_type = args.submission_type
    workflow = args.workflow
    local = args.local

    if args.all:
        raise SystemExit("--all is not supported in run_vendor_etl.py (use orchestrator)")

    if not args.vendor:
        raise SystemExit("Provide --vendor <vendor_name>")

    vendor = args.vendor

    print(
        f"\n🚀 Running full ETL | vendor={vendor} | submission={submission_id}"
    )

    write_status(
        vendor,
        submission_id,
        workflow,
        stage="upload",
        message="Upload complete, starting pipeline...",
        progress=10
    )


    for name, script, flags in STEPS:

        # # 🚫 Skip analytics for review submissions
        # if args.submission_type.endswith("_review") and script in (
        #     "vendor_profiling.py",
        #     "vendor_scorecard.py",
        # ):
        #     print(f"⏭ Skipping {name} (review submission)")
        #     continue

        run_step(
            name,
            script,
            vendor,
            workflow,
            submission_type,
            submission_id,
            local
        )

    print(f"\n✅ ETL pipeline completed successfully for {vendor}")

    write_status(
        vendor,
        submission_id,
        workflow,
        stage="ready",
        message="ETL completed successfully",
        progress=100,
        status="completed"
    )


# ------------------------------------------------------------
# Helper mapping (avoid repeated conditionals)
# ------------------------------------------------------------
STEPS_BY_SCRIPT = {
    script: flags
    for _, script, flags in STEPS
}


if __name__ == "__main__":
    main()