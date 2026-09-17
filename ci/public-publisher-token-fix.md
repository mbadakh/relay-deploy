# Proposed token-exchange correction — not applied

The approved role and repository variables have been installed. GitHub OIDC succeeds on the local runners, but ECR rejects `GetAuthorizationToken` because the `sts:GetServiceBearerToken` statement does not match with its service-name condition.

The proposed change removes only this condition from that statement:

```json
"Condition": {"StringEquals": {"sts:AWSServiceName": "ecr-public.amazonaws.com"}}
```

The resulting statement is:

```json
{"Effect":"Allow","Action":["sts:GetServiceBearerToken"],"Resource":"*"}
```

This broadens token-exchange permission. It does not grant image uploads to any additional repositories, add infrastructure permissions, change the two trusted branch subjects, or create AWS access keys. The six existing ECR repository resource restrictions remain exact.

AWS's documented ECR policies grant this dependent token action without the service-name condition. References: [required token permissions](https://docs.aws.amazon.com/AmazonECRPublic/latest/APIReference/API_GetAuthorizationToken.html) and [AWS-managed public ECR policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonElasticContainerRegistryPublicFullAccess.html). We are not proposing the managed full-access policy; the full proposed replacement inline policy is [public-publisher-token-fix.json](public-publisher-token-fix.json).

Automatic approval review rejected applying this correction because the earlier approval covered the narrower original policy. Explicit approval is required. The original live policy remains unchanged, image publishing remains blocked, and the installer remains gated against incompatible images.
