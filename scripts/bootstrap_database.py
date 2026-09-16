#!/usr/bin/env python3
"""Create least-privilege Relay/Keycloak database principals after apply.

The database is private. EC2 profiles run the bootstrap in an SSM-managed
instance; EKS profiles run a short-lived PostgreSQL client Job in the cluster.
No database password is placed in Git, Terraform state, command arguments, or
workflow output.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path


SQL = r"""\set ON_ERROR_STOP on
\getenv relay_password RELAY_PASSWORD
\getenv keycloak_password KEYCLOAK_PASSWORD
SELECT 'CREATE ROLE relay_app LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'relay_app')\gexec
ALTER ROLE relay_app WITH LOGIN PASSWORD :'relay_password';

SELECT 'CREATE ROLE keycloak_app LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'keycloak_app')\gexec
ALTER ROLE keycloak_app WITH LOGIN PASSWORD :'keycloak_password';
SELECT 'CREATE DATABASE keycloak'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'keycloak')\gexec
REVOKE ALL ON DATABASE relay FROM PUBLIC;
REVOKE ALL ON DATABASE keycloak FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE relay TO relay_app;
GRANT CONNECT, TEMPORARY ON DATABASE keycloak TO keycloak_app;
\connect relay
GRANT USAGE, CREATE ON SCHEMA public TO relay_app;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO relay_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO relay_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES TO relay_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO relay_app;
\connect keycloak
GRANT USAGE, CREATE ON SCHEMA public TO keycloak_app;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO keycloak_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO keycloak_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES TO keycloak_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO keycloak_app;
"""

TERMINAL = {"Success", "Cancelled", "TimedOut", "Failed", "Cancelling"}
POSTGRES_BOOTSTRAP_IMAGE = (
    "docker.io/library/postgres@"
    "sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685"
)


def output_value(outputs: dict, key: str):
    item = outputs.get(key)
    if not isinstance(item, dict) or "value" not in item:
        raise ValueError(f"missing Terraform output: {key}")
    return item["value"]


def optional_output(outputs: dict, key: str, default):
    item = outputs.get(key)
    return item["value"] if isinstance(item, dict) and "value" in item else default


def run(*command: str, input_text: str | None = None, capture: bool = False):
    return subprocess.run(
        list(command),
        check=True,
        input=input_text,
        text=True,
        capture_output=capture,
    )


def aws_json(*arguments: str) -> dict:
    result = run("aws", *arguments, "--output", "json", capture=True)
    return json.loads(result.stdout)


def get_secret(arn: str) -> dict:
    value = aws_json("secretsmanager", "get-secret-value", "--secret-id", arn)
    secret = json.loads(value["SecretString"])
    if not isinstance(secret, dict):
        raise ValueError("Secrets Manager value must be a JSON object")
    return secret


def database_values(outputs: dict) -> dict[str, str]:
    master = get_secret(output_value(outputs, "database_master_secret_arn"))
    relay = get_secret(output_value(outputs, "runtime_secret_arn"))
    keycloak = get_secret(output_value(outputs, "keycloak_secret_arn"))
    values = {
        "host": str(output_value(outputs, "database_endpoint")),
        "master_username": str(master["username"]),
        "master_password": str(master["password"]),
        "relay_password": str(relay["DB_PASSWORD"]),
        "keycloak_password": str(keycloak["KC_DB_PASSWORD"]),
    }
    if any("\n" in value or "\x00" in value for value in values.values()):
        raise ValueError("database credentials contain forbidden control characters")
    return values


def wait_for_asg_instances(asg_name: str, timeout: int = 900) -> list[str]:
    """Wait for healthy instances running the ASG's current launch template."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        groups = aws_json(
            "autoscaling", "describe-auto-scaling-groups",
            "--auto-scaling-group-names", asg_name,
        ).get("AutoScalingGroups", [])
        if len(groups) != 1:
            raise ValueError("expected exactly one Relay Auto Scaling group")
        group = groups[0]
        desired = int(group.get("DesiredCapacity", 0))
        target = group.get("LaunchTemplate") or {}
        target_id = str(target.get("LaunchTemplateId", ""))
        target_version = str(target.get("Version", ""))
        if desired < 1 or not target_id or not target_version:
            raise ValueError("Relay Auto Scaling group has an invalid launch template")
        instances = [
            item["InstanceId"]
            for item in group.get("Instances", [])
            if item.get("LifecycleState") == "InService"
            and item.get("HealthStatus") == "Healthy"
            and str((item.get("LaunchTemplate") or {}).get("LaunchTemplateId", ""))
            == target_id
            and str((item.get("LaunchTemplate") or {}).get("Version", ""))
            == target_version
        ]
        if len(instances) >= desired:
            return instances
        time.sleep(10)
    raise TimeoutError(
        "timed out waiting for healthy Relay instances on the current launch template"
    )


