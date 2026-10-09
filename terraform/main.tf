locals {
  resource_name = var.environment_id == "main" ? var.base_name : "${var.base_name}-${var.environment_id}"
  hostname      = "${local.resource_name}.${var.base_domain}"
  labels = {
    "ves.io/owner"       = "gitops"
    "gitops-environment" = var.environment_id
  }
}

resource "xcsh_namespace" "environment" {
  name   = local.resource_name
  labels = local.labels
}

resource "xcsh_origin_pool" "httpbin" {
  name                   = local.resource_name
  namespace              = xcsh_namespace.environment.name
  labels                 = local.labels
  port                   = 443
  endpoint_selection     = "LOCAL_PREFERRED"
  loadbalancer_algorithm = "ROUND_ROBIN"
  origin_servers {
    public_name {
      dns_name = var.origin_hostname
    }
  }
  use_tls {
    sni                         = var.origin_hostname
    no_mtls                     = {}
    volterra_trusted_ca         = {}
    default_session_key_caching = {}
    tls_config {
      default_security = {}
    }
  }
}

resource "xcsh_http_loadbalancer" "httpbin" {
  name                             = local.resource_name
  namespace                        = xcsh_namespace.environment.name
  labels                           = local.labels
  domains                          = [local.hostname]
  advertise_on_public_default_vip  = {}
  no_challenge                     = {}
  no_service_policies              = {}
  disable_waf                      = {}
  disable_rate_limit               = {}
  disable_api_definition           = {}
  disable_api_discovery            = {}
  disable_bot_defense              = {}
  disable_client_side_defense      = {}
  disable_ip_reputation            = {}
  disable_malicious_user_detection = {}
  disable_trust_client_ip_headers  = {}
  round_robin                      = {}
  http {
    port                 = 80
    dns_volterra_managed = true
  }
  routes {
    simple_route {
      path {
        prefix = "/"
      }
      host_rewrite = var.origin_hostname
      origin_pools {
        pool {
          name      = xcsh_origin_pool.httpbin.name
          namespace = xcsh_namespace.environment.name
        }
        weight   = 1
        priority = 1
      }
    }
  }
}
