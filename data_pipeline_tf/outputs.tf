output "bucket_name" {
  value = module.pipeline.bucket_name
}

output "bucket_arn" {
  value = module.pipeline.bucket_arn
}

output "kinesis_stream_arn" {
  value = module.pipeline.kinesis_stream_arn
}

output "firehose_arn" {
  value = module.pipeline.firehose_arn
}

output "glue_crawler_name" {
  value = module.pipeline.glue_crawler_name
}

output "lambda_attached" {
  value = module.pipeline.lambda_attached
}
