storage "file" {
  path = "/openbao/data"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

# Internal Docker-network address — modules reach OpenBao at http://openbao:8200.
api_addr     = "http://openbao:8200"
cluster_addr = "https://openbao:8201"
ui           = false
