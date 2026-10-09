terraform {
  required_version = ">= 1.16.3, < 2.0.0"
  required_providers {
    xcsh = {
      source  = "f5-sales-demo/xcsh"
      version = "15.5.2"
    }
  }
  backend "kubernetes" {}
}
provider "xcsh" {}
