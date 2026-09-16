# Security boundaries

## Customer boundary

Terraform runs with the customer's own CLI identity. Generated runtime roles trust only their own compute services. This repository contains no customer inventory, platform AWS role, GitHub App key, Firebase private key or central DNS zone. Secrets are generated after Terraform and stored in customer-owned Secrets Manager. Local mode uses unique secrets in a private volume.

The operator's local machine, Docker daemon, Terraform providers, pinned tool image and this checkout are trusted. Review source and image changes before pulling updates. Local Terraform state is not encrypted automatically; use full-disk encryption and encrypted backups. Credentials passed as environment variables can be read by a privileged local administrator.

AWS uses encrypted storage, private databases, instance roles, SSM instead of SSH, and signed/versioned EC2 deployment bundles. Home lab remains a single host. Production sizing does not make the application horizontally scalable; uncertified large tiers are rejected.

## Platform boundary

The push gateway retains the official app's Firebase credential. A server proves control of its public HTTPS origin and stores its own random gateway credential in its own secret store. The gateway stores only its hash. Origin verification rejects private addresses, pins the resolved public IP and never follows redirects.

A server cannot authorize a device merely by knowing its FCM token. The app must have initiated enrollment for its currently selected server, receive a one-time FCM challenge, and return the proof directly to the gateway. Device grants expire, move on server changes, and support independent revocation. Operator suspension overrides reenrollment. Limits apply per tenant, device, IP and globally.

Notification payloads pass through Relay's gateway and Google FCM. The gateway avoids payload/token/credential logging. An authorized malicious customer server can still send abusive messages to its own enrolled devices. Quotas and suspension limit this; they do not eliminate it. The gateway is a shared availability dependency and currently a single-instance service without a durable delivery queue. FCM acceptance does not prove delivery to a phone.

## Open-source readiness

The repository and instructions remain private for owner review. Before making either public, review Git history and generated artifacts for secrets, document a security contact, and keep public-fork jobs off the privileged local runners. Publishing source does not require publishing AWS/Firebase credentials. Public deployment images necessarily expose their packaged application code; they were explicitly requested separately from source-repository publication.

## Operator references

- [AWS public ECR authorization permissions](https://docs.aws.amazon.com/AmazonECRPublic/latest/APIReference/API_GetAuthorizationToken.html)
- [AWS public ECR resource-scoped actions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ecr-public.html)
- [GitHub OIDC subject claims](https://docs.github.com/en/actions/reference/security/oidc)
