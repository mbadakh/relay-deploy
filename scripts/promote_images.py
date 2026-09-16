#!/usr/bin/env python3
"""Copy immutable release images into the customer ECR repositories."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def output_value(outputs: dict, key: str):
    item = outputs.get(key)
    if not isinstance(item, dict) or "value" not in item:
        raise ValueError(f"missing Terraform output: {key}")
    return item["value"]


def optional_output(outputs: dict, key: str, default):
    item = outputs.get(key)
    return item["value"] if isinstance(item, dict) and "value" in item else default


def run(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        list(args),
        input=input_text,
        text=True,
        check=True,
        capture_output=True,
    )
    return result.stdout.strip()


def published_digest(reference: str) -> str | None:
    """Return an existing registry digest without treating absence as success."""
    try:
        return run("crane", "digest", reference)
    except subprocess.CalledProcessError:
        return None


def promote(source: str, destination: str, expected: str, label: str) -> None:
    source_digest = run("crane", "digest", source)
    if source_digest != expected:
        raise RuntimeError(f"source digest mismatch for {label}")

    existing = published_digest(destination)
    if existing is not None:
        if existing != expected:
            raise RuntimeError(
                f"immutable destination tag for {label} already points to {existing}"
            )
        print(f"{label} already promoted at {expected}")
        return

    run("crane", "copy", source, destination)
    destination_digest = run("crane", "digest", destination)
    if destination_digest != expected:
        raise RuntimeError(f"destination digest mismatch for {label}")
    print(f"Promoted {label} at {expected}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-lock", type=Path, required=True)
    parser.add_argument("--platform-images", type=Path, required=True)
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    release = json.loads(args.release_lock.read_text(encoding="utf-8"))
    platform = json.loads(args.platform_images.read_text(encoding="utf-8"))
    outputs = json.loads(args.terraform_output.read_text(encoding="utf-8"))
    repositories = output_value(outputs, "ecr_repository_urls")
    registry = repositories["relay-server"].split("/", 1)[0]
    password = run("aws", "ecr", "get-login-password", "--region", args.region)
    run("crane", "auth", "login", registry, "--username", "AWS", "--password-stdin", input_text=password)

    source_user = os.environ.get("SOURCE_REGISTRY_USERNAME", "")
    source_token = os.environ.get("SOURCE_REGISTRY_TOKEN", "")
    source_registries = {
        str(image["source"]).split("/", 1)[0]
        for image in release.get("images", {}).values()
        if isinstance(image, dict) and "/" in str(image.get("source", ""))
    }
    for image in platform.get("images", {}).values():
        if isinstance(image, dict):
            source_registries.add(str(image.get("source", "")).split("/", 1)[0])
    if "ghcr.io" in source_registries:
        if not source_user or not source_token:
            raise ValueError("GitHub Container Registry credentials are required")
        run(
            "crane", "auth", "login", "ghcr.io", "--username", source_user,
            "--password-stdin", input_text=source_token,
        )

    mapping = {"server": "relay-server", "keycloak": "relay-keycloak"}
    for component, repository_key in mapping.items():
        image = release["images"][component]
        expected = str(image["digest"])
        if not DIGEST.fullmatch(expected):
            raise ValueError(f"invalid digest for {component}")
        source = f"{image['source']}@{expected}"
        destination = f"{repositories[repository_key]}:{release['version']}"
        promote(source, destination, expected, component)

    platform_mapping = {"coturn": "relay-coturn"}
    if optional_output(outputs, "home_lab", False) is True:
        platform_mapping.update(caddy="relay-caddy", postgres="relay-postgres")
    for component, repository_key in platform_mapping.items():
        descriptor = platform.get("images", {}).get(component, {})
        expected = str(descriptor.get("digest", ""))
        version = str(descriptor.get("version", ""))
        if (
            not DIGEST.fullmatch(expected)
            or not version
            or any(char.isspace() for char in version)
        ):
            raise ValueError(f"invalid pinned {component} platform image")
        source = f"{descriptor['source']}@{expected}"
        destination = f"{repositories[repository_key]}:{version}"
        promote(source, destination, expected, component)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, ValueError, RuntimeError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"error: image promotion failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
