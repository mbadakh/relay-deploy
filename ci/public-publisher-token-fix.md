# Approved token-exchange correction

The approved role and repository variables have been installed. GitHub OIDC succeeds on the local runners, but ECR rejects `GetAuthorizationToken` because the `sts:GetServiceBearerToken` statement does not match with its service-name condition.

The approved change removes only this condition from that statement:

```json
"Condition": {"StringEquals": {"sts:AWSServiceName": "ecr-public.amazonaws.com"}}
```

The resulting statement is:

```json
{"Effect":"Allow","Action":["sts:GetServiceBearerToken"],"Resource":"*"}
```

This broadens token-exchange permission. It does not grant image uploads to any additional repositories, add infrastructure permissions, change the two trusted branch subjects, or create AWS access keys. The six existing ECR repository resource restrictions remain exact.

AWS's documented ECR policies grant this dependent token action without the service-name condition. References: [required token permissions](https://docs.aws.amazon.com/AmazonECRPublic/latest/APIReference/API_GetAuthorizationToken.html) and [AWS-managed public ECR policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonElasticContainerRegistryPublicFullAccess.html). We are not proposing the managed full-access policy; the full proposed replacement inline policy is [public-publisher-token-fix.json](public-publisher-token-fix.json).

The owner explicitly approved this correction and it has been applied to the live role. OIDC trust and repository upload limits are unchanged.
