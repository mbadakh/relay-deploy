locals {
  customer_slug = trim(replace(lower(trimspace(var.customer_name)), "/[^a-z0-9-]+/", "-"), "-")
  name          = "${substr("relay-${local.customer_slug}-${var.environment}", 0, 30)}-${substr(sha256("${local.customer_slug}:${var.domain_name}:${var.aws_region}"), 0, 8)}"
  edge_name     = substr(local.name, 0, 24)
  bucket_stem   = "relay-${substr(local.customer_slug, 0, 12)}-${substr(sha256("${local.customer_slug}:${var.domain_name}"), 0, 8)}"
  managed_dns   = var.hosted_zone_id != ""
  managed_tls   = var.hosted_zone_id != ""
  public_origin = var.public_origin != "" ? var.public_origin : (local.managed_tls ? "https://${var.domain_name}" : "http://${local.is_home_lab ? aws_eip.homelab[0].public_ip : aws_lb.relay[0].dns_name}")
  app_fqdn      = var.domain_name
  auth_fqdn     = var.domain_name

  # These are conservative registered-user envelopes, not concurrent-user promises.
  # Application replicas intentionally stay at one; larger tiers scale the surrounding
  # managed services and worker capacity until Relay adopts a certified distributed state.
  profiles = {
    "10" = {
      compute_mode       = "ec2"
      az_count           = 2
      nat_per_az         = false
      ec2_instance_type  = "t4g.small"
      db_instance_class  = "db.t4g.micro"
      db_storage_gib     = 20
      db_max_storage_gib = 100
      db_multi_az        = true
      eks_instance_types = []
      eks_min            = 0
      eks_desired        = 0
      eks_max            = 0
      aurora_instances   = 0
      waf_rate_limit     = 600
      log_retention_days = 365
      turn_instances     = 1
      turn_instance_type = "t4g.small"
    }
    "100" = {
      compute_mode       = "ec2"
      az_count           = 2
      nat_per_az         = false
      ec2_instance_type  = "t4g.small"
      db_instance_class  = "db.t4g.small"
      db_storage_gib     = 30
      db_max_storage_gib = 150
      db_multi_az        = true
      eks_instance_types = []
      eks_min            = 0
      eks_desired        = 0
      eks_max            = 0
      aurora_instances   = 0
      waf_rate_limit     = 1200
      log_retention_days = 365
      turn_instances     = 1
      turn_instance_type = "t4g.small"
    }
    "1k" = {
      compute_mode       = "ec2"
      az_count           = 2
      nat_per_az         = true
      ec2_instance_type  = "t4g.medium"
      db_instance_class  = "db.t4g.medium"
      db_storage_gib     = 100
      db_max_storage_gib = 500
      db_multi_az        = true
      eks_instance_types = []
      eks_min            = 0
      eks_desired        = 0
      eks_max            = 0
      aurora_instances   = 0
      waf_rate_limit     = 3000
      log_retention_days = 365
      turn_instances     = 2
      turn_instance_type = "c7g.large"
    }
    "10k" = {
      compute_mode       = "eks"
      az_count           = 3
      nat_per_az         = true
      ec2_instance_type  = null
      db_instance_class  = "db.r7g.large"
      db_storage_gib     = 0
      db_max_storage_gib = 0
      db_multi_az        = true
      eks_instance_types = ["m7g.large"]
      eks_min            = 2
      eks_desired        = 3
      eks_max            = 6
      aurora_instances   = 2
      waf_rate_limit     = 6000
      log_retention_days = 365
      turn_instances     = 2
      turn_instance_type = "c7gn.large"
    }
    "100k" = {
      compute_mode       = "eks"
      az_count           = 3
      nat_per_az         = true
      ec2_instance_type  = null
      db_instance_class  = "db.r7g.xlarge"
      db_storage_gib     = 0
      db_max_storage_gib = 0
      db_multi_az        = true
      eks_instance_types = ["m7g.xlarge"]
      eks_min            = 3
      eks_desired        = 6
      eks_max            = 20
      aurora_instances   = 3
      waf_rate_limit     = 15000
      log_retention_days = 365
      turn_instances     = 2
      turn_instance_type = "c7gn.xlarge"
    }
    "1m" = {
      compute_mode       = "eks"
      az_count           = 3
      nat_per_az         = true
      ec2_instance_type  = null
      db_instance_class  = "db.r7g.2xlarge"
      db_storage_gib     = 0
      db_max_storage_gib = 0
      db_multi_az        = true
      eks_instance_types = ["m7g.2xlarge"]
      eks_min            = 6
      eks_desired        = 12
      eks_max            = 60
      aurora_instances   = 4
      waf_rate_limit     = 30000
      log_retention_days = 365
      turn_instances     = 4
      turn_instance_type = "c7gn.4xlarge"
    }
    "10m" = {
      compute_mode       = "eks"
      az_count           = 3
      nat_per_az         = true
      ec2_instance_type  = null
      db_instance_class  = "db.r7g.8xlarge"
      db_storage_gib     = 0
      db_max_storage_gib = 0
      db_multi_az        = true
      eks_instance_types = ["m7g.4xlarge"]
      eks_min            = 12
      eks_desired        = 30
      eks_max            = 200
      aurora_instances   = 6
      waf_rate_limit     = 60000
      log_retention_days = 365
      turn_instances     = 12
      turn_instance_type = "c7gn.8xlarge"
    }
  }

  is_home_lab          = var.home_lab
  is_managed_ec2       = !var.home_lab && local.profiles[var.capacity_profile].compute_mode == "ec2"
  managed_edge_enabled = !var.home_lab
  profile = var.home_lab ? merge(local.profiles[var.capacity_profile], {
    compute_mode       = "ec2"
    az_count           = 1
    nat_per_az         = false
    ec2_instance_type  = "t4g.small"
    db_instance_class  = null
    db_storage_gib     = 0
    db_max_storage_gib = 0
    db_multi_az        = false
    log_retention_days = 365
    turn_instances     = 0
    turn_instance_type = null
  }) : local.profiles[var.capacity_profile]
  compute_mode         = local.profile.compute_mode
  relay_target_port    = local.compute_mode == "eks" ? var.relay_node_port : var.relay_container_port
  keycloak_target_port = local.compute_mode == "eks" ? var.keycloak_node_port : var.keycloak_container_port
  azs                  = slice(data.aws_availability_zones.available.names, 0, local.profile.az_count)

  public_subnets   = [for index, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, index)]
  private_subnets  = [for index, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, index + 4)]
  database_subnets = [for index, _ in local.azs : cidrsubnet(var.vpc_cidr, 8, index + 128)]

  common_tags = merge(var.tags, {
    # These two tags are also enforced by the target-account apply role. Keep
    # them repository-owned so a customer leaf cannot weaken the IAM/KMS
    # boundary by overriding or omitting them.
    Application            = "Relay"
    ManagedBy              = "Terragrunt"
    RelayCapacityProfile   = var.capacity_profile
    RelayComputeMode       = local.compute_mode
    RelayDeploymentProfile = var.home_lab ? "home-lab" : "managed-production"
    DataClassification     = "Confidential"
  })

  database_master_secret_arn = var.home_lab ? aws_secretsmanager_secret.database_master[0].arn : (local.compute_mode == "eks" ? aws_rds_cluster.relay[0].master_user_secret[0].secret_arn : aws_db_instance.relay[0].master_user_secret[0].secret_arn)
}

check "customer_slug" {
  assert {
    condition     = length(local.customer_slug) >= 2
    error_message = "customer_name must produce a DNS-safe slug with at least two characters."
  }
}
