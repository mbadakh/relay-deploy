locals {
  codebuild_project_name = "${local.name}-eks-bootstrap"
  codebuild_project_arn  = "arn:${data.aws_partition.current.partition}:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/${local.codebuild_project_name}"
}

resource "aws_security_group" "codebuild" {
  count = local.compute_mode == "eks" ? 1 : 0

  name_prefix = "${local.name}-codebuild-"
  description = "Ephemeral EKS bootstrap builds; no inbound access"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.common_tags, { Name = "${local.name}-codebuild" })

  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_egress_rule" "codebuild_https" {
  count = local.compute_mode == "eks" ? 1 : 0

  security_group_id = aws_security_group.codebuild[0].id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "Private EKS endpoint and TLS-protected AWS/public APIs through NAT"
}

resource "aws_vpc_security_group_egress_rule" "codebuild_dns_udp" {
  count = local.compute_mode == "eks" ? 1 : 0

  security_group_id = aws_security_group.codebuild[0].id
  cidr_ipv4         = "${cidrhost(var.vpc_cidr, 2)}/32"
  from_port         = 53
  to_port           = 53
  ip_protocol       = "udp"
  description       = "VPC resolver DNS"
}

resource "aws_vpc_security_group_egress_rule" "codebuild_dns_tcp" {
  count = local.compute_mode == "eks" ? 1 : 0

  security_group_id = aws_security_group.codebuild[0].id
  cidr_ipv4         = "${cidrhost(var.vpc_cidr, 2)}/32"
  from_port         = 53
  to_port           = 53
  ip_protocol       = "tcp"
  description       = "VPC resolver DNS fallback"
}

resource "aws_vpc_security_group_egress_rule" "codebuild_database" {
  count = local.compute_mode == "eks" ? 1 : 0

  security_group_id            = aws_security_group.codebuild[0].id
  referenced_security_group_id = aws_security_group.database[0].id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "Customer database bootstrap"
}

resource "aws_vpc_security_group_ingress_rule" "database_from_codebuild" {
  count = local.compute_mode == "eks" ? 1 : 0

  security_group_id            = aws_security_group.database[0].id
  referenced_security_group_id = aws_security_group.codebuild[0].id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "Ephemeral customer database bootstrap"
}

data "aws_iam_policy_document" "codebuild_assume" {
  count = local.compute_mode == "eks" ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.codebuild_project_arn]
    }
  }
}

resource "aws_iam_role" "codebuild" {
  count = local.compute_mode == "eks" ? 1 : 0

  name                 = "${local.name}-codebuild"
  assume_role_policy   = data.aws_iam_policy_document.codebuild_assume[0].json
  permissions_boundary = var.workload_permissions_boundary_arn
  tags                 = local.common_tags
}

data "aws_iam_policy_document" "codebuild" {
  count = local.compute_mode == "eks" ? 1 : 0

  statement {
    sid       = "WriteBootstrapLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.codebuild[0].arn}:*"]
  }

  statement {
    sid     = "ReadVersionedDeploymentInputs"
    actions = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [
      "${aws_s3_bucket.relay["config"].arn}/deploy-context/*",
      "${aws_s3_bucket.relay["config"].arn}/deploy-bundles/*",
    ]
  }

  statement {
    sid       = "ReadDeploymentBucketMetadata"
    actions   = ["s3:GetBucketLocation"]
    resources = [aws_s3_bucket.relay["config"].arn]
  }

  statement {
    sid       = "DecryptDeploymentInputsAndSecrets"
    actions   = ["kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.relay.arn]
  }

  statement {
    sid     = "ReadBootstrapSecrets"
    actions = ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.runtime.arn,
      aws_secretsmanager_secret.keycloak.arn,
      local.database_master_secret_arn,
    ]
  }

  statement {
    sid       = "DescribeCustomerCluster"
    actions   = ["eks:DescribeCluster"]
    resources = [module.eks[0].cluster_arn]
  }

  statement {
    sid       = "RunApprovedDocumentOnCustomerInstances"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/Customer"
      values   = [local.customer_slug]
    }
  }

  statement {
    sid       = "UseApprovedSsmDocument"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}::document/AWS-RunShellScript"]
  }

  statement {
    sid = "ObserveCustomerInstancesAndCommands"
    actions = [
      "autoscaling:DescribeAutoScalingGroups",
      "ssm:DescribeInstanceInformation",
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
      "ssm:ListCommands",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }

  statement {
    sid = "ManageVpcBuildInterfaces"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DeleteNetworkInterface",
      "ec2:DescribeDhcpOptions",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSubnets",
      "ec2:DescribeVpcs",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "AuthorizeVpcBuildInterfaces"
    actions   = ["ec2:CreateNetworkInterfacePermission"]
    resources = ["arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:network-interface/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:AuthorizedService"
      values   = ["codebuild.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "ec2:Subnet"
      values   = module.vpc.private_subnet_arns
    }
  }
}

resource "aws_iam_role_policy" "codebuild" {
  count = local.compute_mode == "eks" ? 1 : 0

  name   = "private-eks-bootstrap"
  role   = aws_iam_role.codebuild[0].id
  policy = data.aws_iam_policy_document.codebuild[0].json
}

resource "aws_codebuild_project" "eks_bootstrap" {
  count = local.compute_mode == "eks" ? 1 : 0

  name                   = local.codebuild_project_name
  description            = "Ephemeral no-source bootstrap for the private customer EKS cluster"
  service_role           = aws_iam_role.codebuild[0].arn
  build_timeout          = 90
  queued_timeout         = 30
  concurrent_build_limit = 1
  encryption_key         = aws_kms_key.relay.arn

  source {
    type      = "NO_SOURCE"
    buildspec = file("${path.module}/codebuild-buildspec.yml")
  }

  artifacts { type = "NO_ARTIFACTS" }
  cache { type = "NO_CACHE" }

  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/standard:7.0"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = false

    environment_variable {
      name  = "DEPLOY_BUCKET"
      value = aws_s3_bucket.relay["config"].id
    }
    environment_variable {
      name  = "CUSTOMER_PATH"
      value = "relay/${var.aws_region}/${local.customer_slug}"
    }
    environment_variable {
      name  = "GITHUB_REPOSITORY"
      value = "local/terminal"
    }
    environment_variable {
      name  = "CUSTOMER_KMS_KEY_ARN"
      value = aws_kms_key.relay.arn
    }
  }

  vpc_config {
    vpc_id             = module.vpc.vpc_id
    subnets            = module.vpc.private_subnets
    security_group_ids = [aws_security_group.codebuild[0].id]
  }

  logs_config {
    cloudwatch_logs {
      group_name  = aws_cloudwatch_log_group.codebuild[0].name
      stream_name = "eks-bootstrap"
      status      = "ENABLED"
    }
    s3_logs { status = "DISABLED" }
  }

  tags = local.common_tags

  depends_on = [aws_iam_role_policy.codebuild]
}
