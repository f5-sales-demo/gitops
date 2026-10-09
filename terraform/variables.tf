variable "environment_id" {
  description = "Canonical main or hashed branch identity from scripts/branch_environment.py."
  type        = string
  validation {
    condition     = var.environment_id == "main" || can(regex("^[a-z0-9][a-z0-9-]{0,23}-[0-9a-f]{12}$", var.environment_id))
    error_message = "Use the canonical branch resolver to generate the environment identity."
  }
}
variable "base_name" {
  description = "Base resource name and hostname prefix."
  type        = string
  default     = "gitops"
  validation {
    condition     = var.base_name == "gitops"
    error_message = "This deployment owns only the gitops resource prefix."
  }
}
variable "base_domain" {
  description = "Domain delegated to F5 Distributed Cloud."
  type        = string
  default     = "f5-sales-demo.com"
  validation {
    condition     = var.base_domain == "f5-sales-demo.com"
    error_message = "This demo deploys only under f5-sales-demo.com."
  }
}
variable "origin_hostname" {
  description = "Public HTTPS origin; used for DNS, TLS SNI, and HTTP Host rewriting."
  type        = string
  default     = "httpbin.org"
  validation {
    condition     = var.origin_hostname == "httpbin.org"
    error_message = "The accepted external origin is httpbin.org."
  }
}
