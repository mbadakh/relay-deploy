data "aws_iam_policy_document" "compute_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "compute" {
  count                = local.compute_mode == "ec2" ? 1 : 0
  name                 = "${local.name}-compute"
  assume_role_policy   = data.aws_iam_policy_document.compute_assume.json
  permissions_boundary = var.workload_permissions_boundary_arn
  tags                 = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  count      = local.compute_mode == "ec2" ? 1 : 0
  role       = aws_iam_role.compute[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "compute" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = values(aws_ecr_repository.relay)[*].arn
  }
  statement {
    sid       = "ListRuntimeBuckets"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.relay["media"].arn, aws_s3_bucket.relay["config"].arn]
  }
  statement {
    sid       = "UseMediaObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.relay["media"].arn}/*"]
  }
  statement {
    sid       = "ReadEc2DesiredState"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.relay["config"].arn}/ec2/*"]
  }
  statement {
    actions = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [
      aws_secretsmanager_secret.runtime.arn,
      aws_secretsmanager_secret.keycloak.arn,
      local.database_master_secret_arn,
    ]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.relay.arn]
  }
  dynamic "statement" {
    for_each = local.compute_mode == "ec2" ? [aws_kms_key.ec2_config_signing[0].arn] : []
    content {
      sid       = "VerifyEc2DesiredState"
      actions   = ["kms:DescribeKey", "kms:Verify"]
      resources = [statement.value]
    }
  }
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.application.arn}:*"]
  }
}

resource "aws_iam_role_policy" "compute" {
  count  = local.compute_mode == "ec2" ? 1 : 0
  name   = "relay-runtime"
  role   = aws_iam_role.compute[0].id
  policy = data.aws_iam_policy_document.compute.json
}

resource "aws_iam_instance_profile" "compute" {
  count = local.compute_mode == "ec2" ? 1 : 0
  name  = "${local.name}-compute"
  role  = aws_iam_role.compute[0].name
}
