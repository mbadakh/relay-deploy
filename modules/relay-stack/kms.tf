resource "aws_kms_key" "relay" {
  description             = "Customer-isolated Relay encryption key (${var.customer_name})"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  multi_region            = false
  # KMS key policies require Resource="*" because the policy is attached to
  # this key. Account-root delegation lets IAM enforce the narrowly scoped
  # workload policies; the service statement is constrained to only these two
  # log-group encryption contexts.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "EnableAccountDelegation"
        Effect   = "Allow"
        Action   = "kms:*"
        Resource = "*"
        Principal = {
          AWS = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"
        }
      },
      {
        Sid    = "AllowCloudWatchLogsEncryption"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:ReEncrypt*",
        ]
        Resource = "*"
        Principal = {
          Service = "logs.${var.aws_region}.${data.aws_partition.current.dns_suffix}"
        }
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = [
              "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/relay/${local.customer_slug}/application*",
              "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/relay/${local.customer_slug}/eks-bootstrap*",
              "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:aws-waf-logs-${local.name}*",
            ]
          }
        }
      },
      ], flatten([
        for role in aws_iam_service_linked_role.relay_autoscaling : [
          {
            Sid    = "AllowAutoScalingEncryptedEbs"
            Effect = "Allow"
            Action = [
              "kms:Decrypt",
              "kms:DescribeKey",
              "kms:Encrypt",
              "kms:GenerateDataKey*",
              "kms:ReEncrypt*",
            ]
            Resource = "*"
            Principal = {
              AWS = role.arn
            }
          },
          {
            Sid      = "AllowAutoScalingPersistentResourceGrant"
            Effect   = "Allow"
            Action   = "kms:CreateGrant"
            Resource = "*"
            Principal = {
              AWS = role.arn
            }
            Condition = {
              Bool = {
                "kms:GrantIsForAWSResource" = "true"
              }
            }
          },
        ]
    ]))
  })

  tags = merge(local.common_tags, { Name = "${local.name}-data" })
}

resource "aws_kms_alias" "relay" {
  name          = "alias/${local.name}-data"
  target_key_id = aws_kms_key.relay.key_id
}

# S3 versioning and SHA256 protect against accidental drift, while this
# asymmetric signature also prevents a principal with only S3 write access
# from authorizing executable EC2 desired state. The private key never leaves
# KMS. Only the protected deployment role can sign; instances can only verify.
resource "aws_kms_key" "ec2_config_signing" {
  count = local.compute_mode == "ec2" ? 1 : 0

  description              = "Sign Relay EC2 desired-state bundles (${var.customer_name})"
  customer_master_key_spec = "ECC_NIST_P256"
  key_usage                = "SIGN_VERIFY"
  deletion_window_in_days  = 30
  multi_region             = false
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "EnableAccountDelegation"
      Effect   = "Allow"
      Action   = "kms:*"
      Resource = "*"
      Principal = {
        AWS = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"
      }
    }]
  })

  tags = merge(local.common_tags, { Name = "${local.name}-ec2-config-signing" })
}

resource "aws_kms_alias" "ec2_config_signing" {
  count = local.compute_mode == "ec2" ? 1 : 0

  name          = "alias/${local.name}-ec2-config-signing"
  target_key_id = aws_kms_key.ec2_config_signing[0].key_id
}
