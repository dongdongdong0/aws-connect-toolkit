terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # For real use, add S3 + DynamoDB backend here. Left as local
  # state for the demo so `terraform init` needs no prep.
  # backend "s3" {
  #   bucket         = "your-tf-state-bucket"
  #   key            = "connect-pipeline/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "tf-locks"
  #   encrypt        = true
  # }
}
