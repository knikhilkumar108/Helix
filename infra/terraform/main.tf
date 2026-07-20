# Terraform skeleton for the Automata platform's cloud baseline.
# Real deployments wire up VPC, RDS, ElastiCache, S3, EKS, etc.
# This file is the *shape*; concrete resources depend on the chosen cloud.

terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "cluster_name" {
  type    = string
  default = "automata-prod"
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

# ----------------- VPC -----------------
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags = { Name = "${var.cluster_name}-vpc" }
}

resource "aws_subnet" "private" {
  count             = 3
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags = { Name = "${var.cluster_name}-private-${count.index}" }
}

data "aws_availability_zones" "available" {
  state = "available"
}

# ----------------- EKS -----------------
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.20"

  cluster_name    = var.cluster_name
  cluster_version = "1.30"
  vpc_id          = aws_vpc.main.id
  subnet_ids      = aws_subnet.private[*].id

  cluster_endpoint_public_access  = true
  cluster_endpoint_private_access = true

  eks_managed_node_groups = {
    core = {
      instance_types = ["m6i.xlarge"]
      min_size       = 3
      max_size       = 30
      desired_size   = 6
    }
  }

  tags = { "k8s.io/cluster-autoscaler/enabled" = "true" }
}

# ----------------- RDS (Postgres) -----------------
resource "aws_db_subnet_group" "main" {
  name       = "${var.cluster_name}-db"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_security_group" "rds" {
  name   = "${var.cluster_name}-rds-sg"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_instance" "postgres" {
  identifier              = "${var.cluster_name}-pg"
  engine                  = "postgres"
  engine_version          = "16.3"
  instance_class          = "db.r7g.2xlarge"
  allocated_storage       = 200
  max_allocated_storage   = 1000
  storage_type            = "gp3"
  storage_encrypted       = true
  kms_key_id              = aws_kms_key.main.arn
  username                = "automata"
  password                = random_password.pg.result
  db_subnet_group_name    = aws_db_subnet_group.main.name
  vpc_security_group_ids  = [aws_security_group.rds.id]
  backup_retention_period = 14
  deletion_protection     = true
  multi_az                = true
  skip_final_snapshot     = false
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
}

resource "random_password" "pg" {
  length  = 32
  special = false
}

resource "aws_kms_key" "main" {
  description         = "${var.cluster_name}-data-key"
  enable_key_rotation = true
}

# ----------------- S3 (object store) -----------------
resource "aws_s3_bucket" "artifacts" {
  bucket        = "${var.cluster_name}-artifacts"
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ----------------- Outputs -----------------
output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "postgres_endpoint" {
  value = aws_db_instance.postgres.address
}
