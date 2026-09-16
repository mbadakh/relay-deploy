#!/usr/bin/env python3
"""Publish authenticated EC2 desired state and trigger reconciliation via SSM.

The workflow uploads an immutable, version-pinned bundle to the customer's
configuration bucket, signs its digest with a customer KMS signing key, then
atomically advances a small desired-state pointer. EC2 instances continuously
reconcile that pointer, so Auto Scaling replacements recover without SSH or a
second workflow run.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any


TERMINAL = {"Success", "Cancelled", "TimedOut", "Failed", "Cancelling"}
IMAGE_RE = re.compile(r"(?m)^\s*image\s*:\s*['\"]?([^'\"#\s]+)['\"]?\s*$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
MAX_SSM_ERROR_OUTPUT = 8_000


def invocation_failure(item: dict[str, Any]) -> str:
    instance_id = item.get("InstanceId", "unknown-instance")
    status = item.get("Status", "Unknown")
    output = "\n".join(
        str(plugin.get("Output", "")).strip()
        for plugin in item.get("CommandPlugins", [])
        if str(plugin.get("Output", "")).strip()
    )
    if not output:
        return f"{instance_id}={status}"
    if len(output) > MAX_SSM_ERROR_OUTPUT:
        output = output[-MAX_SSM_ERROR_OUTPUT:]
        output = f"[earlier SSM output omitted]\n{output}"
    return f"{instance_id}={status}:\n{output}"


def output_value(outputs: dict[str, Any], key: str) -> Any:
    item = outputs.get(key)
    if not isinstance(item, dict) or "value" not in item:
        raise ValueError(f"missing Terraform output: {key}")
    return item["value"]


def optional_output(outputs: dict[str, Any], key: str, default: Any) -> Any:
    item = outputs.get(key)
    return item["value"] if isinstance(item, dict) and "value" in item else default


def aws_json(*arguments: str, region: str) -> dict[str, Any]:
    result = subprocess.run(
        ["aws", *arguments, "--region", region, "--output", "json", "--no-cli-pager"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "AWS_PAGER": ""},
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("AWS CLI returned an unexpected response")
    return value


def wait_for_ssm_online(
    instance_ids: list[str], region: str, timeout: int = 900
) -> None:
    deadline = time.monotonic() + timeout
    expected = set(instance_ids)
    while time.monotonic() < deadline:
        information = aws_json(
            "ssm",
            "describe-instance-information",
            "--filters",
            "Key=InstanceIds,Values=" + ",".join(instance_ids),
            region=region,
        ).get("InstanceInformationList", [])
        online = {
            item["InstanceId"]
            for item in information
            if item.get("PingStatus") == "Online"
        }
        if online == expected:
            return
        time.sleep(10)
    raise TimeoutError("instances did not become SSM Online before deployment")


def deployment_instances(outputs: dict[str, Any], region: str) -> list[str]:
    direct_instances = optional_output(outputs, "ec2_instance_ids", [])
    if isinstance(direct_instances, list) and direct_instances:
        if not all(isinstance(item, str) and item.startswith("i-") for item in direct_instances):
            raise ValueError("EC2 deployment instance output is invalid")
        wait_for_ssm_online(direct_instances, region)
        return direct_instances
    asg_name = output_value(outputs, "ec2_asg_name")
    if not isinstance(asg_name, str) or not asg_name:
        raise ValueError("EC2 deployment requested without an ASG output")
    groups = aws_json(
        "autoscaling",
        "describe-auto-scaling-groups",
        "--auto-scaling-group-names",
        asg_name,
        region=region,
    ).get("AutoScalingGroups", [])
    if len(groups) != 1:
        raise ValueError("expected exactly one Relay Auto Scaling group")
    instances = [
        item["InstanceId"]
        for item in groups[0].get("Instances", [])
        if item.get("LifecycleState") == "InService"
    ]
    if not instances:
        raise ValueError("no in-service Relay EC2 instance is available")
    wait_for_ssm_online(instances, region)
    return instances


def read_deployment(
    leaf: Path, outputs: dict[str, Any], source_revision: str | None = None
) -> tuple[dict[str, bytes], dict[str, Any]]:
    compose = (leaf / "compose.yaml").read_bytes()
    realm = (leaf / "keycloak-realm.json").read_bytes()
    home_lab = optional_output(outputs, "home_lab", False) is True
    release = json.loads((leaf / "release.lock.json").read_text(encoding="utf-8"))
    if not isinstance(release, dict) or release.get("schemaVersion") != 1:
        raise ValueError("release.lock.json has an unsupported schema")
    try:
        realm_value = json.loads(realm)
    except json.JSONDecodeError as exc:
        raise ValueError("keycloak-realm.json is not valid JSON") from exc
    if not isinstance(realm_value, dict):
        raise ValueError("keycloak-realm.json must contain an object")

    repository_urls = output_value(outputs, "ecr_repository_urls")
    if not isinstance(repository_urls, dict):
        raise ValueError("ECR repository output is invalid")
    server_repository = str(repository_urls.get("relay-server", ""))
    keycloak_repository = str(repository_urls.get("relay-keycloak", ""))
    suffix = "/relay-server"
    if not server_repository.endswith(suffix):
        raise ValueError("Relay ECR repository output is invalid")
    customer_slug = server_repository[: -len(suffix)].rsplit("/", 1)[-1]
    if not SLUG_RE.fullmatch(customer_slug):
        raise ValueError("could not derive a safe customer slug from ECR")
    expected_prefix = server_repository[: -len(suffix)]
    if keycloak_repository != f"{expected_prefix}/relay-keycloak":
        raise ValueError("Relay and Keycloak ECR repositories disagree")

    images = release.get("images")
    if not isinstance(images, dict):
        raise ValueError("release lock does not contain images")
    server_descriptor = images.get("server")
    keycloak_descriptor = images.get("keycloak")
    if not isinstance(server_descriptor, dict) or not isinstance(keycloak_descriptor, dict):
        raise ValueError("release lock image descriptors are invalid")
    server_digest = str(server_descriptor.get("digest", ""))
    keycloak_digest = str(keycloak_descriptor.get("digest", ""))
    if not DIGEST_RE.fullmatch(server_digest) or not DIGEST_RE.fullmatch(keycloak_digest):
        raise ValueError("release images must be pinned by sha256 digest")
    expected_images = {
        f"{server_repository}@{server_digest}",
        f"{keycloak_repository}@{keycloak_digest}",
    }
    if home_lab:
        platform = output_value(outputs, "platform_image_digests")
        if not isinstance(platform, dict):
            raise ValueError("Home Lab platform image output is invalid")
        for component, repository_key in (
            ("coturn", "relay-coturn"),
            ("caddy", "relay-caddy"),
            ("postgres", "relay-postgres"),
        ):
            digest = str(platform.get(component, ""))
            repository = str(repository_urls.get(repository_key, ""))
            if not DIGEST_RE.fullmatch(digest) or not repository.startswith(
                expected_prefix + "/"
            ):
                raise ValueError(f"Home Lab {component} image output is invalid")
            expected_images.add(f"{repository}@{digest}")
    try:
        compose_text = compose.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("compose.yaml must be UTF-8") from exc
    actual_images = set(IMAGE_RE.findall(compose_text))
    if actual_images != expected_images:
        raise ValueError("Compose images do not match the promoted digest-pinned release")
    if "{{" in compose_text or "__TF_OUTPUT_" in compose_text:
        raise ValueError("Compose deployment contains an unresolved placeholder")

    files = {"compose.yaml": compose, "keycloak-realm.json": realm}
    if home_lab:
        caddyfile = (leaf / "Caddyfile").read_bytes()
        try:
            caddyfile_text = caddyfile.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Caddyfile must be UTF-8") from exc
        if "{{" in caddyfile_text or "__TF_OUTPUT_" in caddyfile_text:
            raise ValueError("Caddyfile contains an unresolved placeholder")
        files["Caddyfile"] = caddyfile
    manifest = {
        "schemaVersion": 1,
        "customerSlug": customer_slug,
        "applicationVersion": release.get("version"),
        "sourceRevision": source_revision or os.environ.get("GITHUB_SHA", "local"),
        "images": sorted(expected_images),
        "files": {
            name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for name, content in sorted(files.items())
        },
    }
    return files, manifest


def deterministic_bundle(files: dict[str, bytes], manifest: dict[str, Any]) -> bytes:
    members = {
        **files,
        "manifest.json": (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode(),
    }
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, content in sorted(members.items()):
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                info.mode = 0o640
                info.uid = 0
                info.gid = 0
                info.uname = "root"
                info.gname = "root"
                info.mtime = 0
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def put_versioned_object(
    *,
    body: Path,
    bucket: str,
    key: str,
    digest: str,
    kms_key_arn: str,
    region: str,
    content_type: str,
) -> str:
    expected_checksum = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
    response = aws_json(
        "s3api",
        "put-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--body",
        str(body),
        "--content-type",
        content_type,
        "--server-side-encryption",
        "aws:kms",
        "--ssekms-key-id",
        kms_key_arn,
        "--checksum-algorithm",
        "SHA256",
        "--metadata",
        f"sha256={digest},schema-version=1",
        region=region,
    )
    version_id = response.get("VersionId")
    if not isinstance(version_id, str) or not version_id or version_id == "null":
        raise ValueError("configuration bucket did not return an S3 VersionId")
    returned_checksum = response.get("ChecksumSHA256")
    if returned_checksum is not None and returned_checksum != expected_checksum:
        raise ValueError("S3 returned a different SHA256 checksum after upload")
    head = aws_json(
        "s3api",
        "head-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--version-id",
        version_id,
        "--checksum-mode",
        "ENABLED",
        region=region,
    )
    if (
        head.get("VersionId") != version_id
        or head.get("ServerSideEncryption") != "aws:kms"
        or head.get("SSEKMSKeyId") != kms_key_arn
        or (head.get("Metadata") or {}).get("sha256") != digest
        or head.get("ContentLength") != body.stat().st_size
    ):
        raise ValueError("uploaded S3 object failed metadata verification")
    head_checksum = head.get("ChecksumSHA256")
    if head_checksum is not None and head_checksum != expected_checksum:
        raise ValueError("uploaded S3 object failed checksum verification")
    return version_id


def sign_digest(digest: str, key_arn: str, region: str) -> str:
    with tempfile.NamedTemporaryFile() as stream:
        stream.write(bytes.fromhex(digest))
        stream.flush()
        response = aws_json(
            "kms",
            "sign",
            "--key-id",
            key_arn,
            "--message-type",
            "DIGEST",
            "--signing-algorithm",
            "ECDSA_SHA_256",
            "--message",
            f"fileb://{stream.name}",
            region=region,
        )
    signature = response.get("Signature")
    if response.get("KeyId") != key_arn or not isinstance(signature, str) or not signature:
        raise ValueError("KMS did not return the expected bundle signature")
    return signature


def publish_desired_state(
    leaf: Path,
    outputs: dict[str, Any],
    region: str,
    source_revision: str | None = None,
) -> tuple[str, str, str]:
    files, manifest = read_deployment(leaf, outputs, source_revision)
    bundle = deterministic_bundle(files, manifest)
    bundle_digest = hashlib.sha256(bundle).hexdigest()
    bucket_names = output_value(outputs, "bucket_names")
    if not isinstance(bucket_names, dict) or not bucket_names.get("config"):
        raise ValueError("configuration bucket output is invalid")
    bucket = str(bucket_names["config"])
    kms_key_arn = str(output_value(outputs, "customer_kms_key_arn"))
    signing_key_arn = str(output_value(outputs, "ec2_config_signing_key_arn"))
    if not signing_key_arn:
        raise ValueError("EC2 deployment has no KMS configuration signing key")
    bundle_key = f"ec2/bundles/sha256/{bundle_digest}.tar.gz"

    with tempfile.TemporaryDirectory() as directory:
        bundle_path = Path(directory) / "bundle.tar.gz"
        bundle_path.write_bytes(bundle)
        bundle_version = put_versioned_object(
            body=bundle_path,
            bucket=bucket,
            key=bundle_key,
            digest=bundle_digest,
            kms_key_arn=kms_key_arn,
            region=region,
            content_type="application/gzip",
        )
        signature = sign_digest(bundle_digest, signing_key_arn, region)
        pointer = {
            "schemaVersion": 1,
            "bundle": {
                "bucket": bucket,
                "key": bundle_key,
                "versionId": bundle_version,
                "sha256": bundle_digest,
                "size": len(bundle),
            },
            "signature": {
                "keyArn": signing_key_arn,
                "algorithm": "ECDSA_SHA_256",
                "value": signature,
            },
        }
        pointer_bytes = (
            json.dumps(pointer, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        pointer_path = Path(directory) / "desired-state.json"
        pointer_path.write_bytes(pointer_bytes)
        pointer_digest = hashlib.sha256(pointer_bytes).hexdigest()
        pointer_version = put_versioned_object(
            body=pointer_path,
            bucket=bucket,
            key="ec2/desired-state.json",
            digest=pointer_digest,
            kms_key_arn=kms_key_arn,
            region=region,
            content_type="application/json",
        )
    print(
        f"Published desired state bundle {bundle_digest} as S3 version {bundle_version}; "
        f"pointer version {pointer_version}."
    )
    return pointer_version, bundle_digest, str(manifest["applicationVersion"])


def trigger_reconciliation(
    instance_ids: list[str], pointer_version: str, application_version: str, region: str
) -> None:
    validate_android = """import json,sys,urllib.request
