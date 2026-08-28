output "bucket_name" {
  value       = aws_s3_bucket.landing.id
  description = "S3 landing bucket name"
}

output "bucket_arn" {
  value       = aws_s3_bucket.landing.arn
  description = "S3 landing bucket ARN"
}

output "kinesis_stream_arn" {
  value       = aws_kinesis_stream.this.arn
  description = "Kinesis Data Stream ARN"
}

output "firehose_arn" {
  value       = aws_kinesis_firehose_delivery_stream.this.arn
  description = "Firehose delivery stream ARN"
}

output "glue_crawler_name" {
  value       = aws_glue_crawler.this.name
  description = "Glue Crawler name"
}

output "lambda_attached" {
  value       = local.attach_lambda
  description = "Whether a Lambda processor was attached to Firehose"
}
