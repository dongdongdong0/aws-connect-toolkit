variable "pipeline_name" {
  type        = string
  description = "Logical pipeline name, used in tags and inline policy names"
}

variable "s3_bucket_name" {
  type        = string
  description = "Landing bucket for CTR data (globally unique)"
}

variable "kinesis_stream_name" {
  type = string
}

variable "firehose_delivery_stream_name" {
  type = string
}

variable "glue_crawler_name" {
  type = string
}

variable "firehose_role_name" {
  type        = string
  description = "Existing IAM role for Firehose"
}

variable "crawler_role_name" {
  type        = string
  description = "Existing IAM role for the Glue Crawler"
}

variable "glue_database_name" {
  type        = string
  description = "Existing Glue database (created by glue_athena stack)"
}

variable "enable_lambda_transform" {
  type    = bool
  default = false
}

variable "lambda_function_name" {
  type    = string
  default = ""
}

variable "firehose_buffer_interval_seconds" {
  type    = number
  default = 300
}

variable "firehose_buffer_size_mb" {
  type    = number
  default = 5
}
