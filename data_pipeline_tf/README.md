# CTR Data Pipeline — Terraform Version

Terraform equivalent of `data_pipeline/data_pipeline.yaml` (CFN stack #4). Reuses the IAM roles, Lambda, and Glue database created by the first three CFN stacks and builds a second, side-by-side pipeline on top of them.

## What it does

- Looks up existing resources by name using `data` blocks
  - `data.aws_iam_role.firehose` — Firehose role from `pipeline_roles.yaml`
  - `data.aws_iam_role.crawler` — Crawler role from `pipeline_roles.yaml`
  - `data.aws_lambda_function.clean` — cleaning Lambda from `clean_ctr_lambda.yaml`
- Creates the pipeline resources: S3 landing bucket, Kinesis stream, Firehose delivery stream, Glue Crawler.
- Attaches bucket-scoped inline S3 policies to the shared Firehose and Crawler roles using `aws_iam_role_policy`. These are owned by this stack, so `terraform destroy` removes them cleanly — closing the "Known gap" from the CFN version's shell script.

## Runbook

```bash
cd data_pipeline_tf
terraform init          # first time only
terraform fmt           # optional: format .tf files
terraform plan          # dry run — see what will be created
terraform apply         # confirm with `yes`
```

Tear down:

```bash
terraform destroy
```

## Design notes worth reading

- **`local.attach_lambda`** — the same "enable AND non-empty name" gate used by `deploy_pipeline.sh`. Drives the `dynamic "processors"` block and the `aws_lambda_permission.firehose_invoke` count.
- **`dynamic "processors"`** — Terraform's replacement for CFN's `!If [Cond, {...}, AWS::NoValue]`. `for_each = local.attach_lambda ? [1] : []` iterates once or zero times, so the block either exists in the plan or vanishes entirely.
- **`data.aws_lambda_function.clean` with `count`** — using `count = local.attach_lambda ? 1 : 0` gates the data lookup itself. If disabled we never even call the Lambda API. References use `data.aws_lambda_function.clean[0].arn`.
- **`default_tags` in the provider** — every resource in this project inherits `Project / Pipeline / ManagedBy` tags. One line, no per-resource tag noise.
- **`depends_on` on IAM policy** — Terraform infers most dependencies from resource references. Firehose already references the bucket, but not the inline policy, so we state the policy dependency explicitly to avoid an "unauthorized" race on first create.
- **State** — this project uses local state for demo simplicity. Uncomment the `backend "s3"` block in `versions.tf` for real use, and put the state bucket in a separate account when possible.