version=sys.argv[1]
metadata=json.load(urllib.request.urlopen('http://127.0.0.1:3001/api/app-updates/android/latest',timeout=10))
assert metadata['versionName']==version
assert metadata['downloadUrl']==f'/versions/relay-{version}.apk'
request=urllib.request.Request('http://127.0.0.1:3001'+metadata['downloadUrl'],method='HEAD')
response=urllib.request.urlopen(request,timeout=10)
assert int(response.headers['Content-Length'])==int(metadata['sizeBytes']) and int(metadata['sizeBytes'])>0
"""
    command = (
        "set -euo pipefail; "
        "for attempt in $(seq 1 180); do "
        "test -f /opt/relay/READY_FOR_GITOPS && break; sleep 5; done; "
        "test -f /opt/relay/READY_FOR_GITOPS; "
        "/usr/local/sbin/relay-ec2-reconcile --wait-lock 900 "
        f"--require-pointer-version {shlex.quote(pointer_version)}; "
        f"python3 -c {shlex.quote(validate_android)} {shlex.quote(application_version)}"
    )
    parameters = {"commands": [command], "executionTimeout": ["1800"]}
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json") as stream:
        json.dump(parameters, stream)
        stream.flush()
        response = aws_json(
            "ssm",
            "send-command",
            "--document-name",
            "AWS-RunShellScript",
            "--comment",
            "Relay signed desired-state reconciliation",
            "--instance-ids",
            *instance_ids,
            "--parameters",
            f"file://{stream.name}",
            "--timeout-seconds",
            "1800",
            region=region,
        )
    command_id = response["Command"]["CommandId"]
    deadline = time.monotonic() + 1900
    while time.monotonic() < deadline:
        invocations = aws_json(
            "ssm",
            "list-command-invocations",
            "--command-id",
            command_id,
            "--details",
            region=region,
        ).get("CommandInvocations", [])
        if len(invocations) == len(instance_ids) and all(
            item.get("Status") in TERMINAL for item in invocations
        ):
            failures = [item for item in invocations if item.get("Status") != "Success"]
            if failures:
                details = "\n\n".join(invocation_failure(item) for item in failures)
                raise RuntimeError(f"EC2 reconciliation failed:\n{details}")
            print(f"Desired state reconciled on {len(instance_ids)} EC2 instance(s).")
            return
        time.sleep(10)
    raise TimeoutError("timed out waiting for EC2 desired-state reconciliation")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--leaf", type=Path, required=True)
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--source-revision")
    args = parser.parse_args()
    if args.source_revision is not None and not re.fullmatch(
        r"[0-9a-f]{40}", args.source_revision
    ):
        raise ValueError("source revision must be a full Git commit SHA")
    outputs = json.loads(args.terraform_output.read_text(encoding="utf-8"))
    if output_value(outputs, "compute_mode") != "ec2":
        raise ValueError("deploy_ec2_ssm.py can only deploy the EC2 topology")
    instances = deployment_instances(outputs, args.region)
    pointer_version, _, application_version = publish_desired_state(
        args.leaf, outputs, args.region, args.source_revision
    )
    trigger_reconciliation(instances, pointer_version, application_version, args.region)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ValueError,
        RuntimeError,
        TimeoutError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
