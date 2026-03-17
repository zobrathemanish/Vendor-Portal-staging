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
- Azure mode (default)
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
def run_step(name, script, vendor, submission_id, local: bool, mode: str):
    print(f"\n▶️  {name}")

    module_name = f"data_ETL.{script.replace('.py','')}"
    cmd = [sys.executable, "-m", module_name]

    if "--vendor" in STEPS_BY_SCRIPT[script]:
        cmd += ["--vendor", vendor]
    
    # need submission id as well
    cmd += ["--submission-id", submission_id]

     # ✅ Do NOT pass --mode to profiling or scorecard
    if script not in ("vendor_profiling.py", "vendor_scorecard.py"):
        cmd += ["--mode", mode]

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
        print(f"\n Failed at step: {name}")
        sys.exit(1)


# ============================================================
# MAIN
# ============================================================
def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", type=str)
    parser.add_argument("--submission-id", type=str, required=True)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--mode", default="full")

    args = parser.parse_args()

    submission_id = args.submission_id
    local = args.local
    mode = "LOCAL" if local else "AZURE"

    if args.all:
        raise SystemExit("--all is not supported in run_vendor_etl.py (use orchestrator)")

    if not args.vendor:
        raise SystemExit("Provide --vendor <vendor_name>")

    vendor = args.vendor

    print(
    f"\n🚀 Running full ETL | vendor={vendor} | submission={submission_id} ({mode} mode)"
)

    for name, script, flags in STEPS:

        # 🚫 Skip analytics in post_review mode
        if args.mode == "post_review" and script in (
            "vendor_profiling.py",
            "vendor_scorecard.py",
        ):
            print(f"⏭ Skipping {name} (post_review mode)")
            continue

        run_step(name, script, vendor, submission_id, local, args.mode)

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