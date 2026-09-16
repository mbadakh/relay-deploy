locals {
  bucket_names = {
    media  = "${local.bucket_stem}-media-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
    config = "${local.bucket_stem}-config-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
    logs   = "${local.bucket_stem}-logs-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  }
}

resource "aws_s3_bucket" "relay" {
  for_each = local.bucket_names

  bucket        = each.value
  force_destroy = var.offboarding || (var.environment != "production" && var.force_destroy_nonproduction_buckets)
  tags          = merge(local.common_tags, { Name = each.value, Purpose = each.key })
}

resource "aws_s3_bucket_ownership_controls" "relay" {
  for_each = aws_s3_bucket.relay
  bucket   = each.value.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_public_access_block" "relay" {
  for_each = aws_s3_bucket.relay
  bucket   = each.value.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "relay" {
  for_each = aws_s3_bucket.relay
  bucket   = each.value.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "relay" {
  for_each = aws_s3_bucket.relay
  bucket   = each.value.id
  rule {
    # ALB access logging requires SSE-S3. Customer media/config use the
    # customer-managed key and S3 bucket keys to reduce KMS request volume.
    bucket_key_enabled = each.key != "logs"
    apply_server_side_encryption_by_default {
      kms_master_key_id = each.key == "logs" ? null : aws_kms_key.relay.arn
      sse_algorithm     = each.key == "logs" ? "AES256" : "aws:kms"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "relay" {
  for_each = aws_s3_bucket.relay
  bucket   = each.value.id

  rule {
    id     = "noncurrent-retention"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 90 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }

  dynamic "rule" {
    for_each = each.key == "logs" ? [1] : []
    content {
      id     = "log-retention"
      status = "Enabled"
      filter {}
      expiration { days = local.profile.log_retention_days }
    }
  }

  depends_on = [aws_s3_bucket_versioning.relay]
}

resource "aws_s3_bucket_logging" "relay" {
  for_each = { for purpose, bucket in aws_s3_bucket.relay : purpose => bucket if purpose != "logs" }

  bucket        = each.value.id
  target_bucket = aws_s3_bucket.relay["logs"].id
  target_prefix = "s3-access/${each.key}/"

  depends_on = [aws_s3_bucket_policy.logs]
}

data "aws_iam_policy_document" "logs_bucket" {
  statement {
    sid = "AllowVPCFlowLogAclCheck"
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.relay["logs"].arn]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid = "AllowALBLogDelivery"
    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.relay["logs"].arn}/alb/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      # AWS validates the destination while enabling access logs. Bind delivery
      # to this account and Region using AWS's documented load-balancer ARN
      # shape; an exact ALB-name path is not consistently present during that
      # validation request.
      values = [
        "arn:${data.aws_partition.current.partition}:elasticloadbalancing:${var.aws_region}:${data.aws_caller_identity.current.account_id}:loadbalancer/*",
      ]
    }
  }

  statement {
    sid = "AllowS3ServerAccessLogDelivery"
    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.relay["logs"].arn}/s3-access/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values = [
        aws_s3_bucket.relay["media"].arn,
        aws_s3_bucket.relay["config"].arn,
      ]
    }
  }

  statement {
    sid = "AllowVPCFlowLogDelivery"
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.relay["logs"].arn}/vpc/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.relay["logs"].arn, "${aws_s3_bucket.relay["logs"].arn}/*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.relay["logs"].id
  policy = data.aws_iam_policy_document.logs_bucket.json
}

data "aws_iam_policy_document" "private_bucket" {
  for_each = { for purpose, bucket in aws_s3_bucket.relay : purpose => bucket if purpose != "logs" }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["s3:*"]
    resources = [each.value.arn, "${each.value.arn}/*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "private" {
  for_each = data.aws_iam_policy_document.private_bucket
  bucket   = aws_s3_bucket.relay[each.key].id
  policy   = each.value.json
}

# Bootstrap code is itself content addressed and pinned to the exact S3
# VersionId from EC2 user data. This keeps launch user data below its size limit
# while preserving a Terraform-rooted chain of trust for the reconciler.
resource "aws_s3_object" "ec2_reconciler" {
  count = local.compute_mode == "ec2" ? 1 : 0

  bucket                 = aws_s3_bucket.relay["config"].id
  key                    = "ec2/reconciler/relay-ec2-reconcile-${filesha256("${path.module}/files/relay-ec2-reconcile.py")}.py"
  content                = file("${path.module}/files/relay-ec2-reconcile.py")
  content_type           = "text/x-python"
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.relay.arn
  source_hash            = filesha256("${path.module}/files/relay-ec2-reconcile.py")
  metadata = {
    sha256           = filesha256("${path.module}/files/relay-ec2-reconcile.py")
    "schema-version" = "1"
  }

  depends_on = [
    aws_s3_bucket_versioning.relay,
    aws_s3_bucket_server_side_encryption_configuration.relay,
    aws_s3_bucket_policy.private,
  ]

  # The key is content-addressed. Publish the new immutable object before
  # removing the superseded key so active instances can always bootstrap.
  lifecycle {
    create_before_destroy = true
  }
}
