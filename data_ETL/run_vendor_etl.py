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


# ============================================================
# PIPELINE STEPS (ORDER MATTERS)
# ============================================================
STEPS = [
    ("Health Check", "data_health_check.py", ["--vendor"]),
    ("Autofix", "data_autofix.py", ["--vendor"]),
    ("Canonicalize", "data_canonicalize.py", ["--vendor"]),
    ("Integrity Checks", "data_integrity_checks.py", ["--vendor"]),
    ("Build Review", "build_etl_mapped.py", ["--vendor"]),

    # ---- Analytics layer (vendor-level)
    ("Vendor Profiling", "vendor_profiling.py", ["--vendor"]),
    ("Vendor Scorecard", "vendor_scorecard.py", ["--vendor"]),
]


# ============================================================
# RUNNER
# ============================================================
def run_step(name, script, vendor, workflow, submission_id, submission_type, local: bool):
    print(f"\n▶️  {name}")

    module_name = f"data_ETL.{script.replace('.py','')}"
    cmd = [sys.executable, "-m", module_name]

    if "--vendor" in STEPS_BY_SCRIPT[script]:
        cmd += ["--vendor", vendor]

    cmd += ["--submission-id", submission_id]
    cmd += ["--workflow", workflow]
    cmd += ["--submission-type", submission_type]

    if local:
        cmd.append("--local")

    import os

    env = os.environ.copy()
    env["PYTHONPATH"] = os.getcwd()

    result = subprocess.run(
        cmd,
        cwd=os.getcwd(),
        env=env
    )

    if result.returncode != 0:
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
    local = args.local

    if args.all:
        raise SystemExit("--all is not supported in run_vendor_etl.py (use orchestrator)")

    if not args.vendor:
        raise SystemExit("Provide --vendor <vendor_name>")

    vendor = args.vendor

    print(
        f"\n🚀 Running full ETL | vendor={vendor} | submission={submission_id}"
    )


    for name, script, flags in STEPS:

        # 🚫 Skip analytics for review submissions
        if args.submission_type.endswith("_review") and script in (
            "vendor_profiling.py",
            "vendor_scorecard.py",
        ):
            print(f"⏭ Skipping {name} (review submission)")
            continue

        run_step(
            name,
            script,
            vendor,
            args.workflow,
            submission_id,
            args.submission_type,
            local
        )

    print(f"\n✅ ETL pipeline completed successfully for {vendor}")


# ------------------------------------------------------------
# Helper mapping (avoid repeated conditionals)
# ------------------------------------------------------------
STEPS_BY_SCRIPT = {
    script: flags
    for _, script, flags in STEPS
}


if __name__ == "__main__":
    main()