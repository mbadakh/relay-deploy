resource "aws_security_group" "load_balancer" {
  count = local.managed_edge_enabled ? 1 : 0

  name_prefix = "${local.name}-alb-"
  description = "Public HTTPS ingress to Relay"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.common_tags, { Name = "${local.name}-alb" })
  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  count             = local.managed_edge_enabled ? 1 : 0
  security_group_id = aws_security_group.load_balancer[0].id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = local.managed_tls ? 443 : 80
  to_port           = local.managed_tls ? 443 : 80
  ip_protocol       = "tcp"
  description       = "Relay HTTPS"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_relay" {
  count             = local.managed_edge_enabled ? 1 : 0
  security_group_id = aws_security_group.load_balancer[0].id
  cidr_ipv4         = var.vpc_cidr
  from_port         = local.relay_target_port
  to_port           = local.relay_target_port
  ip_protocol       = "tcp"
  description       = "Relay targets in the customer VPC"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_keycloak" {
  count             = local.managed_edge_enabled ? 1 : 0
  security_group_id = aws_security_group.load_balancer[0].id
  cidr_ipv4         = var.vpc_cidr
  from_port         = local.keycloak_target_port
  to_port           = local.keycloak_target_port
  ip_protocol       = "tcp"
  description       = "Keycloak targets in the customer VPC"
}

resource "aws_security_group" "application" {
  name_prefix = "${local.name}-app-"
  description = local.is_home_lab ? "Home Lab TLS and TURN ingress" : "Relay application ingress from its ALB only"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.common_tags, { Name = "${local.name}-application" })
  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_ingress_rule" "application_relay_from_alb" {
  count                        = local.is_managed_ec2 ? 1 : 0
  security_group_id            = aws_security_group.application.id
  referenced_security_group_id = aws_security_group.load_balancer[0].id
  from_port                    = var.relay_container_port
  to_port                      = var.relay_container_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "application_keycloak_from_alb" {
  count                        = local.is_managed_ec2 ? 1 : 0
  security_group_id            = aws_security_group.application.id
  referenced_security_group_id = aws_security_group.load_balancer[0].id
  from_port                    = var.keycloak_container_port
  to_port                      = var.keycloak_container_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "application_https" {
  security_group_id = aws_security_group.application.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "TLS-protected AWS and public APIs through managed NAT"
}

resource "aws_vpc_security_group_egress_rule" "application_dns_udp" {
  security_group_id = aws_security_group.application.id
  cidr_ipv4         = "${cidrhost(var.vpc_cidr, 2)}/32"
  from_port         = 53
  to_port           = 53
  ip_protocol       = "udp"
  description       = "VPC resolver DNS"
}

resource "aws_vpc_security_group_egress_rule" "application_dns_tcp" {
  security_group_id = aws_security_group.application.id
  cidr_ipv4         = "${cidrhost(var.vpc_cidr, 2)}/32"
  from_port         = 53
  to_port           = 53
  ip_protocol       = "tcp"
  description       = "VPC resolver DNS fallback"
}

resource "aws_vpc_security_group_egress_rule" "application_database" {
  count = local.is_managed_ec2 ? 1 : 0

  security_group_id            = aws_security_group.application.id
  referenced_security_group_id = aws_security_group.database[0].id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "Relay PostgreSQL"
}

resource "aws_vpc_security_group_egress_rule" "application_metadata" {
  count = local.compute_mode == "ec2" ? 1 : 0

  security_group_id = aws_security_group.application.id
  cidr_ipv4         = "169.254.169.254/32"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  description       = "IMDSv2 credentials and instance metadata"
}

resource "aws_vpc_security_group_egress_rule" "application_time_sync" {
  count = local.compute_mode == "ec2" ? 1 : 0

  security_group_id = aws_security_group.application.id
  cidr_ipv4         = "169.254.169.123/32"
  from_port         = 123
  to_port           = 123
  ip_protocol       = "udp"
  description       = "Amazon Time Sync Service"
}

resource "aws_acm_certificate" "relay" {
  count = local.managed_edge_enabled && local.managed_tls ? 1 : 0

  domain_name               = local.app_fqdn
  subject_alternative_names = []
  validation_method         = "DNS"
  tags                      = local.common_tags
  lifecycle { create_before_destroy = true }
}

resource "aws_route53_record" "certificate_validation" {
  for_each = local.managed_edge_enabled && local.managed_dns ? {
    for option in aws_acm_certificate.relay[0].domain_validation_options : option.domain_name => {
      name   = option.resource_record_name
      record = option.resource_record_value
      type   = option.resource_record_type
    }
  } : {}

  allow_overwrite = true
  zone_id         = var.hosted_zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 300
  records         = [each.value.record]
}

resource "aws_acm_certificate_validation" "relay" {
  count                   = local.managed_edge_enabled && local.managed_tls ? 1 : 0
  certificate_arn         = aws_acm_certificate.relay[0].arn
  validation_record_fqdns = [for record in aws_route53_record.certificate_validation : record.fqdn]
}

resource "aws_lb" "relay" {
  count = local.managed_edge_enabled ? 1 : 0
  #checkov:skip=CKV_AWS_150:Deletion protection is enabled for every live deployment and disabled only by the separately reviewed, digest-bound offboarding preparation plan.

  name                       = "${local.edge_name}-alb"
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.load_balancer[0].id]
  subnets                    = module.vpc.public_subnets
  enable_deletion_protection = var.deletion_protection && !var.offboarding
  drop_invalid_header_fields = true
  enable_http2               = true
  idle_timeout               = 3600

  access_logs {
    bucket  = aws_s3_bucket.relay["logs"].id
    prefix  = "alb"
    enabled = true
  }

  tags       = local.common_tags
  depends_on = [aws_s3_bucket_policy.logs]
}

