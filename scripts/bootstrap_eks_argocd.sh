#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: $0 CUSTOMER_PATH TF_OUTPUT_JSON GITHUB_REPOSITORY GIT_REVISION" >&2
  exit 2
fi

customer_path="$1"
tf_output_json="$2"
github_repository="$3"
git_revision="$4"

case "$customer_path" in relay/*/*) ;; *) echo "invalid customer path" >&2; exit 2 ;; esac
if [[ ! "$github_repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "invalid GitHub repository" >&2
  exit 2
fi

tf_value() {
  python3 - "$tf_output_json" "$1" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(value[sys.argv[2]]["value"])
PY
}

cluster_name="$(tf_value eks_cluster_name)"
# Terraform owns EKS Pod Identity associations for External Secrets and Relay.
# IRSA annotations must not be set on these service accounts.
tf_value external_secrets_pod_role_arn >/dev/null
tf_value relay_pod_role_arn >/dev/null

aws eks update-kubeconfig --name "$cluster_name" --alias "relay-${cluster_name}"
kubectl auth can-i create customresourcedefinitions >/dev/null

helm repo add external-secrets https://charts.external-secrets.io
helm repo update
helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace --version 2.8.0 \
  --set installCRDs=true \
  --wait --timeout 10m

region="${AWS_REGION:?AWS_REGION is required}"
kubectl apply -f - <<YAML
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: aws-secrets-manager
spec:
  provider:
    aws:
      service: SecretsManager
      region: ${region}
YAML

customer_slug="${customer_path##*/}"
helm upgrade --install "relay-${customer_slug}" gitops/charts/relay \
  --namespace "relay-${customer_slug}" --create-namespace \
  --values "${customer_path}/values.yaml" --wait --timeout 15m
