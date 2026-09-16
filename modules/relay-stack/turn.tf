locals {
  turn_fqdn           = "turn.${var.domain_name}"
  turn_repository_url = aws_ecr_repository.relay["relay-coturn"].repository_url
  turn_registry       = split("/", local.turn_repository_url)[0]
  turn_enabled        = local.profile.turn_instances > 0
}

data "aws_ssm_parameter" "turn_amazon_linux_arm64" {
  count = local.turn_enabled ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_security_group" "turn" {
  count = local.turn_enabled ? 1 : 0
  #checkov:skip=CKV2_AWS_5:The conditional group is attached to every conditional TURN instance through vpc_security_group_ids; Home Lab creates neither resource.
  name_prefix = "${local.name}-turn-"
  description = "Public TURN entry and bounded media relay ports"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.common_tags, { Name = "${local.name}-turn" })
  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_ingress_rule" "turn_udp" {
  count             = local.turn_enabled ? 1 : 0
  security_group_id = aws_security_group.turn[0].id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 3478
  to_port           = 3478
  ip_protocol       = "udp"
  description       = "TURN over UDP"
}

resource "aws_vpc_security_group_ingress_rule" "turn_tcp" {
  count             = local.turn_enabled ? 1 : 0
  security_group_id = aws_security_group.turn[0].id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 3478
  to_port           = 3478
  ip_protocol       = "tcp"
  description       = "TURN over TCP"
}

resource "aws_vpc_security_group_ingress_rule" "turn_relay_udp" {
  count             = local.turn_enabled ? 1 : 0
  security_group_id = aws_security_group.turn[0].id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 49152
  to_port           = 65535
  ip_protocol       = "udp"
  description       = "Allocated TURN media ports"
}

resource "aws_vpc_security_group_egress_rule" "turn_media_outbound" {
  count             = local.turn_enabled ? 1 : 0
  security_group_id = aws_security_group.turn[0].id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 1
  to_port           = 65535
  ip_protocol       = "udp"
  description       = "TURN relayed media to internet peers"
}

resource "aws_vpc_security_group_egress_rule" "turn_https" {
  count             = local.turn_enabled ? 1 : 0
  security_group_id = aws_security_group.turn[0].id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "TLS-protected AWS APIs and image downloads"
}

resource "aws_vpc_security_group_egress_rule" "turn_dns_udp" {
  count             = local.turn_enabled ? 1 : 0
  security_group_id = aws_security_group.turn[0].id
  cidr_ipv4         = "${cidrhost(var.vpc_cidr, 2)}/32"
  from_port         = 53
  to_port           = 53
  ip_protocol       = "udp"
  description       = "VPC resolver DNS"
}

resource "aws_vpc_security_group_egress_rule" "turn_dns_tcp" {
  count             = local.turn_enabled ? 1 : 0
  security_group_id = aws_security_group.turn[0].id
  cidr_ipv4         = "${cidrhost(var.vpc_cidr, 2)}/32"
  from_port         = 53
  to_port           = 53
  ip_protocol       = "tcp"
  description       = "VPC resolver DNS fallback"
}

resource "aws_vpc_security_group_egress_rule" "turn_metadata" {
  count             = local.turn_enabled ? 1 : 0
  security_group_id = aws_security_group.turn[0].id
  cidr_ipv4         = "169.254.169.254/32"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  description       = "IMDSv2 credentials and instance metadata"
}

resource "aws_vpc_security_group_egress_rule" "turn_time_sync" {
  count             = local.turn_enabled ? 1 : 0
  security_group_id = aws_security_group.turn[0].id
  cidr_ipv4         = "169.254.169.123/32"
  from_port         = 123
  to_port           = 123
  ip_protocol       = "udp"
  description       = "Amazon Time Sync Service"
}

resource "aws_iam_role" "turn" {
  count                = local.turn_enabled ? 1 : 0
  name                 = "${local.name}-turn"
  assume_role_policy   = data.aws_iam_policy_document.compute_assume.json
  permissions_boundary = var.workload_permissions_boundary_arn
  tags                 = local.common_tags
}

resource "aws_iam_role_policy_attachment" "turn_ssm" {
  count      = local.turn_enabled ? 1 : 0
  role       = aws_iam_role.turn[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "turn" {
  count = local.turn_enabled ? 1 : 0

  statement {
    sid       = "EcrAuthentication"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid       = "PullTurnImage"
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [aws_ecr_repository.relay["relay-coturn"].arn]
  }

  statement {
    sid       = "ReadTurnCredential"
    actions   = ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.runtime.arn]
  }

  statement {
    sid       = "DecryptTurnCredential"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.relay.arn]
  }

  statement {
    sid       = "WriteTurnLogs"
    actions   = ["logs:CreateLogStream", "logs:DescribeLogStreams", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.application.arn}:*"]
  }
}

