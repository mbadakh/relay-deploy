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
region="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
bundle_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$bundle_root"

case "$customer_path" in relay/*/*) ;; *) echo "invalid customer path" >&2; exit 2 ;; esac
[[ "$github_repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || {
  echo "invalid GitHub repository" >&2
  exit 2
}
[[ "$git_revision" =~ ^[0-9a-f]{40}$ ]] || {
  echo "git revision must be a full commit SHA" >&2
  exit 2
}
[[ "$region" =~ ^[a-z]{2}(-gov)?-[a-z]+-[0-9]$ ]] || {
  echo "AWS region is missing or invalid" >&2
  exit 2
}

for command in aws curl python3 sha256sum tar; do
  command -v "$command" >/dev/null || {
    echo "required command is unavailable: $command" >&2
    exit 2
  }
done

tool_directory="$(mktemp -d)"
trap 'rm -rf "$tool_directory"' EXIT

kubectl_version="v1.33.4"
kubectl_sha256="c2ba72c115d524b72aaee9aab8df8b876e1596889d2f3f27d68405262ce86ca1"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  "https://dl.k8s.io/release/${kubectl_version}/bin/linux/amd64/kubectl" \
  --output "$tool_directory/kubectl"
echo "$kubectl_sha256  $tool_directory/kubectl" | sha256sum --check --status
chmod 0755 "$tool_directory/kubectl"

export PATH="$tool_directory:$PATH"
export AWS_PAGER=""
kubectl version --client=true
aws --version

if [[ -f scripts/release-promotion ]]; then
  AWS_REGION="$region" bash scripts/sync_eks_release.sh \
    "$customer_path" "$tf_output_json" "$github_repository" "$git_revision"
  exit 0
fi

helm_version="v3.17.3"
helm_sha256="ee88b3c851ae6466a3de507f7be73fe94d54cbf2987cbaa3d1a3832ea331f2cd"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  "https://get.helm.sh/helm-${helm_version}-linux-amd64.tar.gz" \
  --output "$tool_directory/helm.tar.gz"
echo "$helm_sha256  $tool_directory/helm.tar.gz" | sha256sum --check --status
tar -xzf "$tool_directory/helm.tar.gz" -C "$tool_directory"
install -m 0755 "$tool_directory/linux-amd64/helm" "$tool_directory/helm"
helm version --short

python3 scripts/bootstrap_database.py \
  --topology eks \
  --terraform-output "$tf_output_json" \
  --region "$region"
python3 scripts/reconcile_turn_ssm.py \
  --terraform-output "$tf_output_json" \
  --region "$region"
AWS_REGION="$region" bash scripts/bootstrap_eks_argocd.sh \
  "$customer_path" "$tf_output_json" "$github_repository" "$git_revision"
