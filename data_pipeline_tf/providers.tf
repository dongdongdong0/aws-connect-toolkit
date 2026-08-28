provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "connect-data-pipeline"
      Pipeline  = var.pipeline_name
      ManagedBy = "terraform"
    }
  }
}