resource "aws_iam_role_policy" "turn" {
  count  = local.turn_enabled ? 1 : 0
  name   = "turn-runtime"
  role   = aws_iam_role.turn[0].id
  policy = data.aws_iam_policy_document.turn[0].json
}

resource "aws_iam_instance_profile" "turn" {
  count = local.turn_enabled ? 1 : 0
  name  = "${local.name}-turn"
  role  = aws_iam_role.turn[0].name
}

#checkov:skip=CKV_AWS_88: TURN must have stable public IPs for direct UDP/TCP relay traffic; its SG exposes only TURN ports and IMDSv2 is mandatory.
resource "aws_instance" "turn" {
  count = local.profile.turn_instances

  ami                         = data.aws_ssm_parameter.turn_amazon_linux_arm64[0].value
  instance_type               = local.profile.turn_instance_type
  ebs_optimized               = true
  subnet_id                   = element(module.vpc.public_subnets, count.index)
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.turn[0].id]
  iam_instance_profile        = aws_iam_instance_profile.turn[0].name
  monitoring                  = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_size           = 20
    volume_type           = "gp3"
    encrypted             = true
    kms_key_id            = aws_kms_key.relay.arn
    delete_on_termination = true
  }

  user_data_replace_on_change = true
  user_data                   = <<-USERDATA
    #!/bin/bash
    set -euo pipefail
    dnf install -y docker
    systemctl enable --now docker amazon-ssm-agent
    install -d -m 0700 /etc/relay

    cat >/usr/local/sbin/relay-turn-reconcile <<'SCRIPT'
    #!/usr/bin/env bash
    set -euo pipefail
    umask 077
    token="$(curl --fail --silent --show-error -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' http://169.254.169.254/latest/api/token)"
    public_ip="$(curl --fail --silent --show-error -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/public-ipv4)"
    instance_id="$(curl --fail --silent --show-error -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/instance-id)"
    secret_file="$(mktemp)"
    trap 'rm -f "$secret_file"' EXIT
    aws secretsmanager get-secret-value --region '${var.aws_region}' --secret-id '${aws_secretsmanager_secret.runtime.arn}' --query SecretString --output text >"$secret_file"
    python3 - "$secret_file" /etc/relay/turnserver.conf '${local.turn_fqdn}' "$public_ip" <<'PY'
    import json, pathlib, sys
    source, destination, realm, public_ip = sys.argv[1:]
    values = json.loads(pathlib.Path(source).read_text(encoding="utf-8"))
    username = values.get("TURN_USERNAME", "")
    credential = values.get("TURN_CREDENTIAL", "")
    if not username or not credential or any(c in username + credential for c in "\r\n:"):
        raise SystemExit("TURN_USERNAME and TURN_CREDENTIAL must be safe non-empty values")
    config = f"""listening-port=3478
    min-port=49152
    max-port=65535
    fingerprint
    lt-cred-mech
    realm={realm}
    user={username}:{credential}
    external-ip={public_ip}
    no-multicast-peers
    no-tls
    log-file=stdout
    pidfile=/tmp/turnserver.pid
    stale-nonce=600
    """
    pathlib.Path(destination).write_text(config, encoding="utf-8")
    PY
    chown 65534:65534 /etc/relay/turnserver.conf
    chmod 0400 /etc/relay/turnserver.conf
    aws ecr get-login-password --region '${var.aws_region}' | docker login --username AWS --password-stdin '${local.turn_registry}'
    image='${local.turn_repository_url}@${var.turn_image_digest}'
    docker pull "$image"
    desired_sha="$(sha256sum /etc/relay/turnserver.conf | cut -d' ' -f1)"
    current_sha="$(docker inspect --format '{{ index .Config.Labels "relay.config-sha" }}' relay-turn 2>/dev/null || true)"
    current_image="$(docker inspect --format '{{ .Config.Image }}' relay-turn 2>/dev/null || true)"
    turn_healthy() {
      docker inspect --format '{{ .State.Running }} {{ .RestartCount }} {{ if .State.Health }}{{ .State.Health.Status }}{{ end }}' relay-turn 2>/dev/null | grep -qx 'true 0 healthy' &&
        docker exec --user 65534:65534 relay-turn /bin/sh -c 'test -r /etc/coturn/turnserver.conf' &&
        ss -H -lnt | grep -Eq '(^|[[:space:]])[^[:space:]]*:3478[[:space:]]' &&
        ss -H -lnu | grep -Eq '(^|[[:space:]])[^[:space:]]*:3478[[:space:]]'
    }
    if [[ "$current_sha" == "$desired_sha" && "$current_image" == "$image" ]] && turn_healthy; then
      exit 0
    fi
    docker rm --force relay-turn >/dev/null 2>&1 || true
    docker run --detach --name relay-turn --restart unless-stopped --network host \
      --read-only --tmpfs /tmp:size=64m,mode=1777 --tmpfs /var/lib/coturn:size=64m,mode=0700,uid=65534,gid=65534 \
      --user 65534:65534 \
      --cap-drop ALL --cap-add NET_BIND_SERVICE --security-opt no-new-privileges:true \
      --health-cmd 'test -r /etc/coturn/turnserver.conf && test "$(cat /proc/1/comm)" = turnserver' \
      --health-start-period 5s --health-interval 5s --health-timeout 2s --health-retries 6 \
      --label "relay.config-sha=$desired_sha" \
      --log-driver awslogs --log-opt awslogs-region='${var.aws_region}' \
      --log-opt awslogs-group='${aws_cloudwatch_log_group.application.name}' \
      --log-opt "awslogs-stream=turn-$instance_id" \
      --volume /etc/relay/turnserver.conf:/etc/coturn/turnserver.conf:ro \
      "$image" -c /etc/coturn/turnserver.conf
    for attempt in $(seq 1 30); do
      if turn_healthy; then
        sleep 5
        turn_healthy && exit 0
      fi
      sleep 2
    done
    docker inspect --format 'status={{ .State.Status }} exit={{ .State.ExitCode }} restarts={{ .RestartCount }} health={{ if .State.Health }}{{ .State.Health.Status }}{{ end }}' relay-turn >&2 || true
    exit 1
    SCRIPT
    chmod 0700 /usr/local/sbin/relay-turn-reconcile

    cat >/etc/systemd/system/relay-turn-reconcile.service <<'UNIT'
    [Unit]
    Description=Reconcile the Relay TURN container
    After=docker.service network-online.target
    Wants=network-online.target
    [Service]
    Type=oneshot
    ExecStart=/usr/local/sbin/relay-turn-reconcile
    UNIT
    cat >/etc/systemd/system/relay-turn-reconcile.timer <<'UNIT'
    [Unit]
    Description=Periodically reconcile the Relay TURN container
    [Timer]
    OnBootSec=30s
    OnUnitActiveSec=5m
    RandomizedDelaySec=30s
    Persistent=true
    [Install]
    WantedBy=timers.target
    UNIT
    systemctl daemon-reload
    systemctl enable --now relay-turn-reconcile.timer
  USERDATA

  tags = merge(local.common_tags, {
    Name         = "${local.name}-turn-${count.index + 1}"
    Component    = "turn"
    GitOpsTarget = "true"
  })

  depends_on = [
    aws_iam_role_policy.turn,
    aws_iam_role_policy_attachment.turn_ssm,
    aws_cloudwatch_log_group.application,
  ]
}

