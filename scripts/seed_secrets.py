#!/usr/bin/env python3
"""Seed empty Secrets Manager resources without putting values in TF state/logs."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import string
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path


DEFAULT_ANDROID_PACKAGE_NAME = "com.relay.messenger"
INITIAL_ADMIN_USERNAME = "relay-admin"
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def terraform_output(outputs: dict, key: str):
    item = outputs.get(key)
    if not isinstance(item, dict) or "value" not in item:
        raise ValueError(f"missing Terraform output {key}")
    return item["value"]


def aws_json(*arguments: str) -> dict:
    completed = subprocess.run(
        ["aws", *arguments, "--output", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def random_value(length: int = 48) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def normalized_admin_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise ValueError("initial administrator email is invalid")
    return email


def has_current_value(secret_arn: str) -> bool:
    try:
        metadata = aws_json("secretsmanager", "describe-secret", "--secret-id", secret_arn)
    except subprocess.CalledProcessError:
        return False
    stages = metadata.get("VersionIdsToStages", {})
    return any("AWSCURRENT" in value for value in stages.values())


def put_secret(secret_arn: str, value: dict) -> None:
    if has_current_value(secret_arn):
        return
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as stream:
        os.chmod(stream.name, 0o600)
        json.dump(value, stream, separators=(",", ":"))
        filename = stream.name
    try:
        subprocess.run(
            [
                "aws",
                "secretsmanager",
                "put-secret-value",
                "--secret-id",
                secret_arn,
                "--secret-string",
                f"file://{filename}",
                "--output",
                "json",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    finally:
        Path(filename).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--admin-email", required=True)
    args = parser.parse_args()
    outputs = json.loads(args.terraform_output.read_text(encoding="utf-8"))
    home_lab = terraform_output(outputs, "home_lab") is True
    relay_password = random_value(48)
    keycloak_password = random_value(48)
    user = urllib.parse.quote("relay_app", safe="")
    password = urllib.parse.quote(relay_password, safe="")
    endpoint = terraform_output(outputs, "database_endpoint")
    database_query = urllib.parse.urlencode(
        {"sslmode": "disable"}
        if home_lab
        else {
            "sslmode": "verify-full",
            "sslrootcert": "/etc/relay/rds-global-bundle.pem",
        }
    )
    database_url = (
        f"postgresql://{user}:{password}@{endpoint}:5432/relay?{database_query}"
    )
    buckets = terraform_output(outputs, "bucket_names")
    origin = terraform_output(outputs, "application_url").rstrip("/")
    admin_email = normalized_admin_email(args.admin_email)

    if home_lab:
        master_password = random_value(64)
        put_secret(
            terraform_output(outputs, "database_master_secret_arn"),
            {
                "username": "relay_admin",
                "password": master_password,
                "POSTGRES_USER": "relay_admin",
                "POSTGRES_PASSWORD": master_password,
                "POSTGRES_DB": "relay",
            },
        )

    runtime = {
        "DATABASE_URL": database_url,
        "DB_USERNAME": "relay_app",
        "DB_PASSWORD": relay_password,
        "JWT_SECRET": random_value(64),
        "MEDIA_GRANT_SECRET": random_value(64),
        "KEYCLOAK_ISSUER": f"{origin}/auth/realms/relay",
        "KEYCLOAK_CLIENT_ID": "relay",
        "RELAY_SESSION_ISSUER": origin,
        "RELAY_SESSION_AUDIENCE": "relay-api",
        "RELAY_SESSION_TTL_SECONDS": "604800",
        "CLIENT_ORIGIN": origin,
        "S3_BUCKET": buckets["media"],
        "S3_REGION": args.region,
        "S3_CREATE_BUCKET": "false",
        "S3_FORCE_PATH_STYLE": "false",
        "TURN_URL": str(terraform_output(outputs, "turn_url")),
        "TURN_USERNAME": "relay-" + random_value(20),
        "TURN_CREDENTIAL": random_value(64),
    }
    if home_lab:
        runtime["TURN_EXTERNAL_IP"] = str(
            terraform_output(outputs, "home_lab_public_ip")
        )
    runtime["KEYCLOAK_JWKS_URI"] = ("http://relay-" + str(terraform_output(outputs,"customer_slug")) + "-keycloak:8080" if terraform_output(outputs,"compute_mode")=="eks" else "http://keycloak:8080") + "/auth/realms/relay/protocol/openid-connect/certs"
    runtime["RELAY_SELF_HOSTED"] = "true"
    runtime["PUSH_GATEWAY_URL"] = "https://push.r3l4y.dev"
    runtime["PUSH_GATEWAY_SERVER_SECRET"] = secrets.token_urlsafe(32)
    put_secret(terraform_output(outputs, "runtime_secret_arn"), runtime)
    put_secret(
        terraform_output(outputs, "keycloak_secret_arn"),
        {
            "KC_BOOTSTRAP_ADMIN_USERNAME": "relay-bootstrap-admin",
            "KC_BOOTSTRAP_ADMIN_PASSWORD": random_value(64),
            "RELAY_INITIAL_ADMIN_USERNAME": INITIAL_ADMIN_USERNAME,
            "RELAY_INITIAL_ADMIN_EMAIL": admin_email,
            "RELAY_INITIAL_ADMIN_PASSWORD": random_value(64),
            "KC_DB": "postgres",
            "KC_DB_URL_HOST": endpoint,
            "KC_DB_URL_PORT": "5432",
            "KC_DB_URL_DATABASE": "keycloak",
            "KC_DB_URL_PROPERTIES": (
                "?sslmode=disable"
                if home_lab
                else "?sslmode=verify-full&sslrootcert=/etc/relay/rds-global-bundle.pem"
            ),
            "KC_DB_USERNAME": "keycloak_app",
            "KC_DB_PASSWORD": keycloak_password,
            "KC_HOSTNAME": f"{origin}/auth",
            "KC_HTTP_ENABLED": "true",
            "KC_HTTP_RELATIVE_PATH": "/auth",
            "KC_PROXY_HEADERS": "xforwarded",
        },
    )
    print("Seeded required secret versions; existing AWSCURRENT versions were preserved.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"error: secret seeding failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
