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
master password in Secrets Manager. The runtime and Keycloak secret
resources are empty containers populated by the post-apply workflow, keeping
credentials out of Terraform state and Git.

The module exposes the realm administration URL, fixed non-secret initial
administrator username, and the ARN of the Secrets Manager value holding its
generated temporary credential. It never exposes the password through a
Terraform value, plan, state-derived workflow artifact, or customer Git leaf.

For the retained EKS profiles, the Kubernetes API is private-only. Terraform
creates a no-source CodeBuild project inside the customer's VPC. The customer's
terminal uploads a versioned deployment bundle and invokes that project using
their own AWS identity. No central GitHub trust role or GitOps repository key is
created. The CLI currently rejects large tiers until a distributed release is
certified; these modules alone are not a scalability guarantee.

DNS and ACM are optional. Without a customer Route 53 zone the module returns an
HTTP public IP or load-balancer name to place behind the customer's own HTTPS
reverse proxy. Both application and Keycloak use one origin, with Keycloak under
`/auth`. The official Android app requires trusted public HTTPS.

For EC2 profiles, Terraform installs a systemd reconciler from an exact,
SHA256-verified S3 object version. Application deployment advances a
KMS-signed desired-state pointer to an exact versioned Compose bundle. The
instance recreates `/run/relay` from Secrets Manager, accepts only the two
customer ECR repositories at immutable digests, health-checks Relay and
Keycloak, and retains the previous release for automatic rollback. ASG
replacements follow the same path and therefore do not depend on SSH or a
GitHub runner being online.
