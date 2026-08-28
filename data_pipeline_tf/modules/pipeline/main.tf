# ============================================================
# CTR Data Pipeline — reusable module
#
# Reuses:
#   - IAM roles created by pipeline_roles.yaml
#   - Lambda function created by clean_ctr_lambda.yaml (optional)
#   - Glue database created by glue_athena.yaml
#
# Creates:
#   - S3 landing bucket
#   - Kinesis Data Stream
#   - Firehose delivery stream (with optional Lambda processor)
#   - Glue Crawler
#   - Bucket-scoped inline S3 policies on the shared roles,
#     owned by this stack so `terraform destroy` cleans up.
# ============================================================

locals {
  attach_lambda = var.enable_lambda_transform && var.lambda_function_name != ""
}

########################################
# Data sources — look up existing resources by name
########################################
data "aws_iam_role" "firehose" {
  name = var.firehose_role_name
}

data "aws_iam_role" "crawler" {
  name = var.crawler_role_name
}

data "aws_lambda_function" "clean" {
  count         = local.attach_lambda ? 1 : 0
  function_name = var.lambda_function_name
}

########################################
# S3 landing bucket
########################################
resource "aws_s3_bucket" "landing" {
  bucket = var.s3_bucket_name

  lifecycle {
    prevent_destroy = false
  }
}

resource "aws_s3_bucket_versioning" "landing" {
  bucket = aws_s3_bucket.landing.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "landing" {
  bucket                  = aws_s3_bucket.landing.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

########################################
# Kinesis Data Stream
########################################
resource "aws_kinesis_stream" "this" {
  name             = var.kinesis_stream_name
  shard_count      = 1
  retention_period = 24
}

########################################
# Bucket-scoped inline policies on the shared roles
########################################
resource "aws_iam_role_policy" "firehose_s3" {
  name = "DataPipelineFirehoseS3-${var.pipeline_name}"
  role = data.aws_iam_role.firehose.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "S3WriteForPipeline"
      Effect = "Allow"
      Action = [
        "s3:AbortMultipartUpload",
        "s3:GetBucketLocation",
        "s3:GetObject",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads",
        "s3:PutObject",
      ]
      Resource = [
        aws_s3_bucket.landing.arn,
        "${aws_s3_bucket.landing.arn}/*",
      ]
    }]
  })
}

resource "aws_iam_role_policy" "crawler_s3" {
  name = "DataPipelineCrawlerS3-${var.pipeline_name}"
  role = data.aws_iam_role.crawler.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "S3ReadForCrawler"
      Effect = "Allow"
      Action = ["s3:GetObject", "s3:ListBucket"]
      Resource = [
        aws_s3_bucket.landing.arn,
        "${aws_s3_bucket.landing.arn}/*",
      ]
    }]
  })
}

########################################
# Firehose delivery stream
########################################
resource "aws_kinesis_firehose_delivery_stream" "this" {
  name        = var.firehose_delivery_stream_name
  destination = "extended_s3"

  kinesis_source_configuration {
    kinesis_stream_arn = aws_kinesis_stream.this.arn
    role_arn           = data.aws_iam_role.firehose.arn
  }

  extended_s3_configuration {
    bucket_arn         = aws_s3_bucket.landing.arn
    role_arn           = data.aws_iam_role.firehose.arn
    buffering_interval = var.firehose_buffer_interval_seconds
    buffering_size     = var.firehose_buffer_size_mb
    compression_format = "GZIP"

    processing_configuration {
      enabled = true

      processors {
        type = "AppendDelimiterToRecord"
      }

      dynamic "processors" {
        for_each = local.attach_lambda ? [1] : []
        content {
          type = "Lambda"
          parameters {
            parameter_name  = "LambdaArn"
            parameter_value = data.aws_lambda_function.clean[0].arn
          }
        }
      }
    }
  }

  depends_on = [aws_iam_role_policy.firehose_s3]
}

########################################
# Lambda resource-based permission
########################################
resource "aws_lambda_permission" "firehose_invoke" {
  count         = local.attach_lambda ? 1 : 0
  statement_id  = "AllowFirehoseInvoke-${var.pipeline_name}"
  action        = "lambda:InvokeFunction"
  function_name = data.aws_lambda_function.clean[0].function_name
  principal     = "firehose.amazonaws.com"
  source_arn    = aws_kinesis_firehose_delivery_stream.this.arn
}

########################################
# Glue Crawler
########################################
resource "aws_glue_crawler" "this" {
  name          = var.glue_crawler_name
  role          = data.aws_iam_role.crawler.arn
  database_name = var.glue_database_name

  s3_target {
    path = "s3://${aws_s3_bucket.landing.id}/"
  }

  schema_change_policy {
    update_behavior = "UPDATE_IN_DATABASE"
    delete_behavior = "LOG"
  }

  depends_on = [
    aws_iam_role_policy.crawler_s3,
    aws_kinesis_firehose_delivery_stream.this,
  ]
}
