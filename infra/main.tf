locals {
  app_dir = "/opt/senzii"
}

# ── SSH Key ───────────────────────────────────────────────────────────────────
resource "linode_sshkey" "deploy" {
  label   = "senzii-app-deploy"
  ssh_key = var.ssh_public_key
}

# ── NodeBalancer ──────────────────────────────────────────────────────────────
resource "linode_nodebalancer" "main" {
  label   = "senzii-app-lb"
  region  = var.region
  client_transfers = false
}

# HTTPS config — TCP passthrough to Caddy on each backend (DNS-01 certs)
resource "linode_nodebalancer_config" "https" {
  nodebalancer_id = linode_nodebalancer.main.id
  port            = 443
  protocol        = "tcp"
  algorithm       = "roundrobin"
  check           = "connection"
  check_attempts  = 3
  check_timeout   = 5
  check_interval  = 10
  stickiness       = "none"
}

# HTTP config — TCP passthrough to Caddy (redirects to HTTPS)
resource "linode_nodebalancer_config" "http" {
  nodebalancer_id = linode_nodebalancer.main.id
  port            = 80
  protocol        = "tcp"
  algorithm       = "roundrobin"
  check           = "connection"
  check_attempts  = 3
  check_timeout   = 5
  check_interval  = 10
  stickiness       = "none"
}

# ── Backend Instances ─────────────────────────────────────────────────────────
resource "linode_instance" "backend" {
  count       = var.backend_count
  label       = "senzii-app-${count.index + 1}"
  region      = var.region
  type        = var.instance_type
  image       = "linode/ubuntu24.04"
  private_ip  = true
  ssh_keys    = [linode_sshkey.deploy.ssh_key]
  tags        = ["senzii", "app", "backend"]
  root_pass   = "SenziiApp2026DeployPass!"

  metadata {
    user_data = base64encode(templatefile("${path.module}/cloud-init.yaml.tpl", {
      app_dir            = local.app_dir
      repo_url           = var.repo_url
      repo_branch        = var.repo_branch
      database_url       = var.database_url
      session_secret     = var.session_secret
      port               = 3000
      mcp_port           = var.mcp_port
      domain             = var.domain
      mcp_domain         = var.mcp_domain
      base_url           = var.base_url
      resend_api_key     = var.resend_api_key
      mail_from          = var.mail_from
      notify_email       = var.notify_email
      stripe_secret_key  = var.stripe_secret_key
      stripe_webhook_secret = var.stripe_webhook_secret
      stripe_price_id    = var.stripe_price_id
      mcp_org_id         = var.mcp_org_id
      namecheap_api_user = var.namecheap_api_user
      namecheap_api_key  = var.namecheap_api_key
    }))
  }

  # Don't recreate instances when only metadata changes (env var updates)
  lifecycle {
    ignore_changes = [metadata]
  }
}

# ── Register backends with NodeBalancer ───────────────────────────────────────
resource "linode_nodebalancer_node" "https_backend" {
  count            = var.backend_count
  nodebalancer_id  = linode_nodebalancer.main.id
  config_id        = linode_nodebalancer_config.https.id
  label            = "backend-${count.index + 1}"
  address          = "${linode_instance.backend[count.index].private_ip_address}:443"
  mode             = "accept"
  weight           = 50
}

resource "linode_nodebalancer_node" "http_backend" {
  count            = var.backend_count
  nodebalancer_id  = linode_nodebalancer.main.id
  config_id        = linode_nodebalancer_config.http.id
  label            = "backend-${count.index + 1}"
  address          = "${linode_instance.backend[count.index].private_ip_address}:80"
  mode             = "accept"
  weight           = 50
}

# ── Firewall ──────────────────────────────────────────────────────────────────
resource "linode_firewall" "app" {
  label = "senzii-app-firewall"
  tags  = ["senzii"]

  inbound_policy  = "DROP"
  outbound_policy = "ACCEPT"

  inbound {
    label    = "ssh"
    action   = "ACCEPT"
    protocol = "TCP"
    ports    = "22"
    ipv4     = ["0.0.0.0/0"]
    ipv6     = ["::/0"]
  }

  # Allow health checks from NodeBalancer
  inbound {
    label    = "http-nodebalancer"
    action   = "ACCEPT"
    protocol = "TCP"
    ports    = "80"
    ipv4     = ["192.168.0.0/16"]
  }

  inbound {
    label    = "https-nodebalancer"
    action   = "ACCEPT"
    protocol = "TCP"
    ports    = "443"
    ipv4     = ["192.168.0.0/16"]
  }
}

# Attach firewall to all backends
resource "linode_instance_firewall_attachment" "backend" {
  count         = var.backend_count
  instance_id   = linode_instance.backend[count.index].id
  firewall_id   = linode_firewall.app.id
}

# ── Outputs ───────────────────────────────────────────────────────────────────
output "nodebalancer_ip" {
  value       = linode_nodebalancer.main.ipv4
  description = "NodeBalancer public IP — point app.senzii.com and mcp.senzii.com DNS A records here"
}

output "backend_ips" {
  value       = [for b in linode_instance.backend : b.ipv4]
  description = "Direct IPs of backend instances (for SSH/deploys)"
}

output "backend_private_ips" {
  value       = [for b in linode_instance.backend : b.private_ip_address]
  description = "Private IPs of backends (for NodeBalancer registration)"
}