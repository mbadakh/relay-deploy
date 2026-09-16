module "vpc" {
  # v5.21.0, pinned to the immutable upstream Git commit.
  source = "git::https://github.com/terraform-aws-modules/terraform-aws-vpc.git?ref=7c1f791efd61f326ed6102d564d1a65d1eceedf0"

  name = local.name
  cidr = var.vpc_cidr
  azs  = local.azs

  public_subnets   = local.public_subnets
  private_subnets  = local.private_subnets
  database_subnets = local.database_subnets

  enable_nat_gateway     = !local.is_home_lab
  single_nat_gateway     = !local.profile.nat_per_az
  one_nat_gateway_per_az = local.profile.nat_per_az
  enable_dns_hostnames   = true
  enable_dns_support     = true

  create_database_subnet_group       = false
  create_database_subnet_route_table = true

  enable_flow_log                   = !local.is_home_lab
  flow_log_destination_type         = "s3"
  flow_log_destination_arn          = "${aws_s3_bucket.relay["logs"].arn}/vpc"
  flow_log_file_format              = "parquet"
  flow_log_per_hour_partition       = true
  flow_log_max_aggregation_interval = 60

  public_subnet_tags = local.compute_mode == "eks" ? {
    "kubernetes.io/role/elb" = "1"
  } : {}
  private_subnet_tags = local.compute_mode == "eks" ? {
    "kubernetes.io/role/internal-elb" = "1"
  } : {}

  tags = local.common_tags

  depends_on = [aws_s3_bucket_policy.logs]
}

# S3 gateway endpoints have no hourly or data-processing charge. Keeping media
# traffic on the AWS network also avoids charging every proxied object byte at
# the NAT gateway; workload IAM and bucket policies remain the access boundary.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = local.is_home_lab ? module.vpc.public_route_table_ids : module.vpc.private_route_table_ids

  tags = merge(local.common_tags, { Name = "${local.name}-s3" })
}
