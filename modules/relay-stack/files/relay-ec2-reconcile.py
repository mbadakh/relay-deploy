#!/usr/bin/env python3
"""Reconcile a Relay Docker Compose host from an authenticated S3 bundle.

This program is installed by EC2 user data and executed by systemd. The desired
state pointer is mutable, but it pins an immutable S3 object version, its SHA256
digest, and a KMS signature. Secrets are fetched directly from Secrets Manager
and are never stored in the desired-state bundle or Terraform state.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path("/opt/relay")
RELEASES_DIR = BASE_DIR / "releases"
CURRENT_LINK = BASE_DIR / "current"
STATE_PATH = BASE_DIR / "reconciler-state.json"
REALM_PATH = BASE_DIR / "keycloak" / "realm" / "relay-realm.json"
RUNTIME_DIR = Path("/run/relay")
LOCK_PATH = Path("/run/lock/relay-reconcile.lock")
REQUIRED_MEMBERS = frozenset(("compose.yaml", "keycloak-realm.json", "manifest.json"))
ALLOWED_MEMBERS = REQUIRED_MEMBERS | {"Caddyfile"}
MAX_POINTER_BYTES = 64 * 1024
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BYTES = 16 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
POSTGRES_ENV_KEYS = ("POSTGRES_DB", "POSTGRES_PASSWORD", "POSTGRES_USER")


class ReconcileError(RuntimeError):
    """Desired state is invalid or could not be safely reconciled."""


def log(event: str, **fields: Any) -> None:
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReconcileError(f"cannot read JSON from {path.name}") from exc
    if not isinstance(value, dict):
        raise ReconcileError(f"{path.name} must contain a JSON object")
    return value


def run(
    command: list[str],
    *,
    input_bytes: bytes | None = None,
    capture: bool = True,
    timeout: int = 900,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment.update({"AWS_PAGER": "", "DOCKER_CONFIG": str(RUNTIME_DIR / "docker-config")})
    try:
        return subprocess.run(
            command,
            check=True,
            input=input_bytes,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReconcileError(f"command timed out: {command[0]} {command[1]}") from exc
    except subprocess.CalledProcessError as exc:
        # Never echo stdout: Secrets Manager and ECR authentication commands can
        # return sensitive material. The final stderr line is safe for these
        # fixed commands and is useful for operators.
        detail = exc.stderr.decode("utf-8", "replace").strip().splitlines()
        suffix = f": {detail[-1][:500]}" if detail else ""
        raise ReconcileError(f"command failed: {command[0]} {command[1]}{suffix}") from exc


def aws_json(config: dict[str, Any], *arguments: str) -> dict[str, Any]:
    result = run(
        [
            "aws",
            *arguments,
            "--region",
            config["region"],
            "--output",
            "json",
            "--no-cli-pager",
        ]
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReconcileError("AWS CLI returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise ReconcileError("AWS CLI returned an unexpected response")
    return value


def validate_config(config: dict[str, Any]) -> None:
    required = {
        "schemaVersion",
        "region",
        "configBucket",
        "desiredStateKey",
        "signingKeyArn",
        "dataKeyArn",
        "runtimeSecretArn",
        "keycloakSecretArn",
        "databaseSecretArn",
        "ecrRegistry",
        "customerSlug",
    }
    if config.get("schemaVersion") != 1 or not required.issubset(config):
        raise ReconcileError("unsupported or incomplete reconciler configuration")
    scalar_values = [config[key] for key in required if key != "schemaVersion"]
    if not all(isinstance(value, str) and value and "\x00" not in value for value in scalar_values):
        raise ReconcileError("reconciler configuration contains an invalid scalar")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", config["configBucket"]):
        raise ReconcileError("invalid config bucket")
    if config["desiredStateKey"] != "ec2/desired-state.json":
        raise ReconcileError("unexpected desired-state key")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", config["customerSlug"]):
        raise ReconcileError("invalid customer slug")
    if any(char.isspace() for char in config["ecrRegistry"]) or "/" in config["ecrRegistry"]:
        raise ReconcileError("invalid ECR registry")
    for key in (
        "signingKeyArn",
        "dataKeyArn",
        "runtimeSecretArn",
        "keycloakSecretArn",
        "databaseSecretArn",
    ):
        if not config[key].startswith("arn:"):
            raise ReconcileError(f"invalid ARN in {key}")


def get_s3_object(
    config: dict[str, Any], key: str, destination: Path, *, version_id: str | None = None
) -> dict[str, Any] | None:
    command = [
        "aws",
        "s3api",
        "get-object",
        "--bucket",
        config["configBucket"],
        "--key",
        key,
    ]
    if version_id is not None:
        command.extend(("--version-id", version_id))
    command.extend(
        (
            "--region",
            config["region"],
            "--output",
            "json",
            "--no-cli-pager",
            str(destination),
        )
    )
    environment = os.environ.copy()
    environment["AWS_PAGER"] = ""
    result = subprocess.run(command, capture_output=True, env=environment)
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", "replace")
        if version_id is None and ("NoSuchKey" in error or "404" in error):
            return None
        detail = error.strip().splitlines()
        suffix = f": {detail[-1][:500]}" if detail else ""
        raise ReconcileError(f"could not retrieve desired-state object{suffix}")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReconcileError("S3 get-object returned malformed metadata") from exc
    if not isinstance(response, dict):
        raise ReconcileError("S3 get-object returned unexpected metadata")
    if response.get("ServerSideEncryption") != "aws:kms":
        raise ReconcileError("desired state is not encrypted with AWS KMS")
    if response.get("SSEKMSKeyId") != config["dataKeyArn"]:
        raise ReconcileError("desired state uses the wrong KMS encryption key")
    if version_id is not None and response.get("VersionId") != version_id:
        raise ReconcileError("S3 returned a different object version than requested")
    return response


def load_pointer(config: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=RUNTIME_DIR, prefix="desired-", suffix=".json") as stream:
        response = get_s3_object(config, config["desiredStateKey"], Path(stream.name))
        if response is None:
            return None
        size = Path(stream.name).stat().st_size
        if size <= 0 or size > MAX_POINTER_BYTES:
            raise ReconcileError("desired-state pointer has an invalid size")
        pointer = load_json(Path(stream.name))
    pointer_version = response.get("VersionId")
    if not isinstance(pointer_version, str) or not pointer_version:
        raise ReconcileError("desired-state pointer is not versioned")
    validate_pointer(config, pointer)
    return pointer, pointer_version


def validate_pointer(config: dict[str, Any], pointer: dict[str, Any]) -> None:
    if pointer.get("schemaVersion") != 1:
        raise ReconcileError("unsupported desired-state schema")
    bundle = pointer.get("bundle")
    signature = pointer.get("signature")
    if not isinstance(bundle, dict) or not isinstance(signature, dict):
        raise ReconcileError("desired state is missing bundle authentication data")
    digest = bundle.get("sha256")
    expected_key = f"ec2/bundles/sha256/{digest}.tar.gz"
    if (
        not isinstance(digest, str)
        or not SHA256_RE.fullmatch(digest)
        or bundle.get("bucket") != config["configBucket"]
        or bundle.get("key") != expected_key
        or not isinstance(bundle.get("versionId"), str)
        or not bundle["versionId"]
        or not isinstance(bundle.get("size"), int)
        or not 0 < bundle["size"] <= MAX_BUNDLE_BYTES
    ):
        raise ReconcileError("desired state contains an invalid bundle reference")
    if any(ord(character) < 32 for character in bundle["versionId"]):
        raise ReconcileError("desired state contains an invalid S3 version ID")
    if (
        signature.get("keyArn") != config["signingKeyArn"]
        or signature.get("algorithm") != "ECDSA_SHA_256"
        or not isinstance(signature.get("value"), str)
        or len(signature["value"]) > 2048
    ):
        raise ReconcileError("desired state contains invalid signature metadata")


def fetch_secret(config: dict[str, Any], arn: str) -> tuple[dict[str, Any], str]:
    response = aws_json(
        config,
        "secretsmanager",
        "get-secret-value",
        "--secret-id",
        arn,
        "--version-stage",
        "AWSCURRENT",
    )
    version_id = response.get("VersionId")
    secret_string = response.get("SecretString")
    if not isinstance(version_id, str) or not version_id or not isinstance(secret_string, str):
        raise ReconcileError("Secrets Manager returned an incomplete secret version")
    try:
        secret = json.loads(secret_string)
    except json.JSONDecodeError as exc:
        raise ReconcileError("Secrets Manager value is not valid JSON") from exc
    if not isinstance(secret, dict):
        raise ReconcileError("Secrets Manager value must be a JSON object")
    return secret, version_id


def environment_file(values: dict[str, Any]) -> bytes:
    lines: list[str] = []
    for key, value in sorted(values.items()):
        if not isinstance(key, str) or not ENV_KEY_RE.fullmatch(key):
            raise ReconcileError("secret contains an unsafe environment key")
        text = str(value)
        if any(character in text for character in ("\n", "\r", "\x00")):
            raise ReconcileError("secret contains an unsafe environment value")
        lines.append(f"{key}={text}")
    if not lines:
        raise ReconcileError("secret contains no environment values")
    return ("\n".join(lines) + "\n").encode()


def postgres_environment_file(values: dict[str, Any]) -> bytes:
    """Project a database secret onto the explicit Compose environment contract.

    AWS-managed database secrets contain lowercase metadata, and Home Lab
    secrets retain lowercase username/password aliases for database bootstrap.
    Neither shape may be copied wholesale into a Docker environment file.
    """
    username = values.get("username")
    password = values.get("password")
    if (
        not isinstance(username, str)
        or not username
        or not isinstance(password, str)
        or not password
    ):
        raise ReconcileError("database secret is missing PostgreSQL credentials")
    projected = {
        "POSTGRES_DB": "relay",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_USER": username,
    }
    for key in POSTGRES_ENV_KEYS:
        if key in values and values[key] != projected[key]:
            raise ReconcileError(
                "database secret contains inconsistent PostgreSQL values"
            )
    return environment_file(projected)


def verify_bundle_signature(
    config: dict[str, Any], digest: str, signature_value: str
) -> None:
    try:
        signature = base64.b64decode(signature_value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReconcileError("bundle signature is not valid base64") from exc
    if not 64 <= len(signature) <= 256:
        raise ReconcileError("bundle signature has an invalid length")
    with tempfile.TemporaryDirectory(dir=RUNTIME_DIR, prefix="signature-") as directory:
        digest_path = Path(directory) / "digest.bin"
        signature_path = Path(directory) / "signature.bin"
        digest_path.write_bytes(bytes.fromhex(digest))
        signature_path.write_bytes(signature)
        result = aws_json(
            config,
            "kms",
            "verify",
            "--key-id",
            config["signingKeyArn"],
            "--message-type",
            "DIGEST",
            "--signing-algorithm",
            "ECDSA_SHA_256",
            "--message",
            f"fileb://{digest_path}",
            "--signature",
            f"fileb://{signature_path}",
        )
    if result.get("SignatureValid") is not True or result.get("KeyId") != config["signingKeyArn"]:
        raise ReconcileError("KMS rejected the desired-state bundle signature")


def read_bundle(bundle_path: Path) -> dict[str, bytes]:
    try:
        with tarfile.open(bundle_path, mode="r:gz") as archive:
            members = archive.getmembers()
            names = {member.name for member in members}
            if (
                not REQUIRED_MEMBERS.issubset(names)
                or not names.issubset(ALLOWED_MEMBERS)
                or len(members) != len(names)
            ):
                raise ReconcileError("bundle contains unexpected files")
            if any(not member.isfile() or member.size < 0 for member in members):
                raise ReconcileError("bundle may contain only regular files")
            if sum(member.size for member in members) > MAX_EXPANDED_BYTES:
                raise ReconcileError("expanded bundle is too large")
            contents: dict[str, bytes] = {}
            for member in members:
                source = archive.extractfile(member)
                if source is None:
                    raise ReconcileError("bundle member could not be read")
                contents[member.name] = source.read()
    except (tarfile.TarError, OSError) as exc:
        raise ReconcileError("bundle is not a valid gzip-compressed tar archive") from exc
    return contents


def validate_manifest(
    config: dict[str, Any], contents: dict[str, bytes], bundle_digest: str
) -> dict[str, Any]:
    try:
        manifest = json.loads(contents["manifest.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconcileError("bundle manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        raise ReconcileError("unsupported bundle manifest schema")
    if manifest.get("customerSlug") != config["customerSlug"]:
        raise ReconcileError("bundle was generated for another customer")
    files = manifest.get("files")
    images = manifest.get("images")
    expected_files = set(contents) - {"manifest.json"}
    if not isinstance(files, dict) or set(files) != expected_files:
        raise ReconcileError("bundle manifest has an invalid file inventory")
    for name in files:
        descriptor = files[name]
        if not isinstance(descriptor, dict):
            raise ReconcileError("bundle manifest has an invalid file descriptor")
        content = contents[name]
        if descriptor.get("size") != len(content):
            raise ReconcileError(f"bundle member size does not match: {name}")
        if descriptor.get("sha256") != hashlib.sha256(content).hexdigest():
            raise ReconcileError(f"bundle member digest does not match: {name}")
    if (
        not isinstance(images, list)
        or not 2 <= len(images) <= 5
        or len(set(images)) != len(images)
    ):
        raise ReconcileError("bundle must declare two to five unique workload images")
    required_repositories = {
        f"{config['ecrRegistry']}/{config['customerSlug']}/relay-server",
        f"{config['ecrRegistry']}/{config['customerSlug']}/relay-keycloak",
    }
    allowed_repositories = required_repositories | {
        f"{config['ecrRegistry']}/{config['customerSlug']}/relay-coturn",
        f"{config['ecrRegistry']}/{config['customerSlug']}/relay-caddy",
        f"{config['ecrRegistry']}/{config['customerSlug']}/relay-postgres",
    }
    repositories: set[str] = set()
    for image in images:
        if not isinstance(image, str) or not DIGEST_IMAGE_RE.fullmatch(image):
            raise ReconcileError("bundle contains a mutable or invalid workload image")
        repositories.add(image.split("@", 1)[0])
    if not required_repositories.issubset(repositories) or not repositories.issubset(
        allowed_repositories
    ):
        raise ReconcileError("bundle references an image outside the customer ECR repositories")
    caddy_repository = (
        f"{config['ecrRegistry']}/{config['customerSlug']}/relay-caddy"
    )
    has_caddy = caddy_repository in repositories
    if ("Caddyfile" in files) != has_caddy:
        raise ReconcileError("bundle Caddy configuration does not match its images")
    manifest["bundleSha256"] = bundle_digest
    return manifest


def install_release(contents: dict[str, bytes], digest: str) -> Path:
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    release = RELEASES_DIR / digest
    expected = {name: contents[name] for name in sorted(contents)}
    if release.exists():
        if release.is_symlink() or not release.is_dir():
            raise ReconcileError("existing release path has an unsafe type")
        if all(
            (release / name).is_file()
            and not (release / name).is_symlink()
            and (release / name).read_bytes() == content
            for name, content in expected.items()
        ):
            return release
        raise ReconcileError("existing content-addressed release is corrupt")
    staging = RELEASES_DIR / f".{digest}.{uuid.uuid4().hex}.staging"
    staging.mkdir(mode=0o750)
    try:
        for name, content in expected.items():
            target = staging / name
            target.write_bytes(content)
            target.chmod(0o640)
        os.rename(staging, release)
    finally:
        if staging.exists() and staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging)
    return release


def compose_images(config: dict[str, Any], compose_path: Path) -> set[str]:
    text = compose_path.read_text(encoding="utf-8")
    forbidden = (
        r"(?mi)^\s*privileged\s*:\s*true\s*$",
        r"(?mi)^\s*(?:pid|ipc)\s*:\s*['\"]?host['\"]?\s*$",
        r"(?mi)^\s*(?:devices|volumes_from)\s*:",
        r"/var/run/docker\.sock",
    )
    if any(re.search(pattern, text) for pattern in forbidden):
        raise ReconcileError("Compose bundle requests a forbidden host privilege")
    result = run(
        ["docker", "compose", "-f", str(compose_path), "config", "--format", "json"],
        timeout=120,
    )
    try:
        rendered = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconcileError("Docker Compose returned invalid configuration") from exc
    services = rendered.get("services") if isinstance(rendered, dict) else None
    if not isinstance(services, dict) or not services:
        raise ReconcileError("Compose bundle contains no services")

    # Home Lab needs host networking only for its bounded TURN relay. Preserve
    # the default denial for every other workload and fail closed on added
    # Linux capabilities.
    allowed_capabilities = {
        "postgres": {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"},
        "caddy": {"NET_BIND_SERVICE"},
        "coturn": {"NET_BIND_SERVICE"},
    }
    images: set[str] = set()
    expected_turn_repository = (
        f"{config['ecrRegistry']}/{config['customerSlug']}/relay-coturn@sha256:"
    )
    for name, service in services.items():
        if not isinstance(service, dict):
            raise ReconcileError("Compose bundle contains an invalid service")
        image = service.get("image")
        if not isinstance(image, str) or not image:
            raise ReconcileError("Compose service has no immutable image")
        images.add(image)
        network_mode = service.get("network_mode")
        if network_mode == "host" and not (
            name == "coturn" and image.startswith(expected_turn_repository)
        ):
            raise ReconcileError("Compose bundle requests forbidden host networking")
        if network_mode not in (None, "", "default", "host"):
            raise ReconcileError("Compose bundle requests an unsupported network mode")
        capabilities = {
            str(item).removeprefix("CAP_").upper()
            for item in (service.get("cap_add") or [])
        }
        if not capabilities.issubset(allowed_capabilities.get(name, set())):
            raise ReconcileError("Compose bundle requests a forbidden Linux capability")
    return images


def running_images_match(compose_path: Path, expected_images: set[str]) -> bool:
    """Detect container/image drift before taking the no-op fast path."""
    try:
        identifiers = run(
            ["docker", "compose", "-f", str(compose_path), "ps", "-q"],
            timeout=60,
        ).stdout.decode("utf-8", "strict").splitlines()
        identifiers = [identifier.strip() for identifier in identifiers if identifier.strip()]
        if len(identifiers) != len(expected_images):
            return False
        result = run(
            ["docker", "inspect", "--format", "{{.Config.Image}}", *identifiers],
            timeout=60,
        )
        actual = {
            line.strip()
            for line in result.stdout.decode("utf-8", "strict").splitlines()
            if line.strip()
        }
        return actual == expected_images
    except (ReconcileError, UnicodeError):
        return False


def endpoint_healthy(url: str, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def workloads_healthy() -> bool:
    return endpoint_healthy("http://127.0.0.1:3001/api/health") and endpoint_healthy(
        "http://127.0.0.1:8080/auth/realms/relay"
    )


def wait_for_workloads(timeout: int = 360) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if workloads_healthy():
            return
        time.sleep(5)
    raise ReconcileError("Relay or Keycloak did not become healthy")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file() or STATE_PATH.is_symlink():
        return {}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def current_release() -> Path | None:
    if not CURRENT_LINK.is_symlink():
        return None
    try:
        target = CURRENT_LINK.resolve(strict=True)
        target.relative_to(RELEASES_DIR.resolve())
    except (OSError, ValueError):
        return None
    return target if target.is_dir() and (target / "compose.yaml").is_file() else None


def set_current(release: Path) -> None:
    temporary = BASE_DIR / f".current.{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(release)
        os.replace(temporary, CURRENT_LINK)
    finally:
        temporary.unlink(missing_ok=True)


def docker_login(config: dict[str, Any]) -> None:
    docker_config = RUNTIME_DIR / "docker-config"
    docker_config.mkdir(parents=True, exist_ok=True, mode=0o700)
    password = run(
        [
            "aws",
            "ecr",
            "get-login-password",
            "--region",
            config["region"],
            "--no-cli-pager",
        ]
    ).stdout
    run(
        [
            "docker",
            "login",
            "--username",
            "AWS",
            "--password-stdin",
            config["ecrRegistry"],
        ],
        input_bytes=password,
        timeout=120,
    )


def activate_release(config: dict[str, Any], release: Path, realm: bytes) -> None:
    previous = current_release()
    previous_realm = REALM_PATH.read_bytes() if REALM_PATH.is_file() else None
    compose = release / "compose.yaml"
    try:
        docker_login(config)
        run(["docker", "compose", "-f", str(compose), "pull"], timeout=900)
    finally:
        docker_config = RUNTIME_DIR / "docker-config"
        if docker_config.is_dir() and not docker_config.is_symlink():
            shutil.rmtree(docker_config)
    atomic_write(REALM_PATH, realm, 0o640)
    try:
        run(
            [
                "docker",
                "compose",
                "-f",
                str(compose),
                "up",
                "-d",
                "--remove-orphans",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "300",
            ],
            timeout=600,
        )
        wait_for_workloads()
    except ReconcileError:
        if previous_realm is not None:
            atomic_write(REALM_PATH, previous_realm, 0o640)
        if previous is not None:
            try:
                run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(previous / "compose.yaml"),
                        "up",
                        "-d",
                        "--remove-orphans",
                        "--force-recreate",
                        "--wait",
                        "--wait-timeout",
                        "300",
                    ],
                    timeout=600,
                )
                wait_for_workloads()
                log("rollback_succeeded", release=previous.name)
            except ReconcileError:
                log("rollback_failed", release=previous.name)
        raise


def prune_releases(keep: set[Path], retain: int = 4) -> None:
    candidates = sorted(
        (
            path
            for path in RELEASES_DIR.iterdir()
            if path.is_dir() and not path.is_symlink() and SHA256_RE.fullmatch(path.name)
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates[retain:]:
        if path not in keep:
            shutil.rmtree(path)


def reconcile(config: dict[str, Any]) -> None:
    pointer_result = load_pointer(config)
    if pointer_result is None:
        log("desired_state_absent")
        return
    pointer, pointer_version = pointer_result
    bundle = pointer["bundle"]

    runtime_secret, runtime_version = fetch_secret(config, config["runtimeSecretArn"])
    keycloak_secret, keycloak_version = fetch_secret(config, config["keycloakSecretArn"])
    database_secret, database_version = fetch_secret(config, config["databaseSecretArn"])
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(RUNTIME_DIR / "server.env", environment_file(runtime_secret), 0o600)
    atomic_write(RUNTIME_DIR / "keycloak.env", environment_file(keycloak_secret), 0o600)
    atomic_write(
        RUNTIME_DIR / "postgres.env",
        postgres_environment_file(database_secret),
        0o600,
    )
    secret_versions = {
        "runtime": runtime_version,
        "keycloak": keycloak_version,
        "database": database_version,
    }

    state = load_state()
    active = current_release()
    expected_active_images: set[str] = set()
    if active is not None:
        try:
            active_manifest = load_json(active / "manifest.json")
            declared_images = active_manifest.get("images")
            if isinstance(declared_images, list) and all(
                isinstance(image, str) for image in declared_images
            ):
                expected_active_images = set(declared_images)
        except ReconcileError:
            expected_active_images = set()
    if (
        state.get("schemaVersion") == 1
        and state.get("pointerVersionId") == pointer_version
        and state.get("bundleVersionId") == bundle["versionId"]
        and state.get("bundleSha256") == bundle["sha256"]
        and state.get("secretVersionIds") == secret_versions
        and active is not None
        and active.name == bundle["sha256"]
        and 2 <= len(expected_active_images) <= 5
        and running_images_match(active / "compose.yaml", expected_active_images)
        and workloads_healthy()
    ):
        log("already_in_sync", bundle=bundle["sha256"], pointer_version=pointer_version)
        return

    with tempfile.NamedTemporaryFile(dir=RUNTIME_DIR, prefix="bundle-", suffix=".tar.gz") as stream:
        response = get_s3_object(
            config,
            bundle["key"],
            Path(stream.name),
            version_id=bundle["versionId"],
        )
        if response is None:
            raise ReconcileError("versioned bundle disappeared")
        bundle_size = Path(stream.name).stat().st_size
        if bundle_size != bundle["size"] or bundle_size > MAX_BUNDLE_BYTES:
            raise ReconcileError("downloaded bundle size does not match desired state")
        digest = hashlib.sha256(Path(stream.name).read_bytes()).hexdigest()
        if digest != bundle["sha256"]:
            raise ReconcileError("downloaded bundle digest does not match desired state")
        verify_bundle_signature(config, digest, pointer["signature"]["value"])
        contents = read_bundle(Path(stream.name))

    manifest = validate_manifest(config, contents, digest)
    release = install_release(contents, digest)
    images = compose_images(config, release / "compose.yaml")
    if images != set(manifest["images"]):
        raise ReconcileError("resolved Compose images do not match the signed manifest")

    previous = current_release()
    activate_release(config, release, contents["keycloak-realm.json"])
    set_current(release)
    new_state = {
        "schemaVersion": 1,
        "pointerVersionId": pointer_version,
        "bundleVersionId": bundle["versionId"],
        "bundleSha256": digest,
        "secretVersionIds": secret_versions,
        "applicationVersion": manifest.get("applicationVersion"),
        "appliedAt": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write(
        STATE_PATH,
        (json.dumps(new_state, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        0o600,
    )
    keep = {release}
    if previous is not None:
        keep.add(previous)
    prune_releases(keep)
    log("reconcile_succeeded", bundle=digest, pointer_version=pointer_version)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("/etc/relay/reconciler.json"))
    parser.add_argument("--wait-lock", type=int, default=0, metavar="SECONDS")
    parser.add_argument("--require-pointer-version")
    args = parser.parse_args()
    if args.wait_lock < 0 or args.wait_lock > 1800:
        raise ReconcileError("--wait-lock must be between 0 and 1800 seconds")
    config = load_json(args.config)
    validate_config(config)

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        deadline = time.monotonic() + args.wait_lock
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    if args.require_pointer_version:
                        raise ReconcileError("another reconciliation held the lock too long")
                    log("reconcile_already_running")
                    return 0
                time.sleep(2)
        reconcile(config)
        if args.require_pointer_version:
            state = load_state()
            if state.get("pointerVersionId") != args.require_pointer_version:
                raise ReconcileError("required desired-state version was not applied")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ReconcileError) as exc:
        log("reconcile_failed", error=str(exc))
        raise SystemExit(2)