resource "aws_lb_target_group" "relay" {
  count = local.managed_edge_enabled ? 1 : 0

  name        = "${local.edge_name}-app"
  port        = local.relay_target_port
  protocol    = "HTTP"
  vpc_id      = module.vpc.vpc_id
  target_type = "instance"

  deregistration_delay = 30
  slow_start           = 30

  health_check {
    enabled             = true
    path                = "/api/health"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 15
    matcher             = "200-399"
  }

  stickiness {
    enabled         = true
    type            = "lb_cookie"
    cookie_duration = 86400
  }

  tags = local.common_tags
}


resource "aws_lb_target_group" "keycloak" {
  count = local.managed_edge_enabled ? 1 : 0

  name        = "${local.edge_name}-auth"
  port        = local.keycloak_target_port
  protocol    = "HTTP"
  vpc_id      = module.vpc.vpc_id
  target_type = "instance"

  deregistration_delay = 30
  slow_start           = 30

  health_check {
    enabled             = true
    path                = "/auth/realms/relay"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 5
    timeout             = 5
    interval            = 15
    matcher             = "200-399"
  }

  stickiness {
    enabled         = true
    type            = "lb_cookie"
    cookie_duration = 3600
  }

  tags = local.common_tags
}

resource "aws_lb_listener" "https" {
  count             = local.managed_edge_enabled ? 1 : 0
  load_balancer_arn = aws_lb.relay[0].arn
  port              = local.managed_tls ? 443 : 80
  protocol          = local.managed_tls ? "HTTPS" : "HTTP"
  ssl_policy        = local.managed_tls ? "ELBSecurityPolicy-TLS13-1-2-2021-06" : null
  certificate_arn   = local.managed_tls ? aws_acm_certificate_validation.relay[0].certificate_arn : null

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Not found"
      status_code  = "404"
    }
  }
}


resource "aws_lb_listener_rule" "relay" {
  count        = local.managed_edge_enabled ? 1 : 0
  listener_arn = aws_lb_listener.https[0].arn
  priority     = 20

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.relay[0].arn
  }

  condition {
    path_pattern { values = ["/*"] }
  }
}

resource "aws_lb_listener_rule" "keycloak" {
  count        = local.managed_edge_enabled ? 1 : 0
  listener_arn = aws_lb_listener.https[0].arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.keycloak[0].arn
  }

  condition {
    path_pattern { values = ["/auth", "/auth/*"] }
  }
}

resource "aws_autoscaling_attachment" "eks_relay" {
  count = local.compute_mode == "eks" ? 1 : 0

  autoscaling_group_name = one(module.eks[0].eks_managed_node_groups_autoscaling_group_names)
  lb_target_group_arn    = aws_lb_target_group.relay[0].arn
}

resource "aws_autoscaling_attachment" "eks_keycloak" {
  count = local.compute_mode == "eks" ? 1 : 0

  autoscaling_group_name = one(module.eks[0].eks_managed_node_groups_autoscaling_group_names)
  lb_target_group_arn    = aws_lb_target_group.keycloak[0].arn
}

resource "aws_route53_record" "application" {
  for_each = local.managed_edge_enabled && local.managed_dns ? toset([local.app_fqdn, local.auth_fqdn]) : toset([])
  zone_id  = var.hosted_zone_id
  name     = each.value
  type     = "A"

  alias {
    name                   = aws_lb.relay[0].dns_name
    zone_id                = aws_lb.relay[0].zone_id
    evaluate_target_health = true
  }
}

