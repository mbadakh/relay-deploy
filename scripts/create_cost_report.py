#!/usr/bin/env python3
"""Price an exact Relay Terraform plan with AWS's public Price List API.

No third-party account, token, cached catalog, or hard-coded price is used. The
already-assumed GitHub OIDC plan role calls ``pricing:GetProducts``. Estimation
fails closed if a planned resource or a non-zero price is not understood.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from capacity_model import HOURS_PER_MONTH, build_capacity_model
from relay_customers import load_yaml


PRICE_API_REGION = "us-east-1"
INFINITY = Decimal("Infinity")


class CostEstimationError(ValueError):
    """Raised when the estimate would be incomplete or ambiguous."""


def number(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CostEstimationError(f"invalid {label}: {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise CostEstimationError(f"invalid {label}: {value!r}")
    return result


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class PlannedResource:
    address: str
    resource_type: str
    values: dict[str, Any]


@dataclass(frozen=True)
class PriceRequest:
    key: str
    service_code: str
    filters: tuple[tuple[str, str], ...]
    units: tuple[str, ...]
    description_pattern: str


@dataclass(frozen=True)
class PriceTier:
    begin: Decimal
    end: Decimal
    usd: Decimal
    description: str
    unit: str


@dataclass(frozen=True)
class PriceQuote:
    service_code: str
    sku: str
    effective_date: str
    tiers: tuple[PriceTier, ...]

    def cost(self, quantity: Decimal) -> Decimal:
        if quantity == 0:
            return Decimal(0)
        cursor = Decimal(0)
        total = Decimal(0)
        for tier in self.tiers:
            if tier.begin > cursor:
                raise CostEstimationError(
                    f"AWS SKU {self.sku} has an uncovered range beginning at {cursor}"
                )
            upper = min(quantity, tier.end)
            if upper > tier.begin:
                total += (upper - tier.begin) * tier.usd
                cursor = upper
            if cursor >= quantity:
                return total
        raise CostEstimationError(
            f"AWS SKU {self.sku} does not cover quantity {quantity}"
        )


class PriceResolver(Protocol):
    def quote(self, request: PriceRequest) -> PriceQuote: ...


class EksSupportResolver(Protocol):
    def status(self, region: str, version: str) -> str: ...


class AwsCliEksSupportResolver:
    """Resolve the current support tier for the exact planned EKS version."""

    def __init__(self, timeout_seconds: int = 90) -> None:
        self.timeout_seconds = timeout_seconds
        self.cache: dict[tuple[str, str], str] = {}

    def status(self, region: str, version: str) -> str:
        cache_key = (region, version)
        if cache_key in self.cache:
            return self.cache[cache_key]
        command = [
            "aws", "--region", region, "eks", "describe-cluster-versions",
            "--cluster-versions", version, "--output", "json",
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CostEstimationError(
                f"AWS EKS support lookup could not run for Kubernetes {version}"
            ) from exc
        if completed.returncode:
            raise CostEstimationError(
                f"AWS EKS support lookup failed for Kubernetes {version} "
                f"(exit {completed.returncode})"
            )
        try:
            versions = json.loads(completed.stdout)["clusterVersions"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise CostEstimationError(
                f"AWS returned invalid EKS support data for Kubernetes {version}"
            ) from exc
        matching = [
            item for item in versions
            if isinstance(item, dict) and str(item.get("clusterVersion")) == version
        ]
        if len(matching) != 1:
            raise CostEstimationError(
                f"AWS returned {len(matching)} EKS lifecycle records for "
                f"Kubernetes {version}"
            )
        status = str(matching[0].get("status", ""))
        if status not in {"STANDARD_SUPPORT", "EXTENDED_SUPPORT"}:
            raise CostEstimationError(
                f"unsupported EKS lifecycle status {status!r} for Kubernetes {version}"
            )
        self.cache[cache_key] = status
        return status


class AwsCliPriceResolver:
    """Query live AWS prices via the installed AWS CLI and short-lived role."""

    def __init__(self, timeout_seconds: int = 90) -> None:
        self.timeout_seconds = timeout_seconds
        self.cache: dict[PriceRequest, PriceQuote] = {}

    def quote(self, request: PriceRequest) -> PriceQuote:
        if request in self.cache:
            return self.cache[request]
        filters = [
            {"Type": "TERM_MATCH", "Field": key, "Value": value}
            for key, value in request.filters
        ]
        command = [
            "aws",
            "--region",
            PRICE_API_REGION,
            "pricing",
            "get-products",
            "--service-code",
            request.service_code,
            "--filters",
            json.dumps(filters, separators=(",", ":")),
            "--format-version",
            "aws_v1",
            "--max-results",
            "100",
            "--output",
            "json",
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CostEstimationError(
                f"AWS pricing query could not run for {request.key}"
            ) from exc
        if completed.returncode:
            raise CostEstimationError(
                f"AWS pricing query failed for {request.key} "
                f"(exit {completed.returncode})"
            )
        try:
            payload = json.loads(completed.stdout)
            raw_products = payload["PriceList"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise CostEstimationError(
                f"AWS pricing query returned invalid JSON for {request.key}"
            ) from exc
        if not isinstance(raw_products, list) or not raw_products:
            raise CostEstimationError(f"AWS returned no current price for {request.key}")
        products: list[dict[str, Any]] = []
        for raw in raw_products:
            try:
                product = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError as exc:
                raise CostEstimationError(
                    f"AWS returned an invalid product for {request.key}"
                ) from exc
            if isinstance(product, dict):
                products.append(product)
        quote = self.select_quote(request, products)
        self.cache[request] = quote
        return quote

    @staticmethod
    def select_quote(
        request: PriceRequest, products: Sequence[dict[str, Any]]
    ) -> PriceQuote:
        pattern = re.compile(request.description_pattern, re.IGNORECASE)
        accepted_units = {unit.casefold() for unit in request.units}
        groups: dict[str, tuple[str, list[PriceTier]]] = {}
        for item in products:
            product = item.get("product")
            terms = item.get("terms", {}).get("OnDemand", {})
            if not isinstance(product, dict) or not isinstance(terms, dict):
                continue
            sku = str(product.get("sku", ""))
            for term in terms.values():
                if not isinstance(term, dict):
                    continue
                effective = str(term.get("effectiveDate", ""))
                dimensions = term.get("priceDimensions", {})
                if not isinstance(dimensions, dict):
                    continue
                for dimension in dimensions.values():
                    if not isinstance(dimension, dict):
                        continue
                    unit = str(dimension.get("unit", ""))
                    description = str(dimension.get("description", ""))
                    if unit.casefold() not in accepted_units or not pattern.search(description):
                        continue
                    price = dimension.get("pricePerUnit", {}).get("USD")
                    if price is None:
                        continue
                    begin = number(dimension.get("beginRange", 0), "price begin range")
                    end_text = str(dimension.get("endRange", "Inf"))
                    end = (
                        INFINITY
                        if end_text.casefold() in {"inf", "infinity"}
                        else number(end_text, "price end range")
                    )
                    if end <= begin:
                        raise CostEstimationError(
                            f"AWS returned a malformed price tier for {request.key}"
                        )
                    groups.setdefault(sku, (effective, []))[1].append(
                        PriceTier(begin, end, number(price, "USD price"), description, unit)
                    )
        if not groups:
            raise CostEstimationError(
                f"AWS returned no matching price dimension for {request.key}"
            )
        if len(groups) != 1:
            raise CostEstimationError(
                f"AWS returned {len(groups)} matching current SKUs for "
                f"{request.key}; pricing is ambiguous"
            )
        sku = next(iter(groups))
        effective, tiers = groups[sku]
        tiers.sort(key=lambda item: (item.begin, item.end))
        if not tiers or tiers[0].begin != 0:
            raise CostEstimationError(
                f"AWS SKU {sku} does not have complete tier coverage for {request.key}"
            )
        return PriceQuote(request.service_code, sku, effective, tuple(tiers))


@dataclass
class CostComponent:
    name: str
    resource_address: str
    resource_type: str
    category: str
    quantity: str
    unit: str
    monthly_cost: str
    service_code: str
    sku: str
    effective_date: str
    price_tiers: list[dict[str, str]]
    assumption: str


# Configuration resources with no direct AWS charge. Dependent compute,
# storage, request and transfer usage is priced by a handled resource below.
NO_DIRECT_COST_TYPES = frozenset(
    {
        "aws_acm_certificate", "aws_acm_certificate_validation",
        "aws_autoscaling_attachment", "aws_backup_selection",
        "aws_db_parameter_group", "aws_db_subnet_group",
        "aws_rds_cluster_parameter_group",
        "aws_default_network_acl", "aws_default_route_table",
        "aws_default_security_group", "aws_ec2_tag",
        "aws_ecr_lifecycle_policy", "aws_eks_access_entry",
        "aws_eks_access_policy_association", "aws_eks_addon",
        "aws_eks_pod_identity_association", "aws_flow_log",
        "aws_iam_instance_profile", "aws_iam_openid_connect_provider",
        "aws_iam_policy", "aws_iam_role", "aws_iam_role_policy",
        "aws_iam_role_policy_attachment", "aws_iam_service_linked_role",
        "aws_internet_gateway", "aws_kms_alias", "aws_lb_listener",
        "aws_lb_listener_rule", "aws_lb_target_group", "aws_network_acl",
        "aws_network_acl_association", "aws_network_acl_rule", "aws_route",
        "aws_route53_record", "aws_route_table", "aws_route_table_association",
        "aws_s3_bucket_lifecycle_configuration", "aws_s3_bucket_logging",
        "aws_s3_bucket_ownership_controls", "aws_s3_bucket_policy",
        "aws_s3_bucket_public_access_block",
        "aws_s3_bucket_server_side_encryption_configuration",
        "aws_s3_bucket_versioning", "aws_s3_object", "aws_security_group",
        "aws_security_group_rule", "aws_subnet", "aws_vpc", "aws_vpc_endpoint",
        "aws_vpc_security_group_egress_rule",
        "aws_vpc_security_group_ingress_rule", "aws_wafv2_web_acl_association",
        "aws_wafv2_web_acl_logging_configuration", "aws_volume_attachment",
        "time_sleep",
    }
)

PRICED_RESOURCE_TYPES = frozenset(
    {
        "aws_autoscaling_group", "aws_backup_plan", "aws_backup_vault",
        "aws_cloudwatch_log_group", "aws_cloudwatch_metric_alarm", "aws_ebs_volume",
        "aws_codebuild_project", "aws_db_instance", "aws_ecr_repository",
        "aws_eip", "aws_eks_cluster", "aws_eks_node_group", "aws_instance",
        "aws_kms_key", "aws_launch_template", "aws_lb", "aws_nat_gateway",
        "aws_rds_cluster", "aws_rds_cluster_instance",
        "aws_route53_health_check", "aws_s3_bucket",
        "aws_secretsmanager_secret", "aws_wafv2_web_acl",
    }
)


def walk_resources(module: Any) -> Iterable[PlannedResource]:
    if not isinstance(module, dict):
        return
    for raw in module.get("resources", []):
        if not isinstance(raw, dict) or raw.get("mode", "managed") != "managed":
            continue
        resource_type = str(raw.get("type", ""))
        values = raw.get("values")
        if not isinstance(values, dict):
            # Terraform can represent an entirely-computed resource with null
            # planned values.  Keep known no-charge configuration resources in
            # the inventory so resource-type classification remains fail-closed;
            # priced or unknown resources still require concrete planned values.
            if resource_type not in NO_DIRECT_COST_TYPES:
                raise CostEstimationError(
                    f"planned resource {raw.get('address', '<unknown>')} has no values"
                )
            values = {}
        yield PlannedResource(str(raw.get("address", "")).removeprefix("module.relay."), resource_type, values)
    for child in module.get("child_modules", []):
        yield from walk_resources(child)


def filters(**values: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(values.items()))


class Estimator:
    def __init__(
        self,
        plan: dict[str, Any],
        customer: dict[str, Any],
        resolver: PriceResolver,
        eks_support_resolver: EksSupportResolver | None = None,
    ) -> None:
        self.plan = plan
        self.customer = customer
        self.resolver = resolver
        self.region = str(customer["aws_region"])
        self.home_lab = customer.get("home_lab", False) is True
        self.model = build_capacity_model(
            str(customer["expected_users"]), home_lab=self.home_lab
        )
        self.eks_support_resolver = eks_support_resolver
        root = plan.get("planned_values", {}).get("root_module")
        self.resources = list(walk_resources(root))
        if not self.resources:
            raise CostEstimationError("Terraform plan contains no managed resources")
        self.by_type: dict[str, list[PlannedResource]] = {}
        for resource in self.resources:
            self.by_type.setdefault(resource.resource_type, []).append(resource)
        unknown = sorted(set(self.by_type) - NO_DIRECT_COST_TYPES - PRICED_RESOURCE_TYPES)
        if unknown:
            raise CostEstimationError(
                "unclassified planned resource types (pricing is fail-closed): "
                + ", ".join(unknown)
            )
        self.validate_pricing_assumptions()
        self.components: list[CostComponent] = []

    def validate_pricing_assumptions(self) -> None:
        """Reject same-type resource changes that invalidate derived prices."""
        for endpoint in self.by_type.get("aws_vpc_endpoint", []):
            service_name = str(endpoint.values.get("service_name") or "")
            if (
                endpoint.values.get("vpc_endpoint_type") != "Gateway"
                or not service_name.endswith(".s3")
            ):
                raise CostEstimationError(
                    f"{endpoint.address} is not the free S3 gateway endpoint "
                    "expected by the pricing model"
                )

        for flow_log in self.by_type.get("aws_flow_log", []):
            options = flow_log.values.get("destination_options")
            valid_options = (
                isinstance(options, list)
                and len(options) == 1
                and isinstance(options[0], dict)
                and options[0].get("file_format") == "parquet"
            )
            if (
                flow_log.values.get("log_destination_type") != "s3"
                or not valid_options
            ):
                raise CostEstimationError(
                    f"{flow_log.address} is not an S3 Parquet flow log "
                    "covered by the pricing model"
                )

        buckets = {
            resource.address
            for resource in self.by_type.get("aws_s3_bucket", [])
        }
        expected_buckets = {
            'aws_s3_bucket.relay["config"]',
            'aws_s3_bucket.relay["logs"]',
            'aws_s3_bucket.relay["media"]',
        }
        if buckets and buckets != expected_buckets:
            raise CostEstimationError(
                "planned S3 buckets differ from the three Relay buckets "
                "covered by the usage model"
            )

    def request(
        self,
        key: str,
        service: str,
        product_filters: tuple[tuple[str, str], ...],
        units: str | tuple[str, ...],
        description: str,
    ) -> PriceRequest:
        return PriceRequest(
            key, service, product_filters,
            (units,) if isinstance(units, str) else units,
            description,
        )

    def regional(self, **values: str) -> tuple[tuple[str, str], ...]:
        return filters(
            regionCode=self.region, locationType="AWS Region", **values
        )

    def add(
        self, name: str, address: str, resource_type: str, category: str,
        quantity: Any, unit: str, request: PriceRequest, assumption: str,
        price_tier_offset: Any = 0,
    ) -> None:
        amount = number(quantity, f"quantity for {name}")
        if amount == 0:
            return
        quote = self.resolver.quote(request)
        offset = number(price_tier_offset, f"price tier offset for {name}")
        component_cost = quote.cost(amount + offset) - quote.cost(offset)
        self.components.append(
            CostComponent(
                name, address, resource_type, category, str(amount), unit,
                money(component_cost), quote.service_code, quote.sku,
                quote.effective_date,
                [
                    {
                        "begin": str(t.begin),
                        "end": "Infinity" if t.end == INFINITY else str(t.end),
                        "usdPerUnit": str(t.usd),
                        "unit": t.unit,
                        "description": t.description,
                    }
                    for t in quote.tiers
                ],
                assumption,
            )
        )

    @staticmethod
    def required_number(resource: PlannedResource, key: str) -> Decimal:
        if resource.values.get(key) is None:
            raise CostEstimationError(f"{resource.address} is missing planned {key}")
        return number(resource.values[key], f"{resource.address}.{key}")

    def ec2_request(self, instance_type: str) -> PriceRequest:
        return self.request(
            f"ec2:{instance_type}", "AmazonEC2",
            self.regional(
                productFamily="Compute Instance", instanceType=instance_type,
                operatingSystem="Linux", preInstalledSw="NA", tenancy="Shared",
                capacitystatus="Used",
            ),
            "Hrs", r"(?:Linux|On Demand).*Instance Hour|Instance Hour.*Linux",
        )

    def price_compute(self) -> None:
        hours = Decimal(HOURS_PER_MONTH)
        ebs: list[tuple[str, Decimal]] = []
        monitored = Decimal(0)

        root_templates = [
            item
            for item in self.by_type.get("aws_launch_template", [])
            if not item.address.startswith("module.")
        ]
        for instance in self.by_type.get("aws_instance", []):
            instance_type = str(instance.values.get("instance_type") or "")
            inherited_template = None
            if instance.address == "aws_instance.homelab[0]":
                if len(root_templates) != 1:
                    raise CostEstimationError(
                        "cannot pair Home Lab instance and launch template"
                    )
                inherited_template = root_templates[0]
                instance_type = instance_type or str(
                    inherited_template.values.get("instance_type") or ""
                )
            if not instance_type:
                raise CostEstimationError(f"{instance.address} has no instance_type")
            self.add(
                f"EC2 {instance_type}", instance.address, instance.resource_type,
                "fixed", hours, "instance-hours", self.ec2_request(instance_type),
                "One continuously running on-demand Linux instance from the exact plan.",
            )
            monitored += Decimal(instance.values.get("monitoring") is True)
            blocks = instance.values.get("root_block_device")
            if (not isinstance(blocks, list) or not blocks) and inherited_template:
                mappings = inherited_template.values.get("block_device_mappings")
                if isinstance(mappings, list):
                    blocks = []
                    for mapping in mappings:
                        raw = mapping.get("ebs", []) if isinstance(mapping, dict) else []
                        blocks.extend(raw if isinstance(raw, list) else [raw])
            if not isinstance(blocks, list) or not blocks:
                raise CostEstimationError(f"{instance.address} has no priced root volume")
            for block in blocks:
                if not isinstance(block, dict) or block.get("volume_type") != "gp3":
                    raise CostEstimationError(f"{instance.address} uses unsupported EBS storage")
                ebs.append((instance.address, number(block.get("volume_size"), "EBS size")))

        for volume in self.by_type.get("aws_ebs_volume", []):
            if volume.values.get("type") != "gp3":
                raise CostEstimationError(f"{volume.address} uses unsupported EBS storage")
            ebs.append(
                (volume.address, self.required_number(volume, "size"))
            )

        # The EC2 application instance is delegated to an ASG and therefore is
        # absent as aws_instance in the plan. Pair its direct launch template.
        asgs = [r for r in self.by_type.get("aws_autoscaling_group", []) if not r.address.startswith("module.")]
        templates = [r for r in self.by_type.get("aws_launch_template", []) if not r.address.startswith("module.")]
        if asgs:
            if len(asgs) != 1 or len(templates) != 1:
                raise CostEstimationError("cannot pair Relay ASG and launch template")
            asg, template = asgs[0], templates[0]
            count = self.required_number(asg, "desired_capacity")
            instance_type = str(template.values.get("instance_type") or "")
            if not instance_type:
                raise CostEstimationError(f"{template.address} has no instance_type")
            self.add(
                f"EC2 application fleet ({instance_type})", asg.address,
                asg.resource_type, "fixed", count * hours, "instance-hours",
                self.ec2_request(instance_type),
                f"Exact ASG desired capacity ({count}) for {HOURS_PER_MONTH} hours/month.",
            )
            monitored += count
            mappings = template.values.get("block_device_mappings")
            if not isinstance(mappings, list) or not mappings:
                raise CostEstimationError(f"{template.address} has no block device mapping")
            for mapping in mappings:
                blocks = mapping.get("ebs", []) if isinstance(mapping, dict) else []
                if isinstance(blocks, dict):
                    blocks = [blocks]
                for block in blocks:
                    if not isinstance(block, dict) or block.get("volume_type") != "gp3":
                        raise CostEstimationError(f"{template.address} uses unsupported EBS storage")
                    ebs.append((asg.address, count * number(block.get("volume_size"), "EBS size")))

        for group in self.by_type.get("aws_eks_node_group", []):
            types = group.values.get("instance_types")
            scaling = group.values.get("scaling_config")
            if not isinstance(types, list) or len(types) != 1 or not isinstance(scaling, list) or len(scaling) != 1:
                raise CostEstimationError(f"{group.address} has ambiguous node capacity")
            count = number(scaling[0].get("desired_size"), "EKS desired size")
            instance_type = str(types[0])
            self.add(
                f"EKS managed nodes ({instance_type})", group.address,
                group.resource_type, "fixed", count * hours, "instance-hours",
                self.ec2_request(instance_type),
                f"Exact node-group desired size ({count}) for {HOURS_PER_MONTH} hours/month.",
            )
            monitored += count
            disk_size = group.values.get("disk_size")
            if disk_size is not None:
                ebs.append(
                    (group.address, count * number(disk_size, "EKS disk size"))
                )
                continue
            module_prefix = group.address.rsplit(".aws_eks_node_group.", 1)[0]
            node_templates = [
                template
                for template in self.by_type.get("aws_launch_template", [])
                if template.address.startswith(
                    f"{module_prefix}.aws_launch_template."
                )
            ]
            if len(node_templates) != 1:
                raise CostEstimationError(
                    f"cannot pair {group.address} with one EKS launch template"
                )
            template = node_templates[0]
            mappings = template.values.get("block_device_mappings")
            if not isinstance(mappings, list) or not mappings:
                raise CostEstimationError(
                    f"{template.address} has no explicit priced root volume"
                )
            for mapping in mappings:
                blocks = mapping.get("ebs", []) if isinstance(mapping, dict) else []
                if isinstance(blocks, dict):
                    blocks = [blocks]
                for block in blocks:
                    if not isinstance(block, dict) or block.get("volume_type") != "gp3":
                        raise CostEstimationError(
                            f"{template.address} uses unsupported EBS storage"
                        )
                    ebs.append(
                        (
                            group.address,
                            count * number(block.get("volume_size"), "EKS disk size"),
                        )
                    )

        if ebs:
            self.add(
                "EBS gp3 storage", ", ".join(address for address, _ in ebs),
                "aws_ebs_volume (derived)", "fixed",
                sum((size for _, size in ebs), Decimal(0)), "GB-month",
                self.request(
                    "ebs:gp3", "AmazonEC2",
                    self.regional(productFamily="Storage", volumeApiName="gp3"),
                    "GB-Mo", r"(?:General Purpose|gp3).*storage|storage.*gp3",
                ),
                "Exact root-volume sizes multiplied by exact fleet desired capacity; gp3 baseline IOPS and throughput are included.",
            )
        if monitored:
            self.add(
                "EC2 detailed-monitoring metrics", "derived from monitored EC2",
                "aws_cloudwatch_metric", "usage", monitored * 7, "metric-months",
                self.request(
                    "cloudwatch:metrics", "AmazonCloudWatch",
                    self.regional(
                        productFamily="Metric", group="Metric",
                        groupDescription="CloudWatch Custom Metrics",
                    ),
                    ("Metrics", "Metric"), r"metric-month",
                ),
                "Seven detailed-monitoring metrics per planned EC2 instance; no account free tier deducted.",
            )

    def price_network(self) -> None:
        hours = Decimal(HOURS_PER_MONTH)
        nat_count = Decimal(len(self.by_type.get("aws_nat_gateway", [])))
        if nat_count:
            nat_filters = self.regional(
                productFamily="NAT Gateway",
                group="NGW:NatGateway",
                operation="NatGateway",
            )
            self.add(
                "NAT gateways", "aws_nat_gateway[*]", "aws_nat_gateway", "fixed",
                nat_count * hours, "gateway-hours",
                self.request(
                    "ec2:nat-hours", "AmazonEC2",
                    tuple(sorted((*nat_filters, (
                        "groupDescription", "Hourly charge for NAT Gateways",
                    )))),
                    "Hrs", r"NAT Gateway.*hour|hour.*NAT Gateway",
                ),
                f"All {nat_count} exact planned NAT gateways run for {HOURS_PER_MONTH} hours/month.",
            )
            self.add(
                "NAT data processing", "aws_nat_gateway[*]", "aws_nat_gateway", "usage",
                number(self.model["nat_data_processed_per_gateway_gb"], "NAT GB") * nat_count, "GB",
                self.request(
                    "ec2:nat-data", "AmazonEC2",
                    tuple(sorted((*nat_filters, (
                        "groupDescription",
                        "Charge for per GB data processed by NatGateways",
                    )))),
                    "GB", r"NAT Gateway.*(?:GB|data)|(?:GB|data).*NAT Gateway",
                ),
                "Modeled external control/notification traffic; media uses the free S3 gateway endpoint.",
            )

        load_balancers = self.by_type.get("aws_lb", [])
        for load_balancer in load_balancers:
            if load_balancer.values.get("load_balancer_type") != "application":
                raise CostEstimationError(f"unsupported load balancer {load_balancer.address}")
            alb_filters = self.regional(
                productFamily="Load Balancer-Application",
                operation="LoadBalancing:Application",
            )
            self.add(
                "Application Load Balancer", load_balancer.address,
                load_balancer.resource_type, "fixed", hours, "load-balancer-hours",
                self.request(
                    "elb:alb-hours", "AWSELB",
                    tuple(sorted((*alb_filters, (
                        "groupDescription",
                        "LoadBalancer hourly usage by Application Load Balancer",
                    )))),
                    "Hrs", r"Application Load ?Balancer-hour",
                ),
                f"Exact planned ALB for {HOURS_PER_MONTH} hours/month.",
            )
            hourly_lcus = max(
                Decimal(self.model["new_connections_per_second"]) / 25,
                Decimal(self.model["modeled_active_connections"]) / 3000,
                number(self.model["alb_processed_gb_per_hour"], "ALB GB/hour"),
                Decimal(self.model["rule_evaluations_per_second"]) / 1000,
            )
            self.add(
                "Application Load Balancer capacity", load_balancer.address,
                load_balancer.resource_type, "usage", hourly_lcus * hours, "LCU-hours",
                self.request(
                    "elb:alb-lcu", "AWSELB",
                    tuple(sorted((*alb_filters, (
                        "groupDescription",
                        "Used Application Load Balancer capacity units-hr",
                    )))),
                    ("LCU-Hrs", "LCU-hours"),
                    r"used Application load balancer capacity",
                ),
                "AWS LCU maximum across modeled new/active connections, bytes, and billable rule evaluations.",
            )

        # Explicit EIPs cover NAT and TURN. A public ALB additionally consumes
        # one billed public IPv4 address per Availability Zone.
        public_ips = Decimal(len(self.by_type.get("aws_eip", [])))
        public_ips += sum(
            Decimal(self.model["availability_zones"])
            for item in load_balancers if item.values.get("internal") is False
        )
        if public_ips:
            self.add(
                "In-use public IPv4 addresses", "planned EIPs and public ALB nodes",
                "aws_eip/aws_lb", "fixed", public_ips * hours, "IP-hours",
                self.request(
                    "vpc:public-ipv4", "AmazonVPC",
                    self.regional(
                        group="VPCPublicIPv4Address",
                        groupDescription="Hourly charge for In-use Public IPv4 Addresses",
                    ), "Hrs",
                    r"(?:in-use|public).*IPv4|IPv4.*(?:in-use|public)",
                ),
                "Every planned EIP plus one public ALB address per exact capacity-profile Availability Zone.",
            )

        self.add(
            "Cross-AZ regional data transfer", "modeled Relay regional traffic",
            "aws_data_transfer", "usage",
            number(self.model["regional_data_transfer_gb"], "regional transfer") * 2, "GB",
            self.request(
                "ec2:regional-transfer", "AmazonEC2",
                filters(
                    productFamily="Data Transfer", transferType="IntraRegion",
                    fromRegionCode=self.region, toRegionCode=self.region,
                    fromLocationType="AWS Region", toLocationType="AWS Region",
                ),
                "GB", r"regional|Availability Zone|intra-region",
            ),
            "The model's one-way cross-AZ volume is billed in both directions.",
        )
        self.add(
            "Internet data transfer out", "modeled Relay/media/TURN traffic",
            "aws_data_transfer", "usage", self.model["internet_data_transfer_gb"], "GB",
            self.request(
                "data-transfer:internet-out", "AWSDataTransfer",
                filters(
                    productFamily="Data Transfer", transferType="AWS Outbound",
                    fromRegionCode=self.region, fromLocationType="AWS Region",
                    toLocation="External", toLocationType="Other",
                ),
                "GB", r"data transfer out|out.*(?:Internet|global)",
            ),
            "Recipient messages, media reads, and TURN outbound traffic; no account free tier deducted.",
        )

    def price_database(self) -> Decimal:
        hours = Decimal(HOURS_PER_MONTH)
        database_storage = Decimal(0)
        for database in self.by_type.get("aws_db_instance", []):
            instance_class = str(database.values.get("instance_class") or "")
            deployment = "Multi-AZ" if database.values.get("multi_az") is True else "Single-AZ"
            self.add(
                f"RDS PostgreSQL {instance_class} ({deployment})", database.address,
                database.resource_type, "fixed", hours, "instance-hours",
                self.request(
                    f"rds:postgres:{instance_class}:{deployment}", "AmazonRDS",
                    self.regional(
                        productFamily="Database Instance", databaseEngine="PostgreSQL",
                        instanceType=instance_class, deploymentOption=deployment,
                        licenseModel="No license required",
                    ),
                    "Hrs", r"PostgreSQL.*(?:Instance|hour)|(?:Instance|hour).*PostgreSQL",
                ),
                f"Exact planned {deployment} instance for {HOURS_PER_MONTH} hours/month.",
            )
            storage = self.required_number(database, "allocated_storage")
            database_storage += storage
            if database.values.get("storage_type") != "gp3":
                raise CostEstimationError(f"unsupported RDS storage in {database.address}")
            self.add(
                "RDS PostgreSQL gp3 storage", database.address, database.resource_type,
                "fixed", storage, "GB-month",
                self.request(
                    "rds:postgres:gp3", "AmazonRDS",
                    self.regional(
                        productFamily="Database Storage", databaseEngine="PostgreSQL",
                        volumeType="General Purpose-GP3", deploymentOption=deployment,
                    ),
                    "GB-Mo", r"(?:gp3|General Purpose).*storage|storage.*(?:gp3|General Purpose)",
                ),
                "Exact initial allocation; autoscaling growth beyond the plan is excluded explicitly.",
            )

        clusters = self.by_type.get("aws_rds_cluster", [])
        cluster_instances = self.by_type.get("aws_rds_cluster_instance", [])
        if clusters:
            if len(clusters) != 1 or not cluster_instances:
                raise CostEstimationError("Aurora cluster capacity is incomplete")
            for instance in cluster_instances:
                instance_class = str(instance.values.get("instance_class") or "")
                self.add(
                    f"Aurora PostgreSQL {instance_class}", instance.address,
                    instance.resource_type, "fixed", hours, "instance-hours",
                    self.request(
                        f"rds:aurora:{instance_class}", "AmazonRDS",
                        self.regional(
                            productFamily="Database Instance", databaseEngine="Aurora PostgreSQL",
                            instanceType=instance_class, deploymentOption="Single-AZ",
                            licenseModel="No license required", storage="EBS Only",
                        ),
                        "Hrs", r"Aurora PostgreSQL.*(?:Instance|hour)|(?:Instance|hour).*Aurora PostgreSQL",
                    ),
                    f"Each exact planned Aurora instance for {HOURS_PER_MONTH} hours/month.",
                )
            database_storage = number(self.model["retained_database_storage_gb"], "Aurora storage")
            storage_filters = self.regional(
                productFamily="Database Storage", databaseEngine="Aurora PostgreSQL",
                volumeType="General Purpose-Aurora", deploymentOption="Single-AZ",
            )
            self.add(
                "Aurora PostgreSQL storage", clusters[0].address, clusters[0].resource_type,
                "usage", database_storage, "GB-month",
                self.request("rds:aurora-storage", "AmazonRDS", storage_filters, "GB-Mo", r"Aurora.*storage|storage.*Aurora"),
                "365-day retained message/index footprint from the versioned capacity model.",
            )
            self.add(
                "Aurora PostgreSQL I/O", clusters[0].address, clusters[0].resource_type,
                "usage", Decimal(self.model["monthly_messages"]) * 2, "I/O requests",
                self.request(
                    "rds:aurora-io", "AmazonRDS",
                    self.regional(
                        productFamily="System Operation",
                        databaseEngine="Aurora PostgreSQL",
                        group="Aurora I/O Operation",
                        usagetype="Aurora:StorageIOUsage",
                    ),
                    ("IOs", "I/O requests", "Requests"), r"I/O|IO request",
                ),
                "One modeled read and one modeled write I/O per message.",
            )
        return database_storage

    def price_platform_services(self, database_storage: Decimal) -> None:
        hours = Decimal(HOURS_PER_MONTH)

        for cluster in self.by_type.get("aws_eks_cluster", []):
            version = str(cluster.values.get("version") or "")
            if not version:
                raise CostEstimationError(
                    f"{cluster.address} has no planned Kubernetes version"
                )
            if self.eks_support_resolver is None:
                raise CostEstimationError(
                    "an EKS support resolver is required for an EKS plan"
                )
            support_status = self.eks_support_resolver.status(self.region, version)
            extended = support_status == "EXTENDED_SUPPORT"
            self.add(
                "Amazon EKS control plane", cluster.address, cluster.resource_type,
                "fixed", hours, "cluster-hours",
                self.request(
                    "eks:cluster:base", "AmazonEKS",
                    self.regional(
                        productFamily="Compute", operation="CreateOperation",
                        tiertype="HAStandard",
                    ),
                    ("Hours", "Hrs"), r"(?:EKS|Kubernetes).*cluster",
                ),
                f"Base control-plane charge for exact planned Kubernetes {version}; AWS currently reports {support_status}.",
            )
            if extended:
                self.add(
                    "Amazon EKS extended-support surcharge", cluster.address,
                    cluster.resource_type, "fixed", hours, "cluster-hours",
                    self.request(
                        "eks:cluster:extended-surcharge", "AmazonEKS",
                        self.regional(
                            productFamily="Compute", operation="ExtendedSupport",
                            tiertype="HAExtended",
                        ),
                        ("Hours", "Hrs"), r"extended support",
                    ),
                    f"AWS reports Kubernetes {version} in EXTENDED_SUPPORT; this surcharge is additive to the base control-plane charge.",
                )

        key_count = Decimal(len(self.by_type.get("aws_kms_key", [])))
        if key_count:
            self.add(
                "Customer-managed KMS keys", "aws_kms_key[*]", "aws_kms_key",
                "fixed", key_count, "key-months",
                self.request(
                    "kms:keys", "awskms", self.regional(productFamily="Encryption Key"),
                    ("Keys", "Key"), r"customer managed.*key|KMS key",
                ),
                "Every exact planned customer-managed KMS key; no free allowance is deducted.",
            )
            kms_calls = Decimal(self.model["kms_api_requests"])
            self.add(
                "KMS API requests", "encrypted Relay services", "aws_kms_key",
                "usage", kms_calls, "requests",
                self.request(
                    "kms:requests", "awskms",
                    self.regional(productFamily="API Request", group="awskms-APIRequest-All"),
                    ("Requests", "API Calls"), r"request",
                ),
                "10,000 monthly envelope-key operations plus 1% of media writes; S3 Bucket Keys are enabled and database row I/O does not invoke KMS per query.",
            )

        # RDS creates one managed master-user secret outside Terraform's
        # explicit aws_secretsmanager_secret resources for each DB/cluster.
        secret_count = Decimal(len(self.by_type.get("aws_secretsmanager_secret", [])))
        secret_count += Decimal(
            len(self.by_type.get("aws_db_instance", []))
            + len(self.by_type.get("aws_rds_cluster", []))
        )
        if secret_count:
            self.add(
                "Secrets Manager secrets", "planned and RDS-managed secrets",
                "aws_secretsmanager_secret", "fixed", secret_count, "secret-months",
                self.request(
                    "secrets:stored", "AWSSecretsManager",
                    self.regional(
                        productFamily="Secret", group="AWSSecretsManager-Secret"
                    ),
                    ("Secrets", "Secret"), r"secret",
                ),
                "Explicit secret containers plus one AWS-managed master-user secret for each exact planned database.",
            )
            secret_calls = Decimal(max(1000, int(self.model["daily_active_users"] * 30)))
            self.add(
                "Secrets Manager API calls", "modeled secret reads",
                "aws_secretsmanager_secret", "usage", secret_calls, "API calls",
                self.request(
                    "secrets:api", "AWSSecretsManager",
                    self.regional(
                        productFamily="API Request",
                        group="AWSSecretsManager-APIRequest",
                    ),
                    ("API Calls", "API Requests", "Requests"), r"API call|request",
                ),
                "At least 1,000 calls/month, scaling to one read per daily-active user-day.",
            )

        health_checks = Decimal(len(self.by_type.get("aws_route53_health_check", [])))
        if health_checks:
            self.add(
                "Route 53 basic health checks", "aws_route53_health_check[*]",
                "aws_route53_health_check", "fixed", health_checks,
                "health-check-months",
                self.request(
                    "route53:health", "AmazonRoute53",
                    filters(
                        productFamily="DNS Health Check", resourceEndpoint="AWS",
                        group="Route53-Basic",
                    ),
                    ("Mo", "HealthChecks", "Health Check"), r"Health Check|health check",
                ),
                "Every exact planned TURN TCP health check at the post-free-tier AWS-endpoint rate; the account-wide first-50 allowance is conservatively treated as already consumed.",
                price_tier_offset=50,
            )
            self.add(
                "Route 53 standard TURN queries", "aws_route53_record.turn[*]",
                "aws_route53_record", "usage",
                health_checks * Decimal(self.model["route53_queries_per_turn_record"]),
                "queries",
                self.request(
                    "route53:queries", "AmazonRoute53",
                    filters(
                        productFamily="DNS Query", routingType="Standard",
                        routingTarget="External",
                    ),
                    ("Queries", "Query"), r"quer",
                ),
                "Only TURN records receive modeled queries; ALB aliases and ACM validation records are query-free.",
            )

        alarms = Decimal(len(self.by_type.get("aws_cloudwatch_metric_alarm", [])))
        if alarms:
            self.add(
                "CloudWatch standard metric alarms", "aws_cloudwatch_metric_alarm[*]",
                "aws_cloudwatch_metric_alarm", "fixed", alarms,
                "alarm-metrics/month",
                self.request(
                    "cloudwatch:alarms", "AmazonCloudWatch",
                    self.regional(productFamily="Alarm", group="Alarm", alarmType="Standard"),
                    ("Alarms", "Alarm"), r"standard.*alarm|alarm",
                ),
                "One standard-resolution metric for every exact planned alarm.",
            )

        log_groups = self.by_type.get("aws_cloudwatch_log_group", [])
        if log_groups:
            ingest = Decimal(
                self.model["app_log_ingest_gb"] + self.model["waf_log_ingest_gb"]
            )
            stored = Decimal(
                self.model["app_log_storage_gb"] + self.model["waf_log_storage_gb"]
            )
            if any("codebuild" in item.address for item in log_groups):
                ingest += Decimal("0.25")
                stored += Decimal("0.25")
            self.add(
                "CloudWatch Logs ingestion", "aws_cloudwatch_log_group[*]",
                "aws_cloudwatch_log_group", "usage", ingest, "GB",
                self.request(
                    "cloudwatch:logs-ingest", "AmazonCloudWatch",
                    filters(
                        regionCode=self.region, group="Ingested Logs",
                        groupDescription="Existing system, application, and custom log files",
                        operation="PutLogEvents", version="Current",
                    ),
                    "GB", r"ingest",
                ),
                "Versioned application/filtered-WAF model plus 0.25 GB EKS bootstrap logs when present.",
            )
            self.add(
                "CloudWatch Logs retained storage", "aws_cloudwatch_log_group[*]",
                "aws_cloudwatch_log_group", "usage", stored, "GB-month",
                self.request(
                    "cloudwatch:logs-storage", "AmazonCloudWatch",
                    filters(
                        regionCode=self.region,
                        productFamily="Storage Snapshot",
                        group="Amazon CloudWatch Standard Storage pricing current",
                        version="Current",
                    ),
                    "GB-Mo", r"storage|archiv",
                ),
                "Modeled volume multiplied by the capacity profile's exact Terraform retention window.",
            )

        if self.by_type.get("aws_flow_log"):
            archive_ingest = Decimal(self.model["archive_log_ingest_gb"])
            self.add(
                "VPC Flow Logs delivery to S3", "aws_flow_log[*]",
                "aws_flow_log", "usage", archive_ingest, "GB",
                self.request(
                    "cloudwatch:vended-logs-s3", "AmazonCloudWatch",
                    self.regional(
                        productFamily="Data Payload", group="Delivered Logs",
                        groupDescription=(
                            "Log delivered from AWS services to log destinations "
                            "other than CloudWatch"
                        ),
                        logsDestination="Amazon S3", version="Current",
                    ),
                    "GB", r"log data delivered to S3",
                ),
                "One KiB per modeled edge request plus 10% envelope overhead; no account free tier deducted.",
            )
            self.add(
                "VPC Flow Logs Parquet conversion", "aws_flow_log[*]",
                "aws_flow_log", "usage", archive_ingest, "GB",
                self.request(
                    "cloudwatch:vended-logs-parquet", "AmazonCloudWatch",
                    self.regional(
                        productFamily="Data Payload", group="Converted Logs",
                        groupDescription=(
                            "Log delivered from AWS services to log destinations "
                            "other than CloudWatch"
                        ),
                        operation="ParquetConversion",
                        logsDestination="Amazon S3", version="Current",
                    ),
                    "GB", r"converted to Parquet",
                ),
                "The exact VPC flow-log resource requests Parquet output, charged on input bytes.",
            )

        for waf in self.by_type.get("aws_wafv2_web_acl", []):
            rules = waf.values.get("rule")
            if not isinstance(rules, list):
                raise CostEstimationError(f"{waf.address} has no planned rules")
            waf_filters = self.regional(productFamily="Web Application Firewall")
            self.add(
                "AWS WAF web ACL", waf.address, waf.resource_type, "fixed", 1,
                "web-ACL-months",
                self.request(
                    "waf:acl", "awswaf",
                    tuple(sorted((*waf_filters, ("group", "Web ACL"), ("groupDescription", "Web ACL Activated")))),
                    ("Month", "month"), r"web ACL created",
                ),
                "One exact planned regional web ACL.",
            )
            self.add(
                "AWS WAF rules", waf.address, waf.resource_type, "fixed",
                len(rules), "rule-months",
                self.request(
                    "waf:rules", "awswaf",
                    tuple(sorted((*waf_filters, ("group", "Rule"), ("groupDescription", "Rule Activated")))),
                    ("Month", "month"), r"rule created",
                ),
                "Every exact top-level rule; the selected AWS-managed rule groups have no marketplace subscription.",
            )
            self.add(
                "AWS WAF requests", waf.address, waf.resource_type, "usage",
                self.model["waf_monthly_requests"], "requests",
                self.request(
                    "waf:requests", "awswaf",
                    tuple(sorted((*waf_filters, ("group", "Request"), ("groupDescription", "Request Processed Tier0")))),
                    ("Requests", "Request"), r"Request Processed Tier0",
                ),
                "Modeled WebSocket handshakes, control calls, and media HTTP requests.",
            )

        if self.by_type.get("aws_s3_bucket"):
            storage = Decimal(
                self.model["retained_media_storage_gb"]
                + self.model["archive_log_storage_gb"]
                + 1
            )
            tier1 = Decimal(
                self.model["monthly_media_uploads"]
                + self.model["archive_log_put_requests"]
                + self.model["backup_s3_list_requests"]
                + 100
            )
            tier2 = Decimal(
                self.model["monthly_media_uploads"] * 3
                + self.model["backup_s3_get_requests"]
                + 100
            )
            self.add(
                "S3 Standard storage", "aws_s3_bucket.relay[*]", "aws_s3_bucket",
                "usage", storage, "GB-month",
                self.request(
                    "s3:standard", "AmazonS3",
                    self.regional(productFamily="Storage", storageClass="General Purpose", volumeType="Standard"),
                    "GB-Mo", r"storage",
                ),
                "Retained media and archive logs plus 1 GB configuration; plan classification covers every exact bucket.",
            )
            self.add(
                "S3 PUT/COPY/POST/LIST requests", "aws_s3_bucket.relay[*]",
                "aws_s3_bucket", "usage", tier1, "requests",
                self.request(
                    "s3:tier1", "AmazonS3",
                    self.regional(
                        productFamily="API Request", group="S3-API-Tier1",
                        groupDescription="PUT/COPY/POST or LIST requests",
                    ),
                    "Requests", r"per 1,000 PUT, COPY, POST, or LIST requests$",
                ),
                "Modeled media/log writes, daily AWS Backup LIST scans at one request per 500 retained objects, plus 100 configuration/list operations.",
            )
            self.add(
                "S3 GET and other requests", "aws_s3_bucket.relay[*]",
                "aws_s3_bucket", "usage", tier2, "requests",
                self.request(
                    "s3:tier2", "AmazonS3",
                    self.regional(
                        productFamily="API Request", group="S3-API-Tier2",
                        groupDescription="GET and all other requests",
                    ),
                    "Requests", r"per 10,000 GET and all other requests$",
                ),
                "Three media reads per upload, the AWS Backup pricing example's conservative eight GETs per newly protected object, plus 100 configuration reads.",
            )

        repositories = Decimal(len(self.by_type.get("aws_ecr_repository", [])))
        if repositories:
            self.add(
                "ECR image storage", "aws_ecr_repository[*]", "aws_ecr_repository",
                "usage", repositories * 2, "GB-month",
                self.request(
                    "ecr:storage", "AmazonECR",
                    self.regional(
                        productFamily="EC2 Container Registry", storageType="S3"
                    ), "GB-Mo", r"data storage",
                ),
                "Conservative 2 GB retained image data per exact repository; same-region pulls add no transfer charge.",
            )

        projects = Decimal(len(self.by_type.get("aws_codebuild_project", [])))
        if projects:
            self.add(
                "CodeBuild EKS reconciliation", "aws_codebuild_project[*]",
                "aws_codebuild_project", "usage", projects * 60, "build-minutes",
                self.request(
                    "codebuild:small", "CodeBuild",
                    self.regional(
                        productFamily="Compute", computeFamily="OnDemand-EC2",
                        computeType="general1.small", operatingSystem="Linux",
                        architecture="x86-64",
                    ),
                    ("minutes", "Minutes"), r"build.*minute|minute",
                ),
                "One 60-minute bootstrap/reconciliation allowance per planned project each month; actual release frequency varies.",
            )

        if self.by_type.get("aws_backup_plan") or self.by_type.get("aws_backup_vault"):
            if self.home_lab:
                self.add(
                    "Home Lab encrypted node backup",
                    "aws_backup_selection.relay",
                    "aws_backup_plan",
                    "usage",
                    Decimal(32),
                    "GB-month",
                    self.request(
                        "backup:homelab-ebs",
                        "AmazonEC2",
                        self.regional(
                            productFamily="Storage Snapshot",
                            storageMedia="Amazon S3",
                        ),
                        ("GB-Mo", "GB-month"),
                        r"per GB-Month of snapshot data stored(?:\s*-\s*.+)?$",
                    ),
                    "One conservative full 32-GiB snapshot covering the 12-GiB root and 20-GiB persistent data volumes; incremental savings are not claimed.",
                )
            else:
                self.add(
                    "AWS Backup warm S3 storage", "aws_backup_plan.relay",
                    "aws_backup_plan", "usage",
                    Decimal(self.model["retained_media_storage_gb"] + 1), "GB-month",
                    self.request(
                        "backup:s3", "AWSBackup",
                        self.regional(
                            productFamily="AWS Backup Storage", operation="Storage",
                            backup_service="S3", storagetype="Warm",
                            vaulttype="BackupVault",
                        ), "GB-month", r"for warm backup storage for S3$",
                    ),
                    "One warm retained copy of modeled media plus 1 GB configuration; no incremental/deduplication savings claimed.",
                )
                self.add(
                    "EventBridge events for S3 backup", "aws_backup_plan.relay",
                    "aws_backup_plan", "usage",
                    Decimal(self.model["backup_eventbridge_events"]), "64-KiB events",
                    self.request(
                        "eventbridge:s3-backup-events", "AWSEvents",
                        self.regional(
                            productFamily="EventBridge", operation="PutEvents",
                            eventType="Custom Event",
                        ),
                        "64K-Chunks", r"EventBridge custom events received",
                    ),
                    "One billable S3 opt-in data event per modeled new media object plus 100 configuration mutations; every event is conservatively one 64-KiB chunk.",
                )
            if database_storage and not self.home_lab:
                aurora = bool(self.by_type.get("aws_rds_cluster"))
                self.add(
                    "AWS Backup warm database storage", "aws_backup_plan.relay",
                    "aws_backup_plan", "usage", database_storage, "GB-month",
                    self.request(
                        "backup:database", "AmazonRDS",
                        self.regional(
                            productFamily="Storage Snapshot",
                            databaseEngine=(
                                "Aurora PostgreSQL"
                                if aurora
                                else "PostgreSQL"
                            ),
                            deploymentOption="Single-AZ",
                            storageMedia="AmazonS3",
                        ),
                        ("GB-Mo", "GB-month"),
                        (
                            r"backup storage exceeding free allocation for "
                            r"Aurora PostgreSQL$"
                            if aurora
                            else r"backup storage exceeding free allocation "
                            r"running PostgreSQL$"
                        ),
                    ),
                    "One full warm database copy; native automated-backup allowances are not deducted.",
                )

    def estimate(self) -> list[CostComponent]:
        self.price_compute()
        self.price_network()
        database_storage = self.price_database()
        self.price_platform_services(database_storage)
        if not self.components:
            raise CostEstimationError("exact plan produced no priced components")
        return sorted(
            self.components,
            key=lambda item: (item.category, item.name, item.resource_address),
        )


def build_report(
    plan_path: Path,
    binary_plan_sha256: str,
    customer: dict[str, Any],
    estimator: Estimator,
    components: Sequence[CostComponent],
) -> dict[str, Any]:
    fixed = sum(
        (number(item.monthly_cost, "component cost") for item in components if item.category == "fixed"),
        Decimal(0),
    )
    usage = sum(
        (number(item.monthly_cost, "component cost") for item in components if item.category == "usage"),
        Decimal(0),
    )
    total = fixed + usage
    if customer.get("home_lab", False) is True and total > Decimal("40"):
        raise CostEstimationError(
            f"Home Lab exact-plan estimate ${money(total)}/month exceeds the $40 safety ceiling"
        )
    model = estimator.model
    return {
        "schemaVersion": 1,
        "currency": "USD",
        "priceSource": {
            "provider": "AWS Price List Query API",
            "endpointRegion": PRICE_API_REGION,
            "queriedAt": datetime.now(UTC).isoformat(),
            "authentication": "customer AWS CLI session",
            "planSha256": binary_plan_sha256,
            "planJsonSha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
        "customer": {
            "slug": customer["customer_slug"],
            "region": customer["aws_region"],
            "capacityProfile": customer["expected_users"],
            "homeLab": customer.get("home_lab", False) is True,
        },
        "summary": {
            "monthlyFixed": money(fixed),
            "monthlyUsage": money(usage),
            "monthlyTotal": money(total),
            "annualTotal": money(total * 12),
            "pricedComponents": len(components),
            "unpricedResources": 0,
        },
        "usageModel": {
            "name": model["model_version"],
            "dailyActiveUsers": model["daily_active_users"],
            "monthlyMessages": model["monthly_messages"],
            "monthlyMediaUploads": model["monthly_media_uploads"],
            "monthlyBackupS3GetRequests": model["backup_s3_get_requests"],
            "monthlyBackupS3ListRequests": model["backup_s3_list_requests"],
            "monthlyBackupEventBridgeEvents": model["backup_eventbridge_events"],
            "monthlyKmsApiRequests": model["kms_api_requests"],
            "retainedMediaStorageGB": model["retained_media_storage_gb"],
            "retainedDatabaseStorageGB": model["retained_database_storage_gb"],
            "monthlyEdgeRequests": model["monthly_edge_requests"],
            "monthlyInternetTransferGB": round(model["internet_data_transfer_gb"], 3),
            "monthlyRegionalTransferGBBilledBothDirections": round(
                model["regional_data_transfer_gb"] * 2, 3
            ),
            "monthlyNATProcessedGB": round(
                model["nat_data_processed_per_gateway_gb"] * model["nat_gateways"], 3
            ),
        },
        "planCoverage": {
            "managedResourceCount": len(estimator.resources),
            "managedResourceTypes": sorted(estimator.by_type),
            "classification": "fail-closed",
        },
        "components": [asdict(item) for item in components],
        "exclusions": [
            "Taxes, AWS Support, negotiated/private pricing, Savings Plans, Reserved Instances, and account-wide free tiers",
            "Unexpected or incident traffic, restores, and storage growth beyond the displayed assumptions",
            "Usage-priced account-bootstrap controls (AWS Config, GuardDuty, Security Hub, and CloudTrail data events)",
            "Cross-account central logging and security services absent from this customer Terraform plan",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    customer = report["customer"]
    usage = report["usageModel"]
    detail_rows = "\n".join(
        f"| {item['category']} | {item['name']} | {item['quantity']} {item['unit']} | "
        f"${item['monthly_cost']} | `{item['sku']}` |"
        for item in report["components"]
    )
    exclusions = "\n".join(f"  - {item}" for item in report["exclusions"])
    return f"""## Relay AWS infrastructure estimate

