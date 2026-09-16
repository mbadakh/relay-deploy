#!/usr/bin/env python3
"""Run the private EKS reconciler for one reviewed lifecycle upgrade."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any


def aws_json(region: str, *args: str) -> dict[str, Any]:
    process = subprocess.run(
        ["aws", *args, "--region", region, "--output", "json", "--no-cli-pager"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "AWS_PAGER": ""},
    )
    value = json.loads(process.stdout)
    if not isinstance(value, dict):
        raise ValueError("AWS returned an invalid response")
    return value


def output_value(outputs: dict[str, Any], key: str) -> Any:
    item = outputs.get(key)
    if not isinstance(item, dict) or "value" not in item:
        raise ValueError(f"missing Terraform output: {key}")
    return item["value"]


def bundle(repo: Path, leaf: Path) -> bytes:
    paths = list((repo / "gitops/charts/relay").rglob("*")) + [
        repo / "scripts" / name for name in ["run_eks_codebuild.sh", "bootstrap_eks_argocd.sh", "bootstrap_database.py", "reconcile_turn_ssm.py"]
    ]
    paths = [p for p in paths if p.is_file()]
    paths += list(leaf.glob("*"))
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for path in sorted(paths):
                relative = ("relay/" + os.environ["AWS_REGION"] + "/" + os.environ["RELAY_CUSTOMER_SLUG"] + "/" + path.name) if path.parent == leaf else path.relative_to(repo).as_posix()
                content = path.read_bytes()
                info = tarfile.TarInfo(relative)
                info.size = len(content)
                info.mode = 0o755 if path.suffix == ".sh" or path.name == "release-promotion" else 0o640
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = 0
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--leaf", type=Path, required=True)
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--context-key", required=True)
    parser.add_argument("--context-version", required=True)
    parser.add_argument("--context-sha256", required=True)
    parser.add_argument("--run-token", required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    leaf = (repo / args.leaf).resolve()
    leaf.relative_to(repo / ".relay")
    if not __import__("re").fullmatch(r"[0-9a-f]{40}", args.revision):
        raise ValueError("revision must be a full commit SHA")
    outputs = json.loads(args.terraform_output.read_text(encoding="utf-8"))
    config_bucket = output_value(outputs, "bucket_names")["config"]
    kms_key = output_value(outputs, "customer_kms_key_arn")
    project = output_value(outputs, "codebuild_project_name")
    payload = bundle(repo, leaf)
    digest = hashlib.sha256(payload).hexdigest()
    key = f"deploy-bundles/upgrades/{args.revision}/{leaf.name}.tar.gz"
    with tempfile.NamedTemporaryFile() as temp:
        temp.write(payload)
        temp.flush()
        response = aws_json(
            args.region,
            "s3api", "put-object", "--bucket", config_bucket, "--key", key,
            "--body", temp.name, "--server-side-encryption", "aws:kms",
            "--ssekms-key-id", kms_key, "--metadata", f"sha256={digest},git-sha={args.revision}",
        )
    version = response.get("VersionId")
    if not isinstance(version, str) or not version or version == "null":
        raise ValueError("configuration bucket did not version the EKS bundle")
    overrides = [
        {"name": "DEPLOY_BUNDLE_KEY", "value": key, "type": "PLAINTEXT"},
        {"name": "DEPLOY_BUNDLE_VERSION", "value": version, "type": "PLAINTEXT"},
        {"name": "DEPLOY_BUNDLE_SHA256", "value": digest, "type": "PLAINTEXT"},
        {"name": "DEPLOY_CONTEXT_KEY", "value": args.context_key, "type": "PLAINTEXT"},
        {"name": "DEPLOY_CONTEXT_VERSION", "value": args.context_version, "type": "PLAINTEXT"},
        {"name": "DEPLOY_CONTEXT_SHA256", "value": args.context_sha256, "type": "PLAINTEXT"},
        {"name": "GIT_REVISION", "value": args.revision, "type": "PLAINTEXT"},
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as temp:
        json.dump(overrides, temp, separators=(",", ":"))
        temp.flush()
        started = aws_json(
            args.region,
            "codebuild", "start-build", "--project-name", str(project),
            "--idempotency-token", args.run_token,
            "--environment-variables-override", f"file://{temp.name}",
        )
    build_id = (started.get("build") or {}).get("id")
    if not isinstance(build_id, str) or not build_id:
        raise ValueError("CodeBuild did not return a build ID")
    for _ in range(400):
        result = aws_json(args.region, "codebuild", "batch-get-builds", "--ids", build_id)
        builds = result.get("builds") or []
        status = builds[0].get("buildStatus") if builds else None
        if status == "SUCCEEDED":
            return 0
        if status in {"FAILED", "FAULT", "STOPPED", "TIMED_OUT"}:
            link = ((builds[0].get("logs") or {}).get("deepLink")) if builds else None
            raise RuntimeError(f"private EKS reconciliation ended with {status}: {link or 'no log link'}")
        time.sleep(15)
    raise TimeoutError("timed out waiting for private EKS reconciliation")


if __name__ == "__main__":
    raise SystemExit(main())
