data "aws_iam_policy_document" "eks_pod_identity_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "relay_pod" {
  count = local.compute_mode == "eks" ? 1 : 0

  name                 = "${local.name}-relay-pod"
  assume_role_policy   = data.aws_iam_policy_document.eks_pod_identity_assume.json
  permissions_boundary = var.workload_permissions_boundary_arn
  tags                 = local.common_tags
}

data "aws_iam_policy_document" "relay_pod" {
  statement {
    sid       = "ListMediaBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.relay["media"].arn]
  }

  statement {
    sid = "ManageMediaObjects"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.relay["media"].arn}/*"]
  }

  statement {
    sid       = "UseCustomerKmsKey"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.relay.arn]
  }
}

resource "aws_iam_role_policy" "relay_pod" {
  count  = local.compute_mode == "eks" ? 1 : 0
  name   = "relay-media"
  role   = aws_iam_role.relay_pod[0].id
  policy = data.aws_iam_policy_document.relay_pod.json
}

resource "aws_eks_pod_identity_association" "relay" {
  count = local.compute_mode == "eks" ? 1 : 0

  cluster_name    = module.eks[0].cluster_name
  namespace       = var.kubernetes_namespace
  service_account = "relay-${local.customer_slug}"
  role_arn        = aws_iam_role.relay_pod[0].arn
}

resource "aws_iam_role" "external_secrets_pod" {
  count = local.compute_mode == "eks" ? 1 : 0

  name                 = "${local.name}-external-secrets"
  assume_role_policy   = data.aws_iam_policy_document.eks_pod_identity_assume.json
  permissions_boundary = var.workload_permissions_boundary_arn
  tags                 = local.common_tags
}

data "aws_iam_policy_document" "external_secrets_pod" {
  statement {
    sid     = "ReadCustomerRuntimeSecrets"
    actions = ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.runtime.arn,
      aws_secretsmanager_secret.keycloak.arn,
    ]
  }

  statement {
    sid       = "DecryptCustomerSecrets"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.relay.arn]
  }
}

resource "aws_iam_role_policy" "external_secrets_pod" {
  count  = local.compute_mode == "eks" ? 1 : 0
  name   = "read-customer-runtime-secrets"
  role   = aws_iam_role.external_secrets_pod[0].id
  policy = data.aws_iam_policy_document.external_secrets_pod.json
}

resource "aws_eks_pod_identity_association" "external_secrets" {
  count = local.compute_mode == "eks" ? 1 : 0

  cluster_name    = module.eks[0].cluster_name
  namespace       = "external-secrets"
  service_account = "external-secrets"
  role_arn        = aws_iam_role.external_secrets_pod[0].arn
}
