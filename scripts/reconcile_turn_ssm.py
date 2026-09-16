#!/usr/bin/env python3
"""Trigger and verify the customer TURN reconciliation through AWS SSM."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


TERMINAL = {"Success", "Cancelled", "TimedOut", "Failed", "Cancelling"}
MAX_SSM_ERROR_OUTPUT = 8_000


def invocation_failure(item: dict) -> str:
    instance_id = item.get("InstanceId", "unknown-instance")
    status = item.get("Status", "Unknown")
    output = "\n".join(
        str(plugin.get("Output", "")).strip()
        for plugin in item.get("CommandPlugins", [])
        if str(plugin.get("Output", "")).strip()
    )
    if len(output) > MAX_SSM_ERROR_OUTPUT:
        output = "[earlier SSM output omitted]\n" + output[-MAX_SSM_ERROR_OUTPUT:]
    return f"{instance_id}={status}" + (f":\n{output}" if output else "")


def aws_json(*arguments: str) -> dict:
    result = subprocess.run(
        ["aws", *arguments, "--output", "json"], check=True,
        capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    outputs = json.loads(args.terraform_output.read_text(encoding="utf-8"))
    instances = outputs.get("turn_instance_ids", {}).get("value", [])
    if outputs.get("home_lab", {}).get("value") is True:
        if instances:
            raise ValueError("Home Lab context unexpectedly contains dedicated TURN instances")
        print("Home Lab TURN is co-located and reconciled with the Compose workload.")
        return 0
    if not instances or not all(isinstance(item, str) for item in instances):
        raise ValueError("missing TURN instance IDs")

    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        current = aws_json(
            "ssm", "describe-instance-information", "--filters",
            "Key=InstanceIds,Values=" + ",".join(instances), "--region", args.region,
        ).get("InstanceInformationList", [])
        if {item["InstanceId"] for item in current if item.get("PingStatus") == "Online"} == set(instances):
            break
        time.sleep(10)
    else:
        raise TimeoutError("TURN instances did not become SSM Online")

    parameters = {
        "commands": [
            "set -euo pipefail",
            "/usr/bin/flock -w 900 /var/lock/relay-turn-reconcile.lock systemctl start relay-turn-reconcile.service",
            "docker inspect --format '{{.State.Running}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' relay-turn | grep -qx 'true 0 healthy'",
            "ss -H -lnt | grep -Eq '(^|[[:space:]])[^[:space:]]*:3478[[:space:]]'",
            "ss -H -lnu | grep -Eq '(^|[[:space:]])[^[:space:]]*:3478[[:space:]]'",
        ],
        "executionTimeout": ["1200"],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8") as stream:
        json.dump(parameters, stream)
        stream.flush()
        response = aws_json(
            "ssm", "send-command", "--document-name", "AWS-RunShellScript",
            "--comment", "Relay TURN immutable reconciliation", "--instance-ids", *instances,
            "--parameters", f"file://{stream.name}", "--timeout-seconds", "1200",
            "--region", args.region,
        )
    command_id = response["Command"]["CommandId"]
    deadline = time.monotonic() + 1300
    while time.monotonic() < deadline:
        invocations = aws_json(
            "ssm", "list-command-invocations", "--command-id", command_id,
            "--details", "--region", args.region,
        ).get("CommandInvocations", [])
        if len(invocations) == len(instances) and all(item.get("Status") in TERMINAL for item in invocations):
            failures = [item for item in invocations if item.get("Status") != "Success"]
            if failures:
                details = "\n\n".join(invocation_failure(item) for item in failures)
                raise RuntimeError(f"TURN reconciliation failed through SSM:\n{details}")
            print(f"TURN reconciled on {len(instances)} instance(s).")
            return 0
        time.sleep(10)
    raise TimeoutError("timed out waiting for TURN reconciliation")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, TimeoutError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
