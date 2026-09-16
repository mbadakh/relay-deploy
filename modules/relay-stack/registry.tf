locals {
  # Relay's published server image already contains the web bundle and performs
  # idempotent schema migration at startup. Keycloak is mirrored from its
  # digest-pinned upstream image so production never pulls mutable public tags.
  image_repositories = toset(concat(
    ["relay-server", "relay-keycloak", "relay-coturn"],
    local.is_home_lab ? ["relay-caddy", "relay-postgres"] : [],
  ))
}

resource "aws_ecr_repository" "relay" {
  for_each = local.image_repositories

  name                 = "${local.customer_slug}/${each.value}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.offboarding

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.relay.arn
  }
  image_scanning_configuration { scan_on_push = true }
  tags = merge(local.common_tags, { Component = each.value })
}

resource "aws_ecr_lifecycle_policy" "relay" {
  for_each   = aws_ecr_repository.relay
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Retain the newest 30 untagged build images"
      selection = {
        tagStatus   = "untagged"
        countType   = "imageCountMoreThan"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}
