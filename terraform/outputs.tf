output "hostname" {
  description = "Public HTTP hostname for this environment."
  value       = local.hostname
}
output "namespace" {
  description = "Independently owned F5 Distributed Cloud namespace."
  value       = xcsh_namespace.environment.name
}
output "resource_names" {
  description = "Resources owned by this environment."
  value = {
    namespace         = xcsh_namespace.environment.name
    origin_pool       = xcsh_origin_pool.httpbin.name
    http_loadbalancer = xcsh_http_loadbalancer.httpbin.name
  }
}
