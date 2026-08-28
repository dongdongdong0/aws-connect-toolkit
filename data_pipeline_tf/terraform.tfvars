########################################
# Regional / naming
########################################
region        = "us-east-1"
pipeline_name = "ctr-tf"

########################################
# NEW resources this Terraform will create
########################################
s3_bucket_name                = "my-ctr-landing-tf-zane20260828"
kinesis_stream_name           = "ctr-stream-tf"
firehose_delivery_stream_name = "ctr-firehose-tf"
glue_crawler_name             = "ctr-crawler-tf"

########################################
# EXISTING resources (created by the CFN roles / glue+athena stacks)
########################################
firehose_role_name = "connect-data-pipeline-firehose-role"
crawler_role_name  = "connect-data-pipeline-crawler-role"
glue_database_name = "connect_ctr_db"

########################################
# Optional Lambda transformation — DISABLED this run.
# Flip enable_lambda_transform back to true and set the name
# to re-attach the cleaning Lambda.
########################################
enable_lambda_transform = false
lambda_function_name    = ""

########################################
# Firehose buffering
########################################
firehose_buffer_interval_seconds = 300
firehose_buffer_size_mb          = 5
