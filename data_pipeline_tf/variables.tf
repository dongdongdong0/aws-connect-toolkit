########################################
# Regional / naming
########################################
variable "region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region"
}

variable "pipeline_name" {
  type        = string
  description = "Logical pipeline name, used in tags and inline policy names"
}

########################################
# NEW resources this Terraform will create
########################################
variable "s3_bucket_name" {
  type        = string
  description = "Landing bucket for CTR data (globally unique)"
}

variable "kinesis_stream_name" {
  type        = string
  description = "Kinesis Data Stream name"
}

variable "firehose_delivery_stream_name" {
  type        = string
  description = "Firehose delivery stream name"
}

variable "glue_crawler_name" {
  type        = string
  description = "Glue Crawler name"
}

########################################
# EXISTING resources this Terraform will look up
# (created by the CFN roles / lambda / glue+athena stacks)
########################################
variable "firehose_role_name" {
  type        = string
  description = "Name of an existing IAM role for Firehose"
}

variable "crawler_role_name" {
  type        = string
  description = "Name of an existing IAM role for the Glue Crawler"
}

variable "glue_database_name" {
  type        = string
  description = "Name of an existing Glue database (created by glue_athena stack)"
}

########################################
# Optional Lambda transformation
########################################
variable "enable_lambda_transform" {
  type        = bool
  default     = false
  description = "Attach a Lambda processor to Firehose"
}

variable "lambda_function_name" {
  type        = string
  default     = ""
  description = "Name of the Lambda function (required if enable_lambda_transform=true)"
}

########################################
# Firehose buffering
########################################
variable "firehose_buffer_interval_seconds" {
  type    = number
  default = 300
}

variable "firehose_buffer_size_mb" {
  type    = number
  default = 5
}
