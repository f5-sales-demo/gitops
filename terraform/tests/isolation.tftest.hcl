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
    condition     = output.hostname == "gitops-feature-demo-0123456789ab.f5-sales-demo.com" && alltrue([for name in values(output.resource_names) : name == "gitops-feature-demo-0123456789ab"])
    error_message = "Every preview resource must use its isolated branch identity."
  }
}
