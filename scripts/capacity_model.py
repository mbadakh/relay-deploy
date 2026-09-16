#!/usr/bin/env python3
"""Versioned workload assumptions used by customer pricing artifacts.

The values here estimate a steady-state month; they are not throughput promises.
Keep the topology facts aligned with modules/relay-stack/locals.tf.
"""

from __future__ import annotations

import math
from typing import Any


REGISTERED_USERS = {
    "10": 10,
    "100": 100,
    "1k": 1_000,
    "10k": 10_000,
    "100k": 100_000,
    "1m": 1_000_000,
    "10m": 10_000_000,
}

TOPOLOGY_FACTS = {
    "10": {"availability_zones": 2, "nat_gateways": 1, "turn_instances": 1, "log_retention_days": 365},
    "100": {"availability_zones": 2, "nat_gateways": 1, "turn_instances": 1, "log_retention_days": 365},
    "1k": {"availability_zones": 2, "nat_gateways": 2, "turn_instances": 2, "log_retention_days": 365},
    "10k": {"availability_zones": 3, "nat_gateways": 3, "turn_instances": 2, "log_retention_days": 365},
    "100k": {"availability_zones": 3, "nat_gateways": 3, "turn_instances": 2, "log_retention_days": 365},
    "1m": {"availability_zones": 3, "nat_gateways": 3, "turn_instances": 4, "log_retention_days": 365},
    "10m": {"availability_zones": 3, "nat_gateways": 3, "turn_instances": 12, "log_retention_days": 365},
}

# Base messaging-v1 assumptions. Traffic quantities use binary GiB throughout.
DAU_RATIO = 0.25
MESSAGES_PER_DAU_DAY = 50
PEAK_TO_AVERAGE_MESSAGE_RATE = 10
PEAK_CONCURRENT_CALL_RATIO = 0.002
# One copy enters the service and one copy leaves for the recipient. Only the
# outbound copy is internet egress; both copies are processed by the ALB.
MESSAGE_WIRE_KIB = 1.5
MESSAGE_RECIPIENTS = 1
# Includes the message plus its indexes. One logical message read and write are
# deliberately simple Aurora I/O anchors, not database-engine benchmarks.
MESSAGE_DATABASE_KIB = 1.5
DATABASE_READ_IOS_PER_MESSAGE = 1
DATABASE_WRITE_IOS_PER_MESSAGE = 1
DATABASE_RETENTION_DAYS = 365
MEDIA_UPLOADS_PER_DAU_DAY = 0.05
MEDIA_UPLOAD_MIB = 5
MEDIA_READS_PER_UPLOAD = 3
MEDIA_RETENTION_DAYS = 365
APP_OPENS_PER_DAU_DAY = 2
CONTROL_REQUESTS_PER_APP_OPEN = 4
ALB_RULES_EVALUATED_PER_REQUEST = 2
ALB_FREE_RULES_PER_REQUEST = 10
APP_LOG_KIB_PER_MESSAGE = 1
WAF_LOG_KIB_PER_REQUEST = 2
WAF_LOGGED_REQUEST_RATIO = 0.01
CALLS_PER_DAU_DAY = 0.1
AVERAGE_CALL_MINUTES = 5
TURN_SHARE = 0.20
AUDIO_CALL_SHARE = 0.80
VIDEO_CALL_SHARE = 0.20
# Aggregate outbound bitrate from the TURN server to both call participants.
AUDIO_TURN_OUT_MBIT_S = 0.128
VIDEO_TURN_OUT_MBIT_S = 3.0
HOURS_PER_MONTH = 730
SECONDS_PER_DAY = 86_400
DAYS_PER_MONTH = 30


def ceil_int(value: float, minimum: int = 0) -> int:
    return max(minimum, math.ceil(value))


