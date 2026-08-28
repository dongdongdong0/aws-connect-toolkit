# ============================================================
# Root module — orchestrates the pipeline module.
#
# Every project resource is now inside `modules/pipeline/`.
# The root module's only job is to wire tfvars into the module
# call and re-export a few outputs.
# ============================================================

module "pipeline" {
  source = "./modules/pipeline"

  pipeline_name                    = var.pipeline_name
  s3_bucket_name                   = var.s3_bucket_name
  kinesis_stream_name              = var.kinesis_stream_name
  firehose_delivery_stream_name    = var.firehose_delivery_stream_name
  glue_crawler_name                = var.glue_crawler_name
  firehose_role_name               = var.firehose_role_name
  crawler_role_name                = var.crawler_role_name
  glue_database_name               = var.glue_database_name
  enable_lambda_transform          = var.enable_lambda_transform
  lambda_function_name             = var.lambda_function_name
  firehose_buffer_interval_seconds = var.firehose_buffer_interval_seconds
  firehose_buffer_size_mb          = var.firehose_buffer_size_mb
}

