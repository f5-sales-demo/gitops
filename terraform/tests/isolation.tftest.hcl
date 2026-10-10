mock_provider "xcsh" {}

run "main_names" {
  command = plan
  variables {
    environment_id = "main"
  }
  assert {
    condition     = output.hostname == "gitops.f5-sales-demo.com"
    error_message = "Main must use the unsuffixed hostname."
  }
  assert {
    condition     = output.namespace == "gitops" && alltrue([for name in values(output.resource_names) : name == "gitops"])
    error_message = "Main must own only unsuffixed names."
  }
  assert {
    condition     = xcsh_http_loadbalancer.httpbin.http.port == 80 && xcsh_http_loadbalancer.httpbin.http.dns_volterra_managed
    error_message = "Public HTTP and managed DNS are required."
  }
  assert {
    condition     = xcsh_origin_pool.httpbin.port == 443 && xcsh_origin_pool.httpbin.use_tls.sni == "httpbin.org" && xcsh_origin_pool.httpbin.use_tls.skip_server_verification == null
    error_message = "The origin must use verified TLS and the HTTPBin SNI."
  }
}
run "preview_names" {
  command = plan
  variables {
    environment_id = "feature-demo-0123456789ab"
  }
  assert {
    condition     = output.hostname == "gitops-feature-demo-0123456789ab.f5-sales-demo.com" && output.namespace == "gitops-feature-demo-0123456789ab" && output.resource_names.origin_pool == "gitops" && output.resource_names.http_loadbalancer == "gitops"
    error_message = "Previews must isolate their namespace and hostname while keeping object names fixed."
  }
}

run "preview_references" {
  command = plan
  variables {
    environment_id = "feature-demo-fedcba987654"
  }
  assert {
    condition     = xcsh_origin_pool.httpbin.namespace == xcsh_namespace.environment.name && xcsh_http_loadbalancer.httpbin.namespace == xcsh_namespace.environment.name && xcsh_http_loadbalancer.httpbin.routes[0].simple_route.origin_pools[0].pool.namespace == xcsh_namespace.environment.name && xcsh_http_loadbalancer.httpbin.routes[0].simple_route.origin_pools[0].pool.name == "gitops"
    error_message = "Every object and pool reference must include the environment namespace."
  }
}
