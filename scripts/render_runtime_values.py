#!/usr/bin/env python3
"""Resolve post-apply values required by the EKS GitOps leaf."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


def output_value(outputs: dict, name: str):
    item = outputs.get(name)
    if not isinstance(item, dict) or "value" not in item:
        raise ValueError(f"missing Terraform output: {name}")
    return item["value"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--leaf", type=Path, required=True)
    parser.add_argument("--terraform-output", type=Path, required=True)
    args = parser.parse_args()
    values_path = args.leaf / "values.yaml"
    if not values_path.exists():
        return 0

    values = yaml.safe_load(values_path.read_text(encoding="utf-8"))
    outputs = json.loads(args.terraform_output.read_text(encoding="utf-8"))
    values["database"]["host"] = output_value(outputs, "database_endpoint")
    values["secrets"]["relayRuntimeSecretArn"] = output_value(
        outputs, "runtime_secret_arn"
    )
    values["secrets"]["keycloakRuntimeSecretArn"] = output_value(
        outputs, "keycloak_secret_arn"
    )
    values["storage"]["mediaBucket"] = output_value(outputs, "bucket_names")["media"]

    # EKS Pod Identity associations are managed by Terraform. Do not add IRSA
    # annotations: these roles trust pods.eks.amazonaws.com, not a web identity
    # provider, and an IRSA annotation would select the wrong credential flow.

    rendered = yaml.safe_dump(values, sort_keys=False, allow_unicode=True)
    if "__TF_OUTPUT_" in rendered or "{{" in rendered:
        raise ValueError("runtime values still contain unresolved placeholders")
    values_path.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
