#!/usr/bin/env python3
"""Execute one verification gate declared in verification.json.

This is the only place gate commands are executed. CI, autonomous agents, and
developers all go through here, so there is no second copy to drift from.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "verification.json"


def load_gate(gate_id):
    contract = json.loads(CONTRACT.read_text())
    for gate in contract["gates"]:
        if gate["id"] == gate_id:
            return gate
    known = ", ".join(g["id"] for g in contract["gates"])
    sys.exit(f"unknown gate '{gate_id}'. known gates: {known}")


def run_gate(gate, dry_run=False, json_output=False, continue_on_error=False):
    gate_start = time.monotonic()
    checks = []
    gate_failed = False

    for command in gate["commands"]:
        if dry_run:
            print(command)
            continue

        if gate_failed and not continue_on_error:
            checks.append({
                "command": command,
                "status": "skipped",
                "duration_ms": 0,
                "exit_code": None,
                "error": None,
            })
            continue

        if not json_output:
            print(f"==> {command}", flush=True)

        cmd_start = time.monotonic()
        stdout_dest = sys.stderr if json_output else None
        stderr_dest = sys.stderr if json_output else None

        result = subprocess.run(
            command,
            shell=True,
            cwd=ROOT,
            stdout=stdout_dest,
            stderr=stderr_dest,
        )
        cmd_duration_ms = int((time.monotonic() - cmd_start) * 1000)

        if result.returncode == 0:
            checks.append({
                "command": command,
                "status": "passed",
                "duration_ms": cmd_duration_ms,
                "exit_code": 0,
                "error": None,
            })
        else:
            gate_failed = True
            checks.append({
                "command": command,
                "status": "failed",
                "duration_ms": cmd_duration_ms,
                "exit_code": result.returncode,
                "error": f"command failed with exit code {result.returncode}",
            })

    total_duration_ms = int((time.monotonic() - gate_start) * 1000)
    gate_status = "failed" if gate_failed else "passed"

    return {
        "gate_id": gate["id"],
        "status": gate_status,
        "duration_ms": total_duration_ms,
        "checks": checks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", help="gate id from verification.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the commands instead of running them")
    parser.add_argument("--json", action="store_true",
                        help="output structured attributable GateResult as JSON")
    parser.add_argument("--output", "-o", type=Path,
                        help="write structured attributable GateResult JSON to file")
    parser.add_argument("--continue-on-error", "-c", action="store_true",
                        help="continue running remaining checks in gate even if one fails")
    args = parser.parse_args()

    gate = load_gate(args.gate)

    if args.dry_run:
        for command in gate["commands"]:
            print(command)
        return

    result = run_gate(
        gate,
        json_output=args.json,
        continue_on_error=args.continue_on_error,
    )

    if args.output:
        args.output.write_text(json.dumps(result, indent=2))

    if args.json:
        print(json.dumps(result, indent=2))

    if result["status"] != "passed":
        failing_checks = [c["command"] for c in result["checks"] if c["status"] == "failed"]
        if not args.json:
            sys.exit(f"gate '{args.gate}' failed on: {', '.join(failing_checks)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
