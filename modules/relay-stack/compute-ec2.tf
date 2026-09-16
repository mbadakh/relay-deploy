data "aws_ssm_parameter" "amazon_linux_arm64" {
  count = local.compute_mode == "ec2" ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

locals {
  # A customer-specific Auto Scaling service-linked role keeps each customer
  # key grant isolated. Keep the suffix short enough for IAM's 64-character
  # role-name limit once AWS prepends AWSServiceRoleForAutoScaling_.
  # v2 removes the unsafe create-before-destroy dependency propagation while
  # also giving accounts with a tainted v1 partial apply a collision-free,
  # deterministic migration path.
  ec2_autoscaling_role_suffix = "relay-v2-${substr(sha256(local.name), 0, 13)}"

  # AWS publishes this bundle for verified TLS connections to every current
  # regional RDS CA. Pin the bytes so a compromised download fails cloud-init.
  rds_ca_bundle_url    = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
  rds_ca_bundle_sha256 = "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"

  # Encode each branch before the conditional. If Terraform first unifies the
  # mixed object with {}, it coerces every value to string and turns the schema
  # version into "1", which the fail-closed reconciler correctly rejects.
  ec2_reconciler_config_json = local.compute_mode == "ec2" ? jsonencode({
    schemaVersion     = 1
    region            = var.aws_region
    configBucket      = aws_s3_bucket.relay["config"].id
    desiredStateKey   = "ec2/desired-state.json"
    signingKeyArn     = aws_kms_key.ec2_config_signing[0].arn
    dataKeyArn        = aws_kms_key.relay.arn
    runtimeSecretArn  = aws_secretsmanager_secret.runtime.arn
    keycloakSecretArn = aws_secretsmanager_secret.keycloak.arn
    databaseSecretArn = local.database_master_secret_arn
    ecrRegistry       = split("/", aws_ecr_repository.relay["relay-server"].repository_url)[0]
    customerSlug      = local.customer_slug
  }) : "{}"

  ec2_reconciler_bootstrap = local.compute_mode == "ec2" ? {
    bucket     = aws_s3_bucket.relay["config"].id
    key        = aws_s3_object.ec2_reconciler[0].key
    versionId  = aws_s3_object.ec2_reconciler[0].version_id
    sha256     = filesha256("${path.module}/files/relay-ec2-reconcile.py")
    dataKeyArn = aws_kms_key.relay.arn
  } : {}

  ec2_cloudwatch_agent_config = {
    agent = {
      metrics_collection_interval = 60
      run_as_user                 = "root"
    }
    logs = {
      logs_collected = {
        files = {
          collect_list = [{
            file_path       = "/var/log/relay-reconcile.log"
            log_group_name  = aws_cloudwatch_log_group.application.name
            log_stream_name = "{instance_id}/reconciler"
            timezone        = "UTC"
          }]
        }
      }
    }
  }
}

resource "aws_iam_service_linked_role" "relay_autoscaling" {
  count = local.is_managed_ec2 ? 1 : 0

  aws_service_name = "autoscaling.amazonaws.com"
  custom_suffix    = local.ec2_autoscaling_role_suffix
  description      = "Customer-isolated Auto Scaling role for ${local.name}"

  # The suffix is deterministic, so replacement must release the existing
  # name before AWS can create it again after a partial apply.
  lifecycle { create_before_destroy = false }
}

resource "aws_launch_template" "relay" {
  count = local.compute_mode == "ec2" ? 1 : 0

  name_prefix   = "${local.name}-"
  image_id      = data.aws_ssm_parameter.amazon_linux_arm64[0].value
  instance_type = local.profile.ec2_instance_type
  ebs_optimized = true
  user_data = base64encode(<<-USERDATA
    #!/bin/bash
    set -euo pipefail
    umask 027
    # Amazon Linux 2023 ships curl-minimal, which provides the curl binary.
    # Installing the mutually exclusive full curl package makes cloud-init fail.
    dnf install -y docker amazon-cloudwatch-agent python3
    command -v aws >/dev/null
    command -v curl >/dev/null
    compose_version="v5.5.1"
    compose_sha256="732e3a84c1a0f67256ce80bc2598a24546b10ca05f9faa97efceb1171ece2ef7"
    install -d -m 0755 /usr/local/lib/docker/cli-plugins
    curl --fail --location --silent --show-error \
      "https://github.com/docker/compose/releases/download/$${compose_version}/docker-compose-linux-aarch64" \
      --output /tmp/docker-compose
    echo "$${compose_sha256}  /tmp/docker-compose" | sha256sum --check --status
    install -m 0755 /tmp/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
    rm -f /tmp/docker-compose
    %{if var.home_lab~}
    # Docker's entire state (PostgreSQL, Keycloak, Caddy certificates and
    # images) lives on a separately managed EBS volume which survives instance
    # replacement. The attachment can arrive shortly after cloud-init starts.
    data_volume_id='${aws_ebs_volume.homelab_data[0].id}'
    data_volume_serial="$${data_volume_id//-/}"
    data_device=""
    for attempt in $(seq 1 180); do
      for candidate in "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_$${data_volume_serial}" /dev/sdf; do
        if [[ -b "$candidate" ]]; then
          data_device="$(readlink -f "$candidate")"
          break 2
        fi
      done
      sleep 5
    done
    [[ -n "$data_device" ]] || { echo 'Home Lab data EBS volume did not attach' >&2; exit 1; }
    if ! blkid "$data_device" >/dev/null 2>&1; then
      mkfs.xfs -L relay-data "$data_device"
    fi
    install -d -m 0710 /var/lib/docker
    data_uuid="$(blkid -s UUID -o value "$data_device")"
    grep -q "UUID=$${data_uuid} " /etc/fstab || printf 'UUID=%s /var/lib/docker xfs defaults,nofail 0 2\n' "$data_uuid" >> /etc/fstab
    mountpoint -q /var/lib/docker || mount /var/lib/docker

    # A small encrypted swapfile gives Keycloak/PostgreSQL brief burst room on
    # the budget node without pretending the node is horizontally available.
    if [[ ! -f /swapfile ]]; then
      fallocate -l 2G /swapfile
      chmod 0600 /swapfile
      mkswap /swapfile
      swapon /swapfile
      printf '/swapfile none swap sw 0 0\n' >> /etc/fstab
    fi
    %{endif~}
    systemctl enable --now docker amazon-ssm-agent

    install -d -m 0750 /opt/relay /opt/relay/releases /opt/relay/keycloak/realm
    install -d -m 0755 /etc/relay
    install -d -m 0700 /run/relay
    install -m 0600 /dev/null /var/log/relay-reconcile.log

    curl --fail --location --silent --show-error \
      '${local.rds_ca_bundle_url}' \
      --output /run/relay/rds-global-bundle.pem
    echo '${local.rds_ca_bundle_sha256}  /run/relay/rds-global-bundle.pem' | sha256sum --check --status
    install -m 0644 /run/relay/rds-global-bundle.pem /etc/relay/rds-global-bundle.pem
    rm -f /run/relay/rds-global-bundle.pem

    printf '%s' '${base64encode(jsonencode(local.ec2_reconciler_bootstrap))}' | base64 -d > /run/relay/bootstrap.json
    bootstrap_value() {
      python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]])' /run/relay/bootstrap.json "$1"
    }
    config_bucket="$(bootstrap_value bucket)"
    reconciler_key="$(bootstrap_value key)"
    reconciler_version="$(bootstrap_value versionId)"
    reconciler_sha256="$(bootstrap_value sha256)"
    data_key_arn="$(bootstrap_value dataKeyArn)"
    aws s3api get-object \
      --region '${var.aws_region}' \
      --bucket "$config_bucket" \
      --key "$reconciler_key" \
      --version-id "$reconciler_version" \
      /run/relay/reconciler.py > /run/relay/reconciler-object.json
    python3 - /run/relay/reconciler-object.json "$reconciler_version" "$data_key_arn" <<'PY'
    import json, sys
    value = json.load(open(sys.argv[1], encoding="utf-8"))
    if value.get("VersionId") != sys.argv[2]:
        raise SystemExit("reconciler S3 VersionId mismatch")
    if value.get("ServerSideEncryption") != "aws:kms" or value.get("SSEKMSKeyId") != sys.argv[3]:
        raise SystemExit("reconciler S3 encryption mismatch")
    PY
    echo "$reconciler_sha256  /run/relay/reconciler.py" | sha256sum --check --status
    install -m 0750 /run/relay/reconciler.py /usr/local/sbin/relay-ec2-reconcile
    printf '%s' '${base64encode(local.ec2_reconciler_config_json)}' | base64 -d > /etc/relay/reconciler.json
    chmod 0640 /etc/relay/reconciler.json

    cat > /etc/systemd/system/relay-reconcile.service <<'UNIT'
    [Unit]
    Description=Reconcile Relay signed EC2 desired state
    Wants=network-online.target
    After=network-online.target docker.service
    Requires=docker.service

    [Service]
    Type=oneshot
    ExecStart=/usr/local/sbin/relay-ec2-reconcile
    TimeoutStartSec=1800
    UMask=0077
    NoNewPrivileges=yes
    PrivateTmp=yes
    ProtectHome=yes
    ProtectKernelLogs=yes
    ProtectKernelModules=yes
    ProtectKernelTunables=yes
    ProtectControlGroups=yes
    RestrictSUIDSGID=yes
    LockPersonality=yes
    RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
    StandardOutput=append:/var/log/relay-reconcile.log
    StandardError=append:/var/log/relay-reconcile.log
    UNIT

    cat > /etc/systemd/system/relay-reconcile.timer <<'TIMER'
    [Unit]
    Description=Periodically reconcile Relay EC2 desired state

    [Timer]
    OnBootSec=30s
    OnUnitInactiveSec=2m
    RandomizedDelaySec=30s
    Persistent=true
    Unit=relay-reconcile.service

    [Install]
    WantedBy=timers.target
    TIMER

    printf '%s' '${base64encode(jsonencode(local.ec2_cloudwatch_agent_config))}' | base64 -d > /etc/relay/cloudwatch-agent.json
    /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
      -a fetch-config -m ec2 -s -c file:/etc/relay/cloudwatch-agent.json
    systemctl daemon-reload
    systemctl enable --now relay-reconcile.timer
    touch /opt/relay/READY_FOR_GITOPS
    systemctl start relay-reconcile.service
  USERDATA
  )

  # Home Lab supplies the profile directly on aws_instance so exact-plan
  # scanners can verify it. Passing both that name and this ARN makes the EC2
  # RunInstances API reject the request, so keep the launch-template form only
  # for the managed Auto Scaling topology.
  dynamic "iam_instance_profile" {
    for_each = local.is_managed_ec2 ? [1] : []
    content {
      arn = aws_iam_instance_profile.compute[0].arn
    }
  }
  # Home Lab defines subnet, public-IP behavior, and security groups together
  # on aws_instance. Mixing that primary-interface request with template-level
  # security groups is rejected by EC2. Managed Auto Scaling keeps the group
  # in its launch template.
  vpc_security_group_ids = local.is_managed_ec2 ? [aws_security_group.application.id] : null

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
    # Relay uses the AWS SDK default credential provider from a bridge-network
    # container. IMDSv2 responses therefore need one host hop plus one bridge
    # hop. Tokens remain mandatory; a hop limit of one silently breaks S3.
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "enabled"
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = var.home_lab ? 12 : 40
      volume_type           = "gp3"
      encrypted             = true
      kms_key_id            = aws_kms_key.relay.arn
      delete_on_termination = true
    }
  }

  monitoring { enabled = true }

  dynamic "credit_specification" {
    for_each = var.home_lab ? [1] : []
    content {
      # Keep the budget profile deterministic: sustained bursts may throttle,
      # but cannot create unbounded T-family surplus-credit charges.
      cpu_credits = "standard"
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = merge(local.common_tags, { Name = local.name, GitOpsTarget = "true" })
  }
  tag_specifications {
    resource_type = "volume"
    tags          = local.common_tags
  }
  tags = local.common_tags

  lifecycle {
    precondition {
      condition     = try(jsondecode(local.ec2_reconciler_config_json).schemaVersion == 1, false)
      error_message = "EC2 reconciler configuration must contain numeric schemaVersion 1."
    }
  }

  depends_on = [
    aws_iam_role_policy.compute,
    aws_iam_role_policy_attachment.ssm,
    aws_cloudwatch_log_group.application,
  ]
}

resource "aws_autoscaling_group" "relay" {
  count = local.is_managed_ec2 ? 1 : 0

  name                    = local.name
  vpc_zone_identifier     = module.vpc.private_subnets
  min_size                = var.relay_app_replicas
  desired_capacity        = var.relay_app_replicas
  max_size                = var.relay_app_replicas + 1
  service_linked_role_arn = aws_iam_service_linked_role.relay_autoscaling[0].arn
  # Workloads are reconciled only after RDS, secrets, and ECR promotion. EC2
  # health avoids a replacement loop while the ALB targets wait for that stage;
  # load-balancer target health is monitored independently.
  health_check_type         = "EC2"
  health_check_grace_period = 300
  target_group_arns = [
    aws_lb_target_group.relay[0].arn,
    aws_lb_target_group.keycloak[0].arn,
  ]

  launch_template {
    id = aws_launch_template.relay[0].id
    # Pin the concrete version so Terraform observes bootstrap/AMI changes and
    # the ASG instance-refresh block deterministically rolls replacements.
    version = aws_launch_template.relay[0].latest_version
  }

  dynamic "tag" {
    for_each = merge(local.common_tags, { Name = local.name, GitOpsTarget = "true" })
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 100
      max_healthy_percentage = 200
    }
  }
}
