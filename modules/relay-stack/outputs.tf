output "compute_mode" {
  value = local.compute_mode
}

output "capacity_profile" {
  value = var.capacity_profile
}

output "home_lab" {
  value       = local.is_home_lab
  description = "Whether this customer uses the budget-oriented single-node profile."
}

output "relay_app_replicas" {
  value       = var.relay_app_replicas
  description = "Capacity-profile replica count; values above one require an immutable scale-ready release attestation."
}

output "application_url" {
  value = local.public_origin
}

output "keycloak_url" {
  value = "${local.public_origin}/auth"
}

output "keycloak_admin_console_url" {
  value       = "${local.public_origin}/auth/admin/relay/console/"
  description = "Realm-scoped Keycloak administration console for the initial Relay administrator."
}

output "initial_relay_admin_username" {
  value       = "relay-admin"
  description = "Non-secret username of the initial Relay realm administrator."
}

output "initial_relay_admin_credentials_secret_arn" {
  value       = aws_secretsmanager_secret.keycloak.arn
  description = "Secrets Manager ARN containing the generated temporary initial administrator credential. The value is never returned by Terraform."
}

output "turn_url" {
  value = "turn:${local.is_home_lab ? aws_eip.homelab[0].public_ip : aws_eip.turn[0].public_ip}:3478?transport=udp"
}

output "turn_instance_ids" {
  value = aws_instance.turn[*].id
}

output "database_endpoint" {
  value     = local.is_home_lab ? "postgres" : (local.compute_mode == "eks" ? aws_rds_cluster.relay[0].endpoint : aws_db_instance.relay[0].address)
  sensitive = true
}

output "database_bootstrap_endpoint" {
  value       = local.is_home_lab ? "127.0.0.1" : (local.compute_mode == "eks" ? aws_rds_cluster.relay[0].endpoint : aws_db_instance.relay[0].address)
  sensitive   = true
  description = "Host used by database initialization; Home Lab bootstraps its local PostgreSQL container over loopback."
}

output "database_port" {
  value = 5432
}

output "database_master_secret_arn" {
  value = local.database_master_secret_arn
}

output "runtime_secret_arn" {
  value = aws_secretsmanager_secret.runtime.arn
}

output "keycloak_secret_arn" {
  value = aws_secretsmanager_secret.keycloak.arn
}

output "bucket_names" {
  value = { for purpose, bucket in aws_s3_bucket.relay : purpose => bucket.id }
}

output "ec2_config_signing_key_arn" {
  value       = local.compute_mode == "ec2" ? aws_kms_key.ec2_config_signing[0].arn : null
  description = "Asymmetric KMS key whose private key signs EC2 desired-state bundle digests."
}

output "ec2_reconciler_artifact" {
  value = local.compute_mode == "ec2" ? {
    bucket     = aws_s3_bucket.relay["config"].id
    key        = aws_s3_object.ec2_reconciler[0].key
    version_id = aws_s3_object.ec2_reconciler[0].version_id
    sha256     = filesha256("${path.module}/files/relay-ec2-reconcile.py")
  } : null
  description = "Content-addressed reconciler bootstrap pinned by S3 VersionId and SHA256."
}

output "ecr_repository_urls" {
  value = { for component, repository in aws_ecr_repository.relay : component => repository.repository_url }
}

output "ec2_asg_name" {
  value = local.is_managed_ec2 ? aws_autoscaling_group.relay[0].name : null
}

output "ec2_instance_ids" {
  value       = local.is_home_lab ? aws_instance.homelab[*].id : []
  description = "Static EC2 targets used by the Home Lab GitOps reconciler."
}

output "eks_cluster_name" {
  value = local.compute_mode == "eks" ? module.eks[0].cluster_name : null
}

output "eks_cluster_endpoint" {
  value     = local.compute_mode == "eks" ? module.eks[0].cluster_endpoint : null
  sensitive = true
}

output "alb_target_group_arn" {
  value       = local.managed_edge_enabled ? aws_lb_target_group.relay[0].arn : null
  description = "Relay target group used by the EC2 ASG or EKS node groups."
}

output "keycloak_target_group_arn" {
  value       = local.managed_edge_enabled ? aws_lb_target_group.keycloak[0].arn : null
  description = "Keycloak target group used by the EC2 ASG or EKS node groups."
}

output "certificate_arn" {
  value = local.managed_edge_enabled && local.managed_tls ? aws_acm_certificate_validation.relay[0].certificate_arn : null
}

output "waf_acl_arn" {
  value = local.managed_edge_enabled ? aws_wafv2_web_acl.relay[0].arn : null
}

output "home_lab_public_ip" {
  value       = local.is_home_lab ? aws_eip.homelab[0].public_ip : null
  description = "Static public endpoint shared by Home Lab HTTPS and TURN."
}

output "home_lab_postgres_volume_name" {
  value       = local.is_home_lab ? "relay-${local.customer_slug}-postgres-data" : null
  description = "Stable Docker volume initialized before the Home Lab workload starts."
}

output "home_lab_data_volume_id" {
  value       = local.is_home_lab ? aws_ebs_volume.homelab_data[0].id : null
  description = "Encrypted persistent Docker-state volume preserved across Home Lab instance replacement."
}

output "platform_image_digests" {
  value = {
    coturn   = var.turn_image_digest
    caddy    = var.caddy_image_digest
    postgres = var.postgres_image_digest
  }
}

output "kubernetes_namespace" {
  value = local.compute_mode == "eks" ? var.kubernetes_namespace : null
}

output "relay_pod_role_arn" {
  value = local.compute_mode == "eks" ? aws_iam_role.relay_pod[0].arn : null
}

output "external_secrets_pod_role_arn" {
  value = local.compute_mode == "eks" ? aws_iam_role.external_secrets_pod[0].arn : null
}

output "codebuild_project_name" {
  value       = local.compute_mode == "eks" ? aws_codebuild_project.eks_bootstrap[0].name : null
  description = "No-source in-VPC project used for private EKS bootstrap and reconciliation."
}

output "codebuild_log_group_name" {
  value       = local.compute_mode == "eks" ? aws_cloudwatch_log_group.codebuild[0].name : null
  description = "CloudWatch Logs group for auditable EKS bootstrap output."
}

output "customer_kms_key_arn" {
  value       = aws_kms_key.relay.arn
  description = "Customer-managed key protecting deployment bundles, secrets, and logs."
}

output "node_ports" {
  value = local.compute_mode == "eks" ? {
    relay    = var.relay_node_port
    keycloak = var.keycloak_node_port
  } : null
}

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "private_subnet_ids" {
  value = module.vpc.private_subnets
}

output "public_endpoint" { value = local.is_home_lab ? aws_eip.homelab[0].public_ip : aws_lb.relay[0].dns_name }
output "customer_slug" { value = local.customer_slug }
