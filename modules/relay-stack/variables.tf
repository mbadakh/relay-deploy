variable "aws_region" {
  description = "AWS region in which this isolated customer stack is created."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]$", var.aws_region))
    error_message = "aws_region must be a valid AWS region name."
  }
}

variable "customer_name" {
  description = "Human-readable customer name."
  type        = string
  validation {
    condition     = length(trimspace(var.customer_name)) >= 2 && length(var.customer_name) <= 80
    error_message = "customer_name must contain between 2 and 80 characters."
  }
}

variable "environment" {
  type    = string
  default = "production"
  validation {
    condition     = contains(["production", "staging", "development"], var.environment)
    error_message = "environment must be production, staging, or development."
  }
}

variable "capacity_profile" {
  description = "Expected registered-user tier selected by the customer pipeline."
  type        = string
  validation {
    condition     = contains(["10", "100", "1k", "10k", "100k", "1m", "10m"], var.capacity_profile)
    error_message = "capacity_profile must be one of: 10, 100, 1k, 10k, 100k, 1m, 10m."
  }
}

variable "home_lab" {
  description = "Deploy the budget-oriented, single-node profile. Relay, Keycloak, PostgreSQL, TLS ingress, and TURN share one EC2 instance; managed HA edge/database services are omitted."
  type        = bool
  default     = false
}

variable "offboarding" {
  description = "Ephemeral, workflow-only confirmation used during an authorized full destroy. Never persist this value in a customer leaf."
  type        = bool
  default     = false
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
  validation {
    condition     = can(cidrhost(var.vpc_cidr, 65534)) && can(regex("/(?:[0-9]|1[0-6])$", var.vpc_cidr))
    error_message = "vpc_cidr must be valid IPv4 and provide at least a /16-sized address space."
  }
}

variable "domain_name" {
  description = "Exact public Relay hostname, for example relay.customer.example."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$", var.domain_name))
    error_message = "domain_name must be a lower-case fully-qualified DNS name without a trailing dot."
  }
}

variable "hosted_zone_id" {
  description = "Route53 public hosted-zone ID authoritative for domain_name and auth.domain_name."
  type        = string
  validation {
    condition     = var.hosted_zone_id == "" || can(regex("^Z[A-Z0-9]+$", var.hosted_zone_id))
    error_message = "hosted_zone_id must be a Route53 hosted-zone ID."
  }
}

variable "relay_container_port" {
  type    = number
  default = 3001
  validation {
    condition     = var.relay_container_port >= 1024 && var.relay_container_port <= 65535
    error_message = "relay_container_port must be between 1024 and 65535."
  }
}

variable "keycloak_container_port" {
  type    = number
  default = 8080
}

variable "relay_node_port" {
  description = "Fixed EKS NodePort targeted by the Terraform-managed ALB."
  type        = number
  default     = 30080
  validation {
    condition     = var.relay_node_port >= 30000 && var.relay_node_port <= 32767
    error_message = "relay_node_port must be in the Kubernetes NodePort range."
  }
}

variable "keycloak_node_port" {
  description = "Fixed EKS NodePort targeted by the Terraform-managed ALB."
  type        = number
  default     = 30081
  validation {
    condition     = var.keycloak_node_port >= 30000 && var.keycloak_node_port <= 32767 && var.keycloak_node_port != var.relay_node_port
    error_message = "keycloak_node_port must be unique and in the Kubernetes NodePort range."
  }
}

variable "kubernetes_namespace" {
  description = "Namespace in which Argo CD deploys this customer workload."
  type        = string
  default     = "relay"
  validation {
    condition     = can(regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", var.kubernetes_namespace))
    error_message = "kubernetes_namespace must be a DNS-label-compatible Kubernetes namespace."
  }
}

variable "relay_app_replicas" {
  description = "Relay replicas selected by the capacity profile. Values above one require a signed scale-ready release attestation."
  type        = number
  default     = 1
  validation {
    condition     = var.relay_app_replicas >= 1 && var.relay_app_replicas <= 500
    error_message = "relay_app_replicas must be between 1 and 500."
  }
}

variable "scale_ready_release" {
  description = "True only when the immutable release catalog attests distributed realtime state and representative load testing."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect persistent data stores and load balancers from accidental deletion."
  type        = bool
  default     = true
}

variable "force_destroy_nonproduction_buckets" {
  type    = bool
  default = false
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "alarm_topic_arns" {
  description = "Pre-approved SNS topic ARNs notified by customer workload alarms."
  type        = list(string)
  default     = []
}

variable "turn_image_digest" {
  description = "Immutable multi-architecture coturn image digest promoted into the customer ECR repository."
  type        = string
  default     = "sha256:bbefd3e1fdfdc0d58770fe01b581fd8b00d9f3a5580d00acb77cf719a6bc78e3"
  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.turn_image_digest))
    error_message = "turn_image_digest must be a sha256 OCI digest."
  }
}

variable "caddy_image_digest" {
  description = "Immutable multi-architecture Caddy image digest used by the Home Lab TLS edge."
  type        = string
  default     = "sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d"
  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.caddy_image_digest))
    error_message = "caddy_image_digest must be a sha256 OCI digest."
  }
}

variable "postgres_image_digest" {
  description = "Immutable multi-architecture PostgreSQL image digest used by the Home Lab database."
  type        = string
  default     = "sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685"
  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.postgres_image_digest))
    error_message = "postgres_image_digest must be a sha256 OCI digest."
  }
}

check "relay_horizontal_scaling_attested" {
  assert {
    condition     = var.relay_app_replicas == 1 || var.scale_ready_release
    error_message = "Relay replicas above one require scale_ready_release=true from the trusted release catalog."
  }
}

check "home_lab_capacity" {
  assert {
    condition     = !var.home_lab || contains(["10", "100"], var.capacity_profile)
    error_message = "The single-node Home Lab profile supports only the 10 and 100 registered-user tiers."
  }
}

check "home_lab_single_node" {
  assert {
    condition     = !var.home_lab || var.relay_app_replicas == 1
    error_message = "The Home Lab profile must use exactly one Relay application replica."
  }
}

variable "operator_principal_arn" {
  description = "Customer IAM user/role running the installer; no platform or GitHub trust."
  type        = string
}
variable "workload_permissions_boundary_arn" {
  description = "Optional customer-owned IAM permissions boundary."
  type        = string
  default     = null
}
variable "public_origin" {
  description = "Customer-facing origin; empty uses the generated IP or load-balancer hostname over HTTP until a reverse proxy is configured."
  type        = string
  default     = ""
}