resource "aws_eip" "turn" {
  count    = local.profile.turn_instances
  domain   = "vpc"
  instance = aws_instance.turn[count.index].id
  tags     = merge(local.common_tags, { Name = "${local.name}-turn-${count.index + 1}" })
}

resource "aws_route53_health_check" "turn" {
  count = local.profile.turn_instances

  ip_address        = aws_eip.turn[count.index].public_ip
  port              = 3478
  type              = "TCP"
  request_interval  = 30
  failure_threshold = 3
  tags              = merge(local.common_tags, { Name = "${local.name}-turn-${count.index + 1}" })
}

resource "aws_route53_record" "turn" {
  count = local.managed_dns ? local.profile.turn_instances : 0

  zone_id                          = var.hosted_zone_id
  name                             = local.turn_fqdn
  type                             = "A"
  ttl                              = 60
  set_identifier                   = "turn-${count.index + 1}"
  multivalue_answer_routing_policy = true
  health_check_id                  = aws_route53_health_check.turn[count.index].id
  records                          = [aws_eip.turn[count.index].public_ip]
}

resource "aws_cloudwatch_metric_alarm" "turn_instance_status" {
  count = local.profile.turn_instances

  alarm_name          = "${local.name}-turn-${count.index + 1}-status"
  alarm_description   = "A Relay TURN node failed its EC2 status check."
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
  dimensions          = { InstanceId = aws_instance.turn[count.index].id }
  tags                = local.common_tags
}
