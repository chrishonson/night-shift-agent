#!/usr/bin/env python3
"""Offline validator and reviewer CLI for S4 E2E run records."""
import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is on python path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_record import (
    review_run_record,
    run_bounded_fixture,
    validate_run_record,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    # validate
    validate_parser = sub.add_parser("validate", help="Validate a run record directory or manifest.json")
    validate_parser.add_argument("path", type=Path, help="Path to run record directory or manifest.json")
    validate_parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    # review
    review_parser = sub.add_parser("review", help="Review a run record directory or manifest.json")
    review_parser.add_argument("path", type=Path, help="Path to run record directory or manifest.json")

    # generate-sample
    sample_parser = sub.add_parser("generate-sample", help="Generate a bounded coding fixture sample")
    sample_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "eval/s4/e2e-run-record-sample",
        help="Target directory for generated sample",
    )

    args = parser.parse_args()

    if args.command == "generate-sample":
        manifest = run_bounded_fixture(output_dir=args.output_dir)
        print(f"Sample generated at: {args.output_dir}")
        print(f"Record ID: {manifest.get('record_id')}")
        print(f"Outcome: {manifest.get('execution_summary', {}).get('terminal_outcome')}")
        sys.exit(0)

    elif args.command == "review":
        try:
            report_str = review_run_record(args.path)
            print(report_str)
            sys.exit(0)
        except Exception as e:
            print(f"Error reviewing run record: {e}", file=sys.stderr)
            sys.exit(3)

    elif args.command == "validate":
        try:
            result = validate_run_record(args.path)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                report_str = review_run_record(args.path)
                print(report_str)

            if not result.get("complete") or any("missing" in err or "schema" in err or "secret" in err for err in result.get("errors", [])):
                sys.exit(3)
            elif not result.get("valid") or result.get("reconstructed", {}).get("terminal_outcome") != "succeeded":
                sys.exit(2)
            else:
                sys.exit(0)
        except Exception as e:
            print(f"Validation fatal error: {e}", file=sys.stderr)
            sys.exit(3)


if __name__ == "__main__":
    main()
