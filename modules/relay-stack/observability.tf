resource "aws_cloudwatch_log_group" "application" {
  name              = "/relay/${local.customer_slug}/application"
  retention_in_days = local.profile.log_retention_days
  kms_key_id        = aws_kms_key.relay.arn
  skip_destroy      = var.environment == "production" && !var.offboarding
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "waf" {
  count = local.managed_edge_enabled ? 1 : 0

  name              = "aws-waf-logs-${local.name}"
  retention_in_days = local.profile.log_retention_days
  kms_key_id        = aws_kms_key.relay.arn
  skip_destroy      = var.environment == "production" && !var.offboarding
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "codebuild" {
  count = local.compute_mode == "eks" ? 1 : 0

  name              = "/relay/${local.customer_slug}/eks-bootstrap"
  retention_in_days = local.profile.log_retention_days
  kms_key_id        = aws_kms_key.relay.arn
  skip_destroy      = var.environment == "production" && !var.offboarding
  tags              = local.common_tags
}

resource "aws_wafv2_web_acl_logging_configuration" "relay" {
  count                   = local.managed_edge_enabled ? 1 : 0
  resource_arn            = aws_wafv2_web_acl.relay[0].arn
  log_destination_configs = [aws_cloudwatch_log_group.waf[0].arn]

  redacted_fields {
    single_header { name = "authorization" }
  }

  redacted_fields {
    single_header { name = "cookie" }
  }

  logging_filter {
    default_behavior = "DROP"

    filter {
      behavior    = "KEEP"
      requirement = "MEETS_ANY"
      condition {
        action_condition { action = "BLOCK" }
      }
      condition {
        action_condition { action = "COUNT" }
      }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_relay_targets" {
  count = local.managed_edge_enabled ? 1 : 0

  alarm_name          = "${local.name}-relay-unhealthy-targets"
  alarm_description   = "Relay has one or more unhealthy load-balancer targets."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "UnHealthyHostCount"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "breaching"
  alarm_actions       = var.alarm_topic_arns
  ok_actions          = var.alarm_topic_arns

  dimensions = {
    LoadBalancer = aws_lb.relay[0].arn_suffix
    TargetGroup  = aws_lb_target_group.relay[0].arn_suffix
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_keycloak_targets" {
  count = local.managed_edge_enabled ? 1 : 0

  alarm_name          = "${local.name}-keycloak-unhealthy-targets"
  alarm_description   = "Keycloak has one or more unhealthy load-balancer targets."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "UnHealthyHostCount"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "breaching"
  alarm_actions       = var.alarm_topic_arns
  ok_actions          = var.alarm_topic_arns

  dimensions = {
    LoadBalancer = aws_lb.relay[0].arn_suffix
    TargetGroup  = aws_lb_target_group.keycloak[0].arn_suffix
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  count = local.managed_edge_enabled ? 1 : 0

  alarm_name          = "${local.name}-alb-5xx"
  alarm_description   = "The customer ALB is returning elevated HTTP 5xx responses."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  metric_name         = "HTTPCode_ELB_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Sum"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arns
  ok_actions          = var.alarm_topic_arns
  dimensions          = { LoadBalancer = aws_lb.relay[0].arn_suffix }
  tags                = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "database_cpu" {
  count = local.is_home_lab ? 0 : 1

  alarm_name          = "${local.name}-database-cpu"
  alarm_description   = "The Relay database has sustained high CPU utilization."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 5
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = 60
  statistic           = "Average"
  threshold           = 80
  treat_missing_data  = "breaching"
  alarm_actions       = var.alarm_topic_arns
  ok_actions          = var.alarm_topic_arns
  dimensions = local.compute_mode == "eks" ? {
    DBClusterIdentifier = aws_rds_cluster.relay[0].cluster_identifier
    } : {
    DBInstanceIdentifier = aws_db_instance.relay[0].identifier
  }
  tags = local.common_tags
}
