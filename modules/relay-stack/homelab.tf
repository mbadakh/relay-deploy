# The Home Lab profile intentionally consolidates the full runtime onto one
# static-address node. It removes hourly NAT, ALB, WAF, managed database and
# dedicated TURN charges. This is production-usable for a small private group,
# but it is not highly available and instance replacement causes a brief restart.
resource "aws_ebs_volume" "homelab_data" {
  count = local.is_home_lab ? 1 : 0

  availability_zone = local.azs[0]
  size              = 20
  type              = "gp3"
  encrypted         = true
  kms_key_id        = aws_kms_key.relay.arn

  tags = merge(local.common_tags, {
    Name      = "${local.name}-home-lab-data"
    Component = "persistent-runtime-data"
  })
}

resource "aws_instance" "homelab" {
  count = local.is_home_lab ? 1 : 0

  # Keep the controls explicit on the instance as well as its required launch
  # template. AWS applies these RunInstances values as launch-template
  # overrides, and exact-plan policy scanners can verify them without having
  # to infer the relationship between two planned resources.
  ebs_optimized          = true
  iam_instance_profile   = aws_iam_instance_profile.compute[0].name
  monitoring             = true
  subnet_id              = module.vpc.public_subnets[0]
  vpc_security_group_ids = [aws_security_group.application.id]
  # Cloud-init needs internet access before Terraform can finish associating
  # the stable EIP below. The short-lived auto-assigned address avoids a boot
  # race during package/image bootstrap; AWS releases it as soon as the EIP is
  # associated, so the node still has only one billable public IPv4 address.
  associate_public_ip_address = true

  launch_template {
    id      = aws_launch_template.relay[0].id
    version = aws_launch_template.relay[0].latest_version
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_size           = 12
    volume_type           = "gp3"
    encrypted             = true
    kms_key_id            = aws_kms_key.relay.arn
    delete_on_termination = true
  }

  tags = merge(local.common_tags, {
    Name         = local.name
    Component    = "home-lab"
    GitOpsTarget = "true"
  })

  lifecycle {
    replace_triggered_by = [aws_launch_template.relay[0].latest_version]
  }
}

resource "aws_volume_attachment" "homelab_data" {
  count = local.is_home_lab ? 1 : 0

  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.homelab_data[0].id
  instance_id                    = aws_instance.homelab[0].id
  stop_instance_before_detaching = true
}

resource "aws_eip" "homelab" {
  count    = local.is_home_lab ? 1 : 0
  domain   = "vpc"
  instance = aws_instance.homelab[0].id
  tags     = merge(local.common_tags, { Name = "${local.name}-home-lab" })
}

resource "aws_route53_record" "homelab" {
  for_each = local.is_home_lab && local.managed_dns ? toset([local.app_fqdn, local.auth_fqdn, "turn.${var.domain_name}"]) : toset([])

  zone_id = var.hosted_zone_id
  name    = each.value
  type    = "A"
  ttl     = 60
  records = [aws_eip.homelab[0].public_ip]
}

resource "aws_vpc_security_group_ingress_rule" "homelab_https" {
  count             = local.is_home_lab ? 1 : 0
  security_group_id = aws_security_group.application.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "Caddy-managed Relay and Keycloak TLS"
}

resource "aws_vpc_security_group_ingress_rule" "homelab_turn_udp" {
  count             = local.is_home_lab ? 1 : 0
  security_group_id = aws_security_group.application.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 3478
  to_port           = 3478
  ip_protocol       = "udp"
  description       = "Co-located TURN over UDP"
}

resource "aws_vpc_security_group_ingress_rule" "homelab_turn_tcp" {
  count             = local.is_home_lab ? 1 : 0
  security_group_id = aws_security_group.application.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 3478
  to_port           = 3478
  ip_protocol       = "tcp"
  description       = "Co-located TURN over TCP"
}

resource "aws_vpc_security_group_ingress_rule" "homelab_turn_media" {
  count             = local.is_home_lab ? 1 : 0
  security_group_id = aws_security_group.application.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 49152
  to_port           = 65535
  ip_protocol       = "udp"
  description       = "Co-located TURN media allocation"
}

resource "aws_vpc_security_group_egress_rule" "homelab_turn_tcp" {
  count             = local.is_home_lab ? 1 : 0
  security_group_id = aws_security_group.application.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 1
  to_port           = 65535
  ip_protocol       = "tcp"
  description       = "TURN TCP relay and outbound public dependencies"
}

resource "aws_vpc_security_group_egress_rule" "homelab_turn_udp" {
  count             = local.is_home_lab ? 1 : 0
  security_group_id = aws_security_group.application.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 1
  to_port           = 65535
  ip_protocol       = "udp"
  description       = "TURN relayed media to internet peers"
}

resource "aws_cloudwatch_metric_alarm" "homelab_instance_status" {
  count = local.is_home_lab ? 1 : 0

  alarm_name          = "${local.name}-home-lab-status"
  alarm_description   = "The single Home Lab node failed its EC2 status check."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "StatusCheckFailed"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "breaching"
  alarm_actions       = var.alarm_topic_arns
  ok_actions          = var.alarm_topic_arns
  dimensions          = { InstanceId = aws_instance.homelab[0].id }
  tags                = local.common_tags
}

resource "aws_vpc_security_group_ingress_rule" "homelab_http" {
  count             = local.is_home_lab ? 1 : 0
  security_group_id = aws_security_group.application.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  description       = "HTTP endpoint and ACME renewal; use HTTPS for remote clients"
}
