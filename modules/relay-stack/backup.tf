data "aws_iam_policy_document" "backup_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["backup.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backup" {
  name                 = "${local.name}-backup"
  assume_role_policy   = data.aws_iam_policy_document.backup_assume.json
  permissions_boundary = var.workload_permissions_boundary_arn
  tags                 = local.common_tags
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_iam_role_policy_attachment" "backup_s3" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AWSBackupServiceRolePolicyForS3Backup"
}

resource "aws_backup_vault" "relay" {
  name          = local.name
  kms_key_arn   = aws_kms_key.relay.arn
  force_destroy = var.offboarding
  tags          = local.common_tags
}

resource "aws_backup_plan" "relay" {
  name = local.name

  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.relay.name
    schedule          = "cron(0 3 * * ? *)"
    start_window      = 60
    completion_window = 360

    lifecycle {
      delete_after = local.is_home_lab ? 14 : (var.environment == "production" ? 35 : 7)
    }

    recovery_point_tags = local.common_tags
  }

  tags = local.common_tags
}

resource "aws_backup_selection" "relay" {
  name         = local.name
  iam_role_arn = aws_iam_role.backup.arn
  plan_id      = aws_backup_plan.relay.id
  # The Home Lab node is disposable and fully reconstructed by cloud-init;
  # back up its separately attached persistent data volume, not its root disk.
  resources = local.is_home_lab ? [aws_ebs_volume.homelab_data[0].arn] : concat(
    [local.compute_mode == "eks" ? aws_rds_cluster.relay[0].arn : aws_db_instance.relay[0].arn],
    [aws_s3_bucket.relay["media"].arn, aws_s3_bucket.relay["config"].arn],
  )
}