def wait_for_ssm_online(instance_ids: list[str], region: str, timeout: int = 900) -> None:
    expected = set(instance_ids)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = aws_json(
            "ssm",
            "describe-instance-information",
            "--filters",
            "Key=InstanceIds,Values=" + ",".join(instance_ids),
            "--region",
            region,
        ).get("InstanceInformationList", [])
        online = {
            item["InstanceId"]
            for item in current
            if item.get("PingStatus") == "Online"
        }
        if online == expected:
            return
        time.sleep(10)
    raise TimeoutError("EC2 database-bootstrap target did not become SSM Online")


def bootstrap_ec2(outputs: dict, region: str) -> None:
    home_lab = optional_output(outputs, "home_lab", False) is True
    direct_instances = optional_output(outputs, "ec2_instance_ids", [])
    instances = (
        [str(item) for item in direct_instances]
        if home_lab and isinstance(direct_instances, list) and direct_instances
        else wait_for_asg_instances(str(output_value(outputs, "ec2_asg_name")))
    )
    wait_for_ssm_online(instances, region)
    sql_b64 = base64.b64encode(SQL.encode()).decode("ascii")
    master_arn = str(output_value(outputs, "database_master_secret_arn"))
    runtime_arn = str(output_value(outputs, "runtime_secret_arn"))
    keycloak_arn = str(output_value(outputs, "keycloak_secret_arn"))
    endpoint = str(
        optional_output(
            outputs,
            "database_bootstrap_endpoint",
            output_value(outputs, "database_endpoint"),
        )
    )
    postgres_setup = ""
    database_command = f'''docker run --rm --network host --env-file "$work/db.env" \\
  -v "$work/bootstrap.sql:/bootstrap.sql:ro" {POSTGRES_BOOTSTRAP_IMAGE} \\
  psql --no-password --file=/bootstrap.sql'''
    if home_lab:
        image = str(output_value(outputs, "ecr_repository_urls")["relay-postgres"])
        digest = str(output_value(outputs, "platform_image_digests")["postgres"])
        volume = str(output_value(outputs, "home_lab_postgres_volume_name"))
        project = volume.removesuffix("-postgres-data")
        if project == volume:
            raise ValueError("Home Lab PostgreSQL volume name is invalid")
        registry = image.split("/", 1)[0]
        postgres_setup = f"""
aws ecr get-login-password --region {shlex.quote(region)} | docker login --username AWS --password-stdin {shlex.quote(registry)}
docker pull {shlex.quote(image + '@' + digest)}
docker volume create {shlex.quote(volume)} >/dev/null
docker rm --force relay-postgres-bootstrap >/dev/null 2>&1 || true
master_user="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[\"username\"])' \"$work/master.json\")"
master_password="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[\"password\"])' \"$work/master.json\")"
postgres_containers="$(docker ps -q \\
  --filter {shlex.quote('label=com.docker.compose.project=' + project)} \\
  --filter 'label=com.docker.compose.service=postgres')"
if [[ "$postgres_containers" == *$'\n'* ]]; then
  echo 'More than one Home Lab PostgreSQL service is running' >&2
  exit 1
elif [[ -n "$postgres_containers" ]]; then
  postgres_container="$postgres_containers"
else
  docker run --detach --name relay-postgres-bootstrap --network host \\
    -e POSTGRES_USER="$master_user" -e POSTGRES_PASSWORD="$master_password" -e POSTGRES_DB=relay \\
    -e POSTGRES_INITDB_ARGS='--auth-host=scram-sha-256 --auth-local=scram-sha-256' \\
    -v {shlex.quote(volume)}:/var/lib/postgresql/data \\
    {shlex.quote(image + '@' + digest)} >/dev/null
  postgres_container=relay-postgres-bootstrap
  bootstrap_postgres_started=true
fi
for attempt in $(seq 1 60); do
  docker exec "$postgres_container" pg_isready -U "$master_user" -d relay >/dev/null 2>&1 && break
  sleep 2
done
docker exec "$postgres_container" pg_isready -U "$master_user" -d relay >/dev/null
"""
        database_command = '''docker exec -i --env-file "$work/db.env" "$postgres_container" \\
  psql --no-password --file=- < "$work/bootstrap.sql"'''
    parser = r'''import json, pathlib, sys
master = json.loads(pathlib.Path(sys.argv[1]).read_text())
relay = json.loads(pathlib.Path(sys.argv[2]).read_text())
keycloak = json.loads(pathlib.Path(sys.argv[3]).read_text())
values = {
    "PGHOST": sys.argv[4], "PGPORT": "5432", "PGDATABASE": "postgres",
    "PGUSER": str(master["username"]), "PGPASSWORD": str(master["password"]),
    "RELAY_PASSWORD": str(relay["DB_PASSWORD"]),
    "KEYCLOAK_PASSWORD": str(keycloak["KC_DB_PASSWORD"]),
}
if any("\n" in value or "\x00" in value for value in values.values()):
    raise SystemExit("unsafe database credential")
pathlib.Path(sys.argv[5]).write_text("".join(f"{k}={v}\n" for k, v in values.items()))
'''
    parser_b64 = base64.b64encode(parser.encode()).decode("ascii")
    remote = f"""#!/usr/bin/env bash
set -euo pipefail
umask 077
ready=false
for attempt in $(seq 1 180); do
  if test -f /opt/relay/READY_FOR_GITOPS && systemctl is-active --quiet docker; then
    ready=true
    break
  fi
  if systemctl is-failed --quiet cloud-final.service; then
    echo 'Relay EC2 cloud-init failed before database bootstrap readiness' >&2
    cloud-init status --long >&2 || true
    journalctl -u cloud-final.service -n 80 --no-pager >&2 || true
    exit 1
  fi
  sleep 5
done
if [[ "$ready" != true ]]; then
  echo 'Timed out waiting for Relay EC2 bootstrap readiness' >&2
  cloud-init status --long >&2 || true
  systemctl status docker --no-pager >&2 || true
  exit 1
fi
work="$(mktemp -d /run/relay-db-bootstrap.XXXXXX)"
bootstrap_postgres_started=false
cleanup() {{
  if [[ "$bootstrap_postgres_started" == true ]]; then
    docker rm --force relay-postgres-bootstrap >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}}
trap cleanup EXIT
aws secretsmanager get-secret-value --region {shlex.quote(region)} --secret-id {shlex.quote(master_arn)} --query SecretString --output text > "$work/master.json"
aws secretsmanager get-secret-value --region {shlex.quote(region)} --secret-id {shlex.quote(runtime_arn)} --query SecretString --output text > "$work/runtime.json"
aws secretsmanager get-secret-value --region {shlex.quote(region)} --secret-id {shlex.quote(keycloak_arn)} --query SecretString --output text > "$work/keycloak.json"
{postgres_setup}
printf '%s' {shlex.quote(parser_b64)} | base64 -d > "$work/render.py"
python3 "$work/render.py" "$work/master.json" "$work/runtime.json" "$work/keycloak.json" {shlex.quote(endpoint)} "$work/db.env"
printf '%s' {shlex.quote(sql_b64)} | base64 -d > "$work/bootstrap.sql"
{database_command}
"""
    encoded = base64.b64encode(remote.encode()).decode("ascii")
    parameters = {
        "commands": [
            "set -euo pipefail",
            f"printf '%s' {shlex.quote(encoded)} | base64 -d > /tmp/relay-db-bootstrap.sh",
            "chmod 0700 /tmp/relay-db-bootstrap.sh",
            "/usr/bin/flock -w 900 /var/lock/relay-db-bootstrap.lock /tmp/relay-db-bootstrap.sh",
            "rm -f /tmp/relay-db-bootstrap.sh",
        ],
        "executionTimeout": ["1800"],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8") as stream:
        json.dump(parameters, stream)
        stream.flush()
        response = aws_json(
            "ssm", "send-command", "--document-name", "AWS-RunShellScript",
            "--comment", "Relay idempotent database bootstrap", "--instance-ids", *instances,
            "--parameters", f"file://{stream.name}", "--timeout-seconds", "1800",
            "--region", region,
        )
    command_id = response["Command"]["CommandId"]
    deadline = time.monotonic() + 1900
    while time.monotonic() < deadline:
        invocations = aws_json(
            "ssm", "list-command-invocations", "--command-id", command_id,
            "--details", "--region", region,
        ).get("CommandInvocations", [])
        if len(invocations) == len(instances) and all(
            item.get("Status") in TERMINAL for item in invocations
        ):
            failed = [item for item in invocations if item.get("Status") != "Success"]
            if failed:
                details = []
                for item in failed:
                    output = "\n".join(
                        str(plugin.get("Output") or "").strip()
                        for plugin in item.get("CommandPlugins", [])
                        if str(plugin.get("Output") or "").strip()
                    )
                    output = output[-8_000:]
                    summary = (
                        f"{item.get('InstanceId', 'unknown instance')}="
                        f"{item.get('Status', 'unknown status')}"
                    )
                    details.append(summary + (f"\n{output}" if output else ""))
                raise RuntimeError(
                    "database bootstrap failed through SSM:\n" + "\n".join(details)
                )
            return
        time.sleep(10)
    raise TimeoutError("timed out waiting for database bootstrap through SSM")


def bootstrap_eks(outputs: dict, region: str) -> None:
    values = database_values(outputs)
    cluster = str(output_value(outputs, "eks_cluster_name"))
    run("aws", "eks", "update-kubeconfig", "--name", cluster, "--alias", f"relay-{cluster}", "--region", region)
    suffix = secrets.token_hex(4)
    namespace = f"relay-db-bootstrap-{suffix}"
    secret = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "database", "namespace": namespace},
        "stringData": {
            "PGHOST": values["host"], "PGPORT": "5432", "PGDATABASE": "postgres",
            "PGUSER": values["master_username"], "PGPASSWORD": values["master_password"],
            "RELAY_PASSWORD": values["relay_password"],
            "KEYCLOAK_PASSWORD": values["keycloak_password"],
        },
    }
    config = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "sql", "namespace": namespace},
        "data": {"bootstrap.sql": SQL},
    }
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "database", "namespace": namespace},
        "spec": {
            "backoffLimit": 3, "ttlSecondsAfterFinished": 300,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "containers": [{
                        "name": "psql", "image": POSTGRES_BOOTSTRAP_IMAGE,
                        "command": ["sh", "-ec"],
                        "args": ["psql --no-password --file=/sql/bootstrap.sql"],
                        "envFrom": [{"secretRef": {"name": "database"}}],
                        "volumeMounts": [{"name": "sql", "mountPath": "/sql", "readOnly": True}],
                        "securityContext": {
                            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "runAsNonRoot": True, "runAsUser": 999, "runAsGroup": 999,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }],
                    "volumes": [{"name": "sql", "configMap": {"name": "sql"}}],
                    "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                }
            },
        },
    }
    try:
        run("kubectl", "create", "namespace", namespace)
        for manifest in (secret, config, job):
            run("kubectl", "apply", "-f", "-", input_text=json.dumps(manifest))
        run("kubectl", "-n", namespace, "wait", "--for=condition=complete", "job/database", "--timeout=15m")
    finally:
        subprocess.run(
            ["kubectl", "delete", "namespace", namespace, "--wait=false", "--ignore-not-found"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--topology", choices=("ec2", "eks"), required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    outputs = json.loads(args.terraform_output.read_text(encoding="utf-8"))
    if args.topology == "ec2":
        bootstrap_ec2(outputs, args.region)
    else:
        bootstrap_eks(outputs, args.region)
    print("Database roles and databases are reconciled with app-only credentials.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, RuntimeError, TimeoutError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"error: database bootstrap failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