def build_capacity_model(tier: str, *, home_lab: bool = False) -> dict[str, Any]:
    """Return deterministic monthly usage quantities for one capacity tier."""
    if tier not in REGISTERED_USERS:
        raise ValueError(f"unsupported capacity tier: {tier}")

    registered = REGISTERED_USERS[tier]
    topology = dict(TOPOLOGY_FACTS[tier])
    if home_lab:
        if tier not in {"10", "100"}:
            raise ValueError("Home Lab supports only the 10 and 100 user tiers")
        topology.update(
            availability_zones=1,
            nat_gateways=0,
            turn_instances=1,
            log_retention_days=365,
        )
    # Keep the fleet-level expectation fractional for the smallest profile.
    # Rounding 2.5 DAU down to 2 would make the generated 10-user estimate
    # disagree with the documented model and systematically understate usage.
    dau = max(1.0, registered * DAU_RATIO)
    messages = ceil_int(dau * MESSAGES_PER_DAU_DAY * DAYS_PER_MONTH)
    uploads = max(1, round(dau * MEDIA_UPLOADS_PER_DAU_DAY * DAYS_PER_MONTH))
    new_media_gb = uploads * MEDIA_UPLOAD_MIB / 1024
    media_download_gb = new_media_gb * MEDIA_READS_PER_UPLOAD
    retained_media_gb = new_media_gb * MEDIA_RETENTION_DAYS / DAYS_PER_MONTH
    new_database_gb = messages * MESSAGE_DATABASE_KIB / (1024 * 1024)
    retained_database_gb = (
        new_database_gb * DATABASE_RETENTION_DAYS / DAYS_PER_MONTH
    )
    average_messages_per_second = messages / (DAYS_PER_MONTH * SECONDS_PER_DAY)

    app_opens = ceil_int(dau * APP_OPENS_PER_DAU_DAY * DAYS_PER_MONTH)
    control_requests = app_opens * CONTROL_REQUESTS_PER_APP_OPEN
    # WAF sees the WebSocket upgrade, HTTP control calls, and media requests;
    # it does not inspect each frame on an established WebSocket connection.
    edge_requests = app_opens + control_requests + uploads * (1 + MEDIA_READS_PER_UPLOAD)
    billable_rule_evaluations = edge_requests * max(
        ALB_RULES_EVALUATED_PER_REQUEST - ALB_FREE_RULES_PER_REQUEST, 0
    )
    message_ingress_gb = messages * MESSAGE_WIRE_KIB / (1024 * 1024)
    message_outbound_gb = message_ingress_gb * MESSAGE_RECIPIENTS
    alb_monthly_gb = (
        new_media_gb
        + media_download_gb
        + message_ingress_gb
        + message_outbound_gb
    )

    calls = dau * CALLS_PER_DAU_DAY * DAYS_PER_MONTH
    turn_minutes = calls * AVERAGE_CALL_MINUTES * TURN_SHARE
    weighted_turn_mbit_s = (
        AUDIO_CALL_SHARE * AUDIO_TURN_OUT_MBIT_S
        + VIDEO_CALL_SHARE * VIDEO_TURN_OUT_MBIT_S
    )
    turn_outbound_gb = turn_minutes * 60 * weighted_turn_mbit_s / 8 / 1024

    app_log_gb = messages * APP_LOG_KIB_PER_MESSAGE / (1024 * 1024)
    waf_log_gb = (
        edge_requests
        * WAF_LOGGED_REQUEST_RATIO
        * WAF_LOG_KIB_PER_REQUEST
        / (1024 * 1024)
    )
    retention_months = topology["log_retention_days"] / DAYS_PER_MONTH
    # ALB/VPC access archives vary with flow volume. One KiB per modeled edge
    # request plus 10% envelope/index overhead is a reviewable base estimate.
    archive_log_new_gb = edge_requests * 1.1 / (1024 * 1024)
    archive_puts = DAYS_PER_MONTH * 24 * (1 + topology["availability_zones"])

    # Media uses the S3 gateway endpoint and does not traverse NAT. This small
    # message-derived allowance covers external notification/control egress.
    nat_total_gb = messages * 0.25 / (1024 * 1024)
    nat_per_gateway_gb = (
        nat_total_gb / topology["nat_gateways"]
        if topology["nat_gateways"]
        else 0
    )
    # ALB-to-target traffic in the same VPC is free, including cross-AZ. Model
    # actual application/database traffic instead: one payload write and read,
    # weighted by the chance that app and database endpoints are in different
    # AZs. A singleton NAT also adds its non-local AZ's control traffic.
    cross_az_probability = (topology["availability_zones"] - 1) / topology[
        "availability_zones"
    ]
    database_cross_az_gb = (
        messages * MESSAGE_DATABASE_KIB * 2 / (1024 * 1024)
    ) * cross_az_probability
    nat_cross_az_gb = (
        nat_total_gb * cross_az_probability
        if topology["nat_gateways"] == 1
        else 0
    )
    backup_s3_get_requests = (uploads + 100) * 8
    retained_backup_objects = ceil_int(
        uploads * MEDIA_RETENTION_DAYS / DAYS_PER_MONTH + 100, 1
    )
    backup_s3_list_requests = (
        ceil_int(retained_backup_objects / 500, 1) * DAYS_PER_MONTH
    )
    if home_lab:
        # Home Lab protects its local database through the encrypted node
        # backup and relies on S3 versioning for media/configuration.
        backup_s3_get_requests = 0
        backup_s3_list_requests = 0

    return {
        "model_version": "messaging-v1-home-lab" if home_lab else "messaging-v1",
        "registered_users": registered,
        "daily_active_users": dau,
        "monthly_messages": messages,
        "peak_messages_per_second": (
            average_messages_per_second * PEAK_TO_AVERAGE_MESSAGE_RATE
        ),
        "peak_concurrent_calls": ceil_int(
            registered * PEAK_CONCURRENT_CALL_RATIO, 1
        ),
        "monthly_media_uploads": uploads,
        "monthly_new_media_gb": new_media_gb,
        "retained_media_storage_gb": ceil_int(retained_media_gb, 1),
        "monthly_media_download_gb": media_download_gb,
        "monthly_new_database_gb": new_database_gb,
        "retained_database_storage_gb": ceil_int(retained_database_gb, 1),
        "database_read_requests_per_second": ceil_int(
            average_messages_per_second * DATABASE_READ_IOS_PER_MESSAGE, 1
        ),
        "database_write_requests_per_second": ceil_int(
            average_messages_per_second * DATABASE_WRITE_IOS_PER_MESSAGE, 1
        ),
        "monthly_edge_requests": edge_requests,
        "monthly_calls": calls,
        "monthly_turn_outbound_gb": turn_outbound_gb,
        "app_log_ingest_gb": ceil_int(app_log_gb, 1),
        "app_log_storage_gb": ceil_int(app_log_gb * retention_months, 1),
        "waf_log_ingest_gb": ceil_int(waf_log_gb, 1),
        "waf_log_storage_gb": ceil_int(waf_log_gb * retention_months, 1),
        "archive_log_ingest_gb": ceil_int(archive_log_new_gb, 1),
        "archive_log_storage_gb": ceil_int(archive_log_new_gb * retention_months, 1),
        "archive_log_put_requests": archive_puts,
        "backup_s3_get_requests": backup_s3_get_requests,
        "backup_s3_list_requests": backup_s3_list_requests,
        "backup_eventbridge_events": 0 if home_lab else uploads + 100,
        # S3 Bucket Keys are enabled. Model their documented up-to-99% KMS
        # request reduction plus a 10k/month envelope-key operations allowance.
        "kms_api_requests": 10_000 + ceil_int(uploads * 0.01),
        "new_connections_per_second": ceil_int(app_opens / (DAYS_PER_MONTH * SECONDS_PER_DAY), 1),
        # Hold the 5% peak socket target across the hour for a conservative LCU.
        "modeled_active_connections": ceil_int(registered * 0.05, 1),
        # ALB's processed_bytes_gb input is average GB/hour, not GB/month.
        "alb_processed_gb_per_hour": max(alb_monthly_gb / HOURS_PER_MONTH, 0.001),
        "rule_evaluations_per_second": ceil_int(
            billable_rule_evaluations / (DAYS_PER_MONTH * SECONDS_PER_DAY),
        ),
        "waf_monthly_requests": edge_requests,
        "route53_queries_per_turn_record": ceil_int(
            calls * 2 / topology["turn_instances"], 1
        ),
        "nat_data_processed_per_gateway_gb": max(nat_per_gateway_gb, 0.001),
        # AWS charges this traffic at both endpoints; the estimator applies the
        # factor explicitly. ALB-to-target traffic is correctly excluded.
        "regional_data_transfer_gb": max(
            database_cross_az_gb + nat_cross_az_gb, 0.001
        ),
        "internet_data_transfer_gb": max(
            media_download_gb + message_outbound_gb + turn_outbound_gb, 0.001
        ),
        **topology,
    }