| Item | Estimate |
|---|---:|
| Fixed monthly | ${summary['monthlyFixed']} |
| Usage-dependent monthly | ${summary['monthlyUsage']} |
| Estimated monthly total | ${summary['monthlyTotal']} |
| Estimated annual total | ${summary['annualTotal']} |

- Customer: `{customer['slug']}`
- AWS workload region: `{customer['region']}`
- Capacity profile: `{customer['capacityProfile']}` registered users
- Deployment profile: `{'Home Lab (single node, not highly available)' if customer['homeLab'] else 'managed production'}`
- Exact saved binary Terraform plan SHA-256: `{report['priceSource']['planSha256']}`
- Derived plan JSON SHA-256: `{report['priceSource']['planJsonSha256']}`
- Prices queried live from the official AWS Price List Query API in `{report['priceSource']['endpointRegion']}` at `{report['priceSource']['queriedAt']}`. Every product query filters the workload region where the service is regional.
- Coverage is fail-closed: all {report['planCoverage']['managedResourceCount']} managed resources were classified and every non-zero component received one unambiguous current AWS SKU. An unsupported or unpriced billable resource stops the workflow before approval.

### Detailed monthly components

| Kind | Component | Quantity | Monthly | AWS SKU |
|---|---|---:|---:|---|
{detail_rows}

