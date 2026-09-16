# Relay customer stack

This module creates one isolated Relay customer data plane. Small profiles (`10`,
`100`, and `1k`) use one SSM-managed EC2 application host in an Auto Scaling Group.
Larger profiles use private-subnet EKS managed node groups. RDS/Aurora, S3, ECR,
KMS, Secrets Manager, ACM, Route53, ALB, WAF, and audit logs are managed AWS
resources.

The optional `home_lab` input selects a bounded 10/100-user alternative. It
keeps the complete service surface but runs Caddy, Relay, Keycloak, PostgreSQL,
and coturn on one static-address EC2 node and omits hourly ALB/WAF/NAT/RDS
costs. Persistent container state uses an independently managed encrypted EBS
volume so routine node replacement does not erase data. It is budget-oriented
and explicitly not highly available.

The Relay server remains exactly **one application replica** in every profile.
The present realtime/call implementation contains process-local state; node and
database headroom must not be mistaken for permission to scale that server
horizontally. Terraform rejects any other value for `relay_app_replicas`.

No secret value is accepted as a Terraform input. RDS/Aurora manages its own
master password in Secrets Manager. The runtime, Keycloak, and GitOps secret
resources are empty containers populated by the post-apply workflow, keeping
credentials out of Terraform state and Git.

The module exposes the realm administration URL, fixed non-secret initial
administrator username, and the ARN of the Secrets Manager value holding its
generated temporary credential. It never exposes the password through a
Terraform value, plan, state-derived workflow artifact, or customer Git leaf.

For EKS profiles, the Kubernetes API is private-only. Terraform creates a
no-source CodeBuild project in the private application subnets and grants its
customer-scoped service role an EKS access entry. GitHub uploads an immutable,
versioned deployment bundle and context to the KMS-encrypted config bucket,
starts the fixed project, and polls it to completion. No public API allow-list or
runner inside the customer VPC is required. The central `relay-local` runner
authenticates to AWS through the protected environment's OIDC role; long-lived
access keys are not used.

That role uses GitHub's immutable 2026 OIDC subject format and independently
checks the canonical repository name, immutable owner/repository IDs, exact
branch, protected Environment, and provisioning workflow display name. Direct
module callers must supply IDs returned as `.owner.id` and `.id` by GitHub's
repository API; name-only OIDC subjects are intentionally unsupported.

For EC2 profiles, Terraform installs a systemd reconciler from an exact,
SHA256-verified S3 object version. Application deployment advances a
KMS-signed desired-state pointer to an exact versioned Compose bundle. The
instance recreates `/run/relay` from Secrets Manager, accepts only the two
customer ECR repositories at immutable digests, health-checks Relay and
Keycloak, and retains the previous release for automatic rollback. ASG
replacements follow the same path and therefore do not depend on SSH or a
GitHub runner being online.
