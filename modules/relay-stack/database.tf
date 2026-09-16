resource "aws_db_subnet_group" "relay" {
  count = local.is_home_lab ? 0 : 1

  name       = local.name
  subnet_ids = module.vpc.database_subnets
  tags       = merge(local.common_tags, { Name = local.name })
}

resource "aws_db_parameter_group" "relay" {
  count = local.is_managed_ec2 ? 1 : 0

  name_prefix = "${local.name}-"
  family      = "postgres16"
  description = "Relay PostgreSQL parameters; require encrypted client sessions"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "immediate"
  }

  parameter {
    name         = "log_statement"
    value        = "ddl"
    apply_method = "immediate"
  }

  parameter {
    name         = "log_min_duration_statement"
    value        = "1000"
    apply_method = "immediate"
  }

  tags = local.common_tags
  lifecycle { create_before_destroy = true }
}

resource "aws_rds_cluster_parameter_group" "relay" {
  count = local.compute_mode == "eks" ? 1 : 0

  name_prefix = "${local.name}-"
  family      = "aurora-postgresql16"
  description = "Relay Aurora PostgreSQL parameters; require encrypted client sessions"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "immediate"
  }

  parameter {
    name         = "log_statement"
    value        = "ddl"
    apply_method = "immediate"
  }

  parameter {
    name         = "log_min_duration_statement"
    value        = "1000"
    apply_method = "immediate"
  }

  tags = local.common_tags
  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "database" {
  count = local.is_home_lab ? 0 : 1
  #checkov:skip=CKV2_AWS_5:The conditional group is attached to the matching conditional RDS instance or Aurora cluster through vpc_security_group_ids.

  name_prefix = "${local.name}-db-"
  description = "Relay PostgreSQL access from application compute only"
  vpc_id      = module.vpc.vpc_id
  egress      = []
  tags        = merge(local.common_tags, { Name = "${local.name}-database" })
  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_ingress_rule" "database_from_ec2" {
  count                        = local.is_managed_ec2 ? 1 : 0
  security_group_id            = aws_security_group.database[0].id
  referenced_security_group_id = aws_security_group.application.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "database_from_eks" {
  count                        = local.compute_mode == "eks" ? 1 : 0
  security_group_id            = aws_security_group.database[0].id
  referenced_security_group_id = module.eks[0].node_security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_db_instance" "relay" {
  count = local.is_managed_ec2 ? 1 : 0

  identifier     = local.name
  engine         = "postgres"
  engine_version = "16"
  instance_class = local.profile.db_instance_class

  db_name              = "relay"
  username             = "relay_admin"
  port                 = 5432
  parameter_group_name = aws_db_parameter_group.relay[0].name

  manage_master_user_password         = true
  master_user_secret_kms_key_id       = aws_kms_key.relay.key_id
  iam_database_authentication_enabled = true

  allocated_storage     = local.profile.db_storage_gib
  max_allocated_storage = local.profile.db_max_storage_gib
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.relay.arn

  multi_az               = local.profile.db_multi_az
  db_subnet_group_name   = aws_db_subnet_group.relay[0].name
  vpc_security_group_ids = [aws_security_group.database[0].id]
  publicly_accessible    = false

  backup_retention_period   = var.environment == "production" ? 35 : 7
  copy_tags_to_snapshot     = true
  deletion_protection       = var.deletion_protection && !var.offboarding
  skip_final_snapshot       = var.offboarding || !var.deletion_protection
  final_snapshot_identifier = var.deletion_protection && !var.offboarding ? "${local.name}-final" : null

  auto_minor_version_upgrade      = true
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.relay.arn
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  apply_immediately               = false

  tags = local.common_tags
}

resource "aws_rds_cluster" "relay" {
  count = local.compute_mode == "eks" ? 1 : 0

  cluster_identifier              = local.name
  engine                          = "aurora-postgresql"
  engine_mode                     = "provisioned"
  engine_version                  = "16.4"
  database_name                   = "relay"
  master_username                 = "relay_admin"
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.relay[0].name

  manage_master_user_password         = true
  master_user_secret_kms_key_id       = aws_kms_key.relay.key_id
  iam_database_authentication_enabled = true
  storage_encrypted                   = true
  kms_key_id                          = aws_kms_key.relay.arn

  db_subnet_group_name   = aws_db_subnet_group.relay[0].name
  vpc_security_group_ids = [aws_security_group.database[0].id]

  backup_retention_period         = 35
  preferred_backup_window         = "02:00-03:00"
  preferred_maintenance_window    = "sun:03:30-sun:04:30"
  enabled_cloudwatch_logs_exports = ["postgresql"]
  deletion_protection             = var.deletion_protection && !var.offboarding
  skip_final_snapshot             = var.offboarding || !var.deletion_protection
  final_snapshot_identifier       = var.deletion_protection && !var.offboarding ? "${local.name}-final" : null
  copy_tags_to_snapshot           = true

  tags = local.common_tags
}

resource "aws_rds_cluster_instance" "relay" {
  count = local.compute_mode == "eks" ? local.profile.aurora_instances : 0

  identifier         = "${local.name}-${count.index + 1}"
  cluster_identifier = aws_rds_cluster.relay[0].id
  instance_class     = local.profile.db_instance_class
  engine             = aws_rds_cluster.relay[0].engine
  engine_version     = aws_rds_cluster.relay[0].engine_version

  publicly_accessible             = false
  auto_minor_version_upgrade      = true
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.relay.arn
  tags                            = local.common_tags
}

resource "aws_secretsmanager_secret" "runtime" {
  # A generated name preserves the 30-day recovery window while allowing a
  # deliberately re-created deployment to receive fresh secret identities.
  name_prefix             = "${local.name}/runtime-"
  description             = "Provisioning target for Relay runtime secrets; values are written out-of-band after infrastructure creation."
  kms_key_id              = aws_kms_key.relay.arn
  recovery_window_in_days = 30
  tags                    = local.common_tags

  lifecycle { create_before_destroy = true }
}

resource "aws_secretsmanager_secret" "database_master" {
  count = local.is_home_lab ? 1 : 0

  name_prefix             = "${local.name}/database-master-"
  description             = "Home Lab PostgreSQL master credential; its value is seeded out-of-band and never enters Terraform state."
  kms_key_id              = aws_kms_key.relay.arn
  recovery_window_in_days = 30
  tags                    = local.common_tags

  lifecycle { create_before_destroy = true }
}

resource "aws_secretsmanager_secret" "keycloak" {
  name_prefix             = "${local.name}/keycloak-bootstrap-"
  description             = "Provisioning target for Keycloak bootstrap and client credentials; Terraform deliberately stores no secret version."
  kms_key_id              = aws_kms_key.relay.arn
  recovery_window_in_days = 30
  tags                    = local.common_tags

  lifecycle { create_before_destroy = true }
}

