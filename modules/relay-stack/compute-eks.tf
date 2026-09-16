module "eks" {
  count = local.compute_mode == "eks" ? 1 : 0
  # v20.36.0, pinned to the immutable upstream Git commit.
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-eks.git?ref=37e3348dffe06ea4b9adf9b54512e4efdb46f425"

  cluster_name    = local.name
  cluster_version = "1.33"

  cluster_endpoint_private_access          = true
  cluster_endpoint_public_access           = false
  enable_cluster_creator_admin_permissions = false
  iam_role_permissions_boundary            = var.workload_permissions_boundary_arn

  cluster_encryption_config = {
    provider_key_arn = aws_kms_key.relay.arn
    resources        = ["secrets"]
  }

  cluster_addons = {
    coredns                = { most_recent = true }
    kube-proxy             = { most_recent = true }
    vpc-cni                = { most_recent = true, before_compute = true }
    aws-ebs-csi-driver     = { most_recent = true }
    eks-pod-identity-agent = { most_recent = true, before_compute = true }
  }

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  eks_managed_node_groups = {
    relay = {
      ami_type       = "AL2023_ARM_64_STANDARD"
      instance_types = local.profile.eks_instance_types
      capacity_type  = "ON_DEMAND"
      min_size       = local.profile.eks_min
      desired_size   = local.profile.eks_desired
      max_size       = local.profile.eks_max
      disk_size      = 80
      # This module uses a custom launch template, so aws_eks_node_group ignores
      # disk_size. Declare the encrypted root volume in the launch template so
      # the planned capacity and the price report both contain the real 80 GiB.
      block_device_mappings = {
        root = {
          device_name = "/dev/xvda"
          ebs = {
            delete_on_termination = true
            encrypted             = true
            volume_size           = 80
            volume_type           = "gp3"
          }
        }
      }
      labels                        = { workload = "relay" }
      update_config                 = { max_unavailable_percentage = 33 }
      iam_role_permissions_boundary = var.workload_permissions_boundary_arn
    }
  }

  node_security_group_additional_rules = {
    alb_relay_ingress = {
      description              = "Relay NodePort traffic from customer ALB"
      protocol                 = "tcp"
      from_port                = var.relay_node_port
      to_port                  = var.relay_node_port
      type                     = "ingress"
      source_security_group_id = aws_security_group.load_balancer[0].id
    }
    alb_keycloak_ingress = {
      description              = "Keycloak NodePort traffic from customer ALB"
      protocol                 = "tcp"
      from_port                = var.keycloak_node_port
      to_port                  = var.keycloak_node_port
      type                     = "ingress"
      source_security_group_id = aws_security_group.load_balancer[0].id
    }
  }

  cluster_security_group_additional_rules = {
    codebuild_private_api = {
      description              = "Private Kubernetes API access from the customer CodeBuild bootstrap"
      protocol                 = "tcp"
      from_port                = 443
      to_port                  = 443
      type                     = "ingress"
      source_security_group_id = aws_security_group.codebuild[0].id
    }
  }

  access_entries = {
    platform_deploy = {
      principal_arn = var.operator_principal_arn
      policy_associations = {
        deploy = {
          policy_arn   = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = { type = "cluster" }
        }
      }
    }
    codebuild_deploy = {
      principal_arn = aws_iam_role.codebuild[0].arn
      policy_associations = {
        bootstrap = {
          policy_arn   = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = { type = "cluster" }
        }
      }
    }
  }

  tags = local.common_tags
}