resource "aws_wafv2_web_acl" "relay" {
  count = local.managed_edge_enabled ? 1 : 0

  name  = local.name
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "AWSManagedCommon"
    priority = 10
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"

        # Relay accepts large multipart media bodies. Keep the rest of the
        # common ruleset, but do not reject a valid upload solely because WAF
        # only inspects the first portion of an ALB request body.
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            count {}
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-common"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSManagedKnownBadInputs"
    priority = 20
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  # These non-terminating marker rules identify only the validator's exact
  # public probes. Keeping each route in a shallow statement tree avoids WAF's
  # nesting limits and prevents a spoofed User-Agent from exempting any other
  # application route from hosting-provider reputation enforcement.
  rule {
    name     = "MarkRelayHealthValidator"
    priority = 21
    action {
      count {}
    }
    rule_label { name = "endpoint-validator" }
    statement {
      and_statement {
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "relay-validator/1"
            field_to_match {
              single_header { name = "user-agent" }
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "GET"
            field_to_match {
              method {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = local.app_fqdn
            field_to_match {
              single_header { name = "host" }
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
        statement {
          or_statement {
            statement {
              byte_match_statement {
                positional_constraint = "EXACTLY"
                search_string         = "/api/health"
                field_to_match {
                  uri_path {}
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            statement {
              byte_match_statement {
                positional_constraint = "EXACTLY"
                search_string         = "/api/push/gateway-proof"
                field_to_match {
                  uri_path {}
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-validator-health"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "MarkKeycloakDiscoveryValidator"
    priority = 22
    action {
      count {}
    }
    rule_label { name = "endpoint-validator" }
    statement {
      and_statement {
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "relay-validator/1"
            field_to_match {
              single_header { name = "user-agent" }
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "GET"
            field_to_match {
              method {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = local.auth_fqdn
            field_to_match {
              single_header { name = "host" }
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "/auth/realms/relay/.well-known/openid-configuration"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-validator-discovery"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "MarkWebSocketValidator"
    priority = 23
    action {
      count {}
    }
    rule_label { name = "endpoint-validator" }
    statement {
      and_statement {
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "relay-validator/1"
            field_to_match {
              single_header { name = "user-agent" }
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "GET"
            field_to_match {
              method {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = local.app_fqdn
            field_to_match {
              single_header { name = "host" }
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "/socket.io/"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-validator-websocket"
      sampled_requests_enabled   = true
    }
  }

  # AWS-managed hosting-provider reputation rules classify legitimate
  # AWS-to-AWS federation traffic as cloud-originated. Mark only Relay's exact
  # discovery document and signed federation namespace; the application still
  # authenticates every mutable federation request cryptographically.
  rule {
    name     = "MarkRelayFederationDiscovery"
    priority = 24
    action {
      count {}
    }
    rule_label { name = "federation-peer" }
    statement {
      and_statement {
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "relay-federation/1"
            field_to_match {
              single_header { name = "user-agent" }
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "GET"
            field_to_match {
              method {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = local.app_fqdn
            field_to_match {
              single_header { name = "host" }
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "/.well-known/relay/server"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-federation-discovery"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "MarkSignedRelayFederationApi"
    priority = 25
    action {
      count {}
    }
    rule_label { name = "federation-peer" }
    statement {
      and_statement {
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "relay-federation/1"
            field_to_match {
              single_header { name = "user-agent" }
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = local.app_fqdn
            field_to_match {
              single_header { name = "host" }
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "STARTS_WITH"
            search_string         = "/api/federation/v1/"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
        statement {
          byte_match_statement {
            positional_constraint = "EXACTLY"
            search_string         = "https://${local.app_fqdn}"
            field_to_match {
              single_header { name = "x-relay-destination" }
            }
            text_transformation {
              priority = 0
              type     = "LOWERCASE"
            }
          }
        }
        statement {
          size_constraint_statement {
            comparison_operator = "GT"
            size                = 0
            field_to_match {
              single_header { name = "x-relay-signature" }
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-federation-api"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSManagedAnonymousIpList"
    priority = 26
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAnonymousIpList"
        vendor_name = "AWS"

        # Count this sub-rule so its managed label can be combined with the
        # exact validator marker below. AnonymousIPList itself still blocks in
        # this managed group, including for validator requests.
        rule_action_override {
          name = "HostingProviderIPList"
          action_to_use {
            count {}
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-anonymous-ip"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "BlockHostingProvidersExceptValidator"
    priority = 27
    action {
      block {}
    }
    statement {
      and_statement {
        statement {
          label_match_statement {
            scope = "LABEL"
            key   = "awswaf:managed:aws:anonymous-ip-list:HostingProviderIPList"
          }
        }
        statement {
          not_statement {
            statement {
              label_match_statement {
                scope = "LABEL"
                # This label is produced by rules in this same web ACL. Use the
                # local label name so WAF supplies the authoritative web ACL
                # namespace; constructing that namespace ourselves is rejected
                # by UpdateWebACL for existing ACLs in some AWS regions.
                key = "endpoint-validator"
              }
            }
          }
        }
        statement {
          not_statement {
            statement {
              label_match_statement {
                scope = "LABEL"
                key   = "federation-peer"
              }
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-hosting-provider-block"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "PerIpRateLimit"
    priority = 30
    action {
      block {}
    }
    statement {
      rate_based_statement {
        aggregate_key_type = "IP"
        limit              = local.profile.waf_rate_limit
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.name
    sampled_requests_enabled   = true
  }
  tags = local.common_tags
}

resource "aws_wafv2_web_acl_association" "relay" {
  count        = local.managed_edge_enabled ? 1 : 0
  resource_arn = aws_lb.relay[0].arn
  web_acl_arn  = aws_wafv2_web_acl.relay[0].arn
}