### Base usage assumptions

- `{usage['name']}`: {usage['dailyActiveUsers']:,} DAU, {usage['monthlyMessages']:,} messages/month, and {usage['monthlyMediaUploads']:,} media uploads/month.
- {usage['retainedMediaStorageGB']:,} GiB retained media and {usage['retainedDatabaseStorageGB']:,} GiB retained message/index data.
- Backup overhead: {usage['monthlyBackupS3GetRequests']:,} S3 GETs, {usage['monthlyBackupS3ListRequests']:,} S3 LISTs, and {usage['monthlyBackupEventBridgeEvents']:,} EventBridge events/month; KMS is modeled at {usage['monthlyKmsApiRequests']:,} requests/month with S3 Bucket Keys enabled.
- {usage['monthlyInternetTransferGB']:,.3f} GiB/month internet egress, {usage['monthlyRegionalTransferGBBilledBothDirections']:,.3f} GiB/month billed cross-AZ transfer, and {usage['monthlyNATProcessedGB']:,.3f} GiB/month NAT processing.
- The attached `aws-cost-detail.json` retains every component assumption, official AWS description, SKU, effective date, tier, unit, quantity, and exact plan digest.
- Excluded:
{exclusions}

This is a reviewable base estimate, not a quote or guaranteed AWS bill. Review the detailed JSON and exact plan before posting the digest-bound approval command.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--customer", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--json", dest="json_output", type=Path, required=True)
    args = parser.parse_args()

    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CostEstimationError("--plan is not valid JSON") from exc
    if not isinstance(plan, dict) or not isinstance(plan.get("planned_values"), dict):
        raise CostEstimationError(
            "--plan must be output from `terraform show -json <saved-plan>`"
        )
    binary_plan_sha256 = args.plan_sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", binary_plan_sha256):
        raise CostEstimationError(
            "--plan-sha256 must be the 64-character SHA-256 of the saved binary plan"
        )
    customer = load_yaml(args.customer)
    estimator = Estimator(
        plan,
        customer,
        AwsCliPriceResolver(),
        AwsCliEksSupportResolver(),
    )
    components = estimator.estimate()
    report = build_report(
        args.plan, binary_plan_sha256, customer, estimator, components
    )
    args.json_output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
