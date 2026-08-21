# ── Azure Provider ──────────────────────────────────────────────────────────
#
# Senzii App (Python/FastAPI) infrastructure on Microsoft Azure.
# Mirrors the senzii-rust Azure deployment resource-for-resource.
#
# Architecture (prod):
#
#   Internet → Standard Load Balancer :443/:80 (TCP passthrough, health probes)
#                   ↓
#   VMSS — N× B2ls_v2 instances across 2 zones
#     each running: Caddy → :3000 FastAPI app / :3001 MCP server
#                   ↓
#   Neon PostgreSQL (external)
#
# Architecture (staging):
#
#   Tailscale → single B2ls_v2 VM
#     Caddy :80 (plain HTTP, no TLS) → :3000 FastAPI / :3001 MCP
#   Public interface: SSH only.
#
# Rolling updates: update the VMSS model, Azure rolls instances in batches
# (upgrade_policy = Rolling). Or trigger manually:
#   az vmss update-instances --resource-group senzii-prod-rg \
#     --name senzii-prod-vmss --instance-ids *
#
# Scale out: change var.instance_count and `terraform apply`.
# The VMSS auto-registers new instances with the LB backend pool.
#
# BAA: AUTOMATIC — included in the Microsoft Product Terms you accept when
# creating a pay-as-you-go subscription. No separate signing step.

terraform {
  required_version = ">= 1.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
}

# ── Locals ───────────────────────────────────────────────────────────────────

locals {
  is_prod     = var.env == "prod"
  name_prefix = "senzii-${var.env}"
  rg_name     = "senzii-${var.env}-rg"
  app_dir     = "/opt/senzii"
}

# ── Resource Group ───────────────────────────────────────────────────────────

resource "azurerm_resource_group" "senzii" {
  name     = local.rg_name
  location = var.azure_region

  tags = {
    Project = "senzii"
    Env     = var.env
  }
}

# ── VNet & Subnet ────────────────────────────────────────────────────────────

resource "azurerm_virtual_network" "senzii_vnet" {
  name                = "${local.name_prefix}-vnet"
  address_space       = ["10.0.0.0/16"]
  location            = azurerm_resource_group.senzii.location
  resource_group_name = azurerm_resource_group.senzii.name

  tags = {
    Project = "senzii"
    Env     = var.env
  }
}

resource "azurerm_subnet" "senzii_subnet" {
  name                 = "${local.name_prefix}-subnet"
  resource_group_name  = azurerm_resource_group.senzii.name
  virtual_network_name = azurerm_virtual_network.senzii_vnet.name
  address_prefixes     = ["10.0.1.0/24"]
}

resource "azurerm_subnet_network_security_group_association" "senzii_nsg_assoc" {
  subnet_id                 = azurerm_subnet.senzii_subnet.id
  network_security_group_id = azurerm_network_security_group.senzii_nsg.id
}

# ── Public IP for Load Balancer (prod) ────────────────────────────────────────

resource "azurerm_public_ip" "senzii_lb_ip" {
  count               = local.is_prod ? 1 : 0
  name                = "${local.name_prefix}-lb-ip"
  location            = azurerm_resource_group.senzii.location
  resource_group_name = azurerm_resource_group.senzii.name
  allocation_method   = "Static"
  sku                 = "Standard"

  tags = {
    Project = "senzii"
    Env     = var.env
  }
}

# ── Standard Load Balancer (prod) ────────────────────────────────────────────
# TCP passthrough on :443 and :80, health probes on :80 (Caddy).
# Closest equivalent to the Linode NodeBalancer.
# TLS termination happens on Caddy (same as Linode).

resource "azurerm_lb" "senzii_lb" {
  count               = local.is_prod ? 1 : 0
  name                = "${local.name_prefix}-lb"
  location            = azurerm_resource_group.senzii.location
  resource_group_name = azurerm_resource_group.senzii.name
  sku                 = "Standard"

  frontend_ip_configuration {
    name                 = "PublicIPAddress"
    public_ip_address_id = azurerm_public_ip.senzii_lb_ip[0].id
  }

  tags = {
    Project = "senzii"
    Env     = var.env
  }
}

resource "azurerm_lb_backend_address_pool" "senzii_backend" {
  count           = local.is_prod ? 1 : 0
  name            = "${local.name_prefix}-backend-pool"
  loadbalancer_id = azurerm_lb.senzii_lb[0].id
}

resource "azurerm_lb_probe" "senzii_hc" {
  count               = local.is_prod ? 1 : 0
  name                = "${local.name_prefix}-hp"
  loadbalancer_id     = azurerm_lb.senzii_lb[0].id
  protocol            = "Tcp"
  port                = 80
  interval_in_seconds = 10
  number_of_probes    = 2
}

resource "azurerm_lb_rule" "senzii_https" {
  count                          = local.is_prod ? 1 : 0
  name                           = "${local.name_prefix}-rule-443"
  loadbalancer_id                = azurerm_lb.senzii_lb[0].id
  frontend_ip_configuration_name = "PublicIPAddress"
  protocol                       = "Tcp"
  frontend_port                  = 443
  backend_port                   = 443
  backend_address_pool_ids       = [azurerm_lb_backend_address_pool.senzii_backend[0].id]
  probe_id                       = azurerm_lb_probe.senzii_hc[0].id
  load_distribution              = "Default"
}

resource "azurerm_lb_rule" "senzii_http" {
  count                          = local.is_prod ? 1 : 0
  name                           = "${local.name_prefix}-rule-80"
  loadbalancer_id                = azurerm_lb.senzii_lb[0].id
  frontend_ip_configuration_name = "PublicIPAddress"
  protocol                       = "Tcp"
  frontend_port                  = 80
  backend_port                   = 80
  backend_address_pool_ids       = [azurerm_lb_backend_address_pool.senzii_backend[0].id]
  probe_id                       = azurerm_lb_probe.senzii_hc[0].id
  load_distribution              = "Default"
}

# ── Network Security Group ──────────────────────────────────────────────────

resource "azurerm_network_security_group" "senzii_nsg" {
  name                = "${local.name_prefix}-nsg"
  location            = azurerm_resource_group.senzii.location
  resource_group_name = azurerm_resource_group.senzii.name

  security_rule {
    name                       = "SSH"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "HTTP"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "HTTPS"
    priority                   = 120
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  dynamic "security_rule" {
    for_each = local.is_prod ? [1] : []
    content {
      name                       = "LB-HealthProbe"
      priority                   = 130
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = "Tcp"
      source_port_range          = "*"
      destination_port_range     = "80"
      source_address_prefix      = "168.63.129.16/32"
      destination_address_prefix = "*"
    }
  }

  dynamic "security_rule" {
    for_each = local.is_prod ? [] : [1]
    content {
      name                       = "HTTP-Staging"
      priority                   = 131
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = "Tcp"
      source_port_range          = "*"
      destination_port_range     = "80"
      source_address_prefix      = "*"
      destination_address_prefix = "*"
    }
  }

  tags = {
    Project = "senzii"
    Env     = var.env
  }
}

# ── SSH Public Key ──────────────────────────────────────────────────────────

resource "azurerm_ssh_public_key" "senzii_key" {
  name                = "${local.name_prefix}-deploy-key"
  resource_group_name = azurerm_resource_group.senzii.name
  location            = azurerm_resource_group.senzii.location
  public_key          = var.ssh_public_key

  tags = {
    Project = "senzii"
    Env     = var.env
  }
}

# ── Cloud-init template (shared by VMSS and staging VM) ─────────────────────

locals {
  cloud_init = templatefile(
    "${path.module}/cloud-init.yaml.tpl",
    {
      env                  = var.env
      domain               = var.domain
      app_dir              = local.app_dir
      repo_url             = var.repo_url
      repo_branch          = var.repo_branch
      database_url         = var.database_url
      session_secret       = var.session_secret
      resend_api_key       = var.resend_api_key
      mail_from            = var.mail_from
      base_url             = var.base_url
      notify_email         = var.notify_email
      mcp_port             = var.mcp_port
      mcp_org_id           = var.mcp_org_id
      tailscale_auth_key   = var.tailscale_auth_key
      storage_account_name = azurerm_storage_account.senzii_deploy.name
      storage_account_key  = azurerm_storage_account.senzii_deploy.primary_access_key
    }
  )
}

# ── Virtual Machine Scale Set (prod) ────────────────────────────────────────

resource "azurerm_linux_virtual_machine_scale_set" "senzii_vmss" {
  count               = local.is_prod ? 1 : 0
  name                = "${local.name_prefix}-vmss"
  location            = azurerm_resource_group.senzii.location
  resource_group_name = azurerm_resource_group.senzii.name
  sku                 = var.instance_type
  instances           = var.instance_count
  admin_username      = "azureuser"

  admin_ssh_key {
    username   = "azureuser"
    public_key = azurerm_ssh_public_key.senzii_key.public_key
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "latest"
  }

  os_disk {
    storage_account_type = "StandardSSD_LRS"
    caching              = "ReadWrite"
  }

  network_interface {
    name                      = "${local.name_prefix}-nic"
    primary                   = true
    network_security_group_id = azurerm_network_security_group.senzii_nsg.id

    ip_configuration {
      name      = "internal"
      primary   = true
      subnet_id = azurerm_subnet.senzii_subnet.id

      load_balancer_backend_address_pool_ids = [
        azurerm_lb_backend_address_pool.senzii_backend[0].id
      ]
    }
  }

  custom_data = base64encode(local.cloud_init)

  upgrade_mode = "Rolling"

  automatic_os_upgrade_policy {
    disable_automatic_rollback  = false
    enable_automatic_os_upgrade = false
  }

  rolling_upgrade_policy {
    max_batch_instance_percent              = 20
    max_unhealthy_instance_percent          = 20
    max_unhealthy_upgraded_instance_percent = 20
    pause_time_between_batches              = "PT1M"
  }

  extension {
    name                       = "HealthExtension"
    publisher                  = "Microsoft.ManagedServices"
    type                       = "ApplicationHealthLinux"
    type_handler_version       = "1.0"
    auto_upgrade_minor_version = true

    settings = jsonencode({
      port     = 80
      protocol = "tcp"
    })
  }

  tags = {
    Project = "senzii"
    Env     = var.env
  }
}

# ── Staging: Single VM (no VMSS, no LB) ─────────────────────────────────────

resource "azurerm_network_interface" "senzii_staging_nic" {
  count               = local.is_prod ? 0 : 1
  name                = "${local.name_prefix}-nic"
  location            = azurerm_resource_group.senzii.location
  resource_group_name = azurerm_resource_group.senzii.name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.senzii_subnet.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.senzii_staging_ip[0].id
  }
}

resource "azurerm_public_ip" "senzii_staging_ip" {
  count               = local.is_prod ? 0 : 1
  name                = "${local.name_prefix}-ip"
  location            = azurerm_resource_group.senzii.location
  resource_group_name = azurerm_resource_group.senzii.name
  allocation_method   = "Static"
  sku                 = "Standard"

  tags = {
    Project = "senzii"
    Env     = var.env
  }
}

resource "azurerm_linux_virtual_machine" "senzii_staging" {
  count                 = local.is_prod ? 0 : 1
  name                  = "${local.name_prefix}-backend-1"
  location              = azurerm_resource_group.senzii.location
  resource_group_name   = azurerm_resource_group.senzii.name
  size                  = var.instance_type
  admin_username        = "azureuser"
  network_interface_ids = [azurerm_network_interface.senzii_staging_nic[0].id]

  admin_ssh_key {
    username   = "azureuser"
    public_key = azurerm_ssh_public_key.senzii_key.public_key
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "StandardSSD_LRS"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "latest"
  }

  custom_data = base64encode(local.cloud_init)

  tags = {
    Project = "senzii"
    Env     = var.env
  }
}

# ── Storage Account for Deploy Artifacts ────────────────────────────────────
# Used by deploy_azure.sh to stage the app tarball, then az vmss run-command
# pulls it onto instances. Keeps instances on private IPs — no SSH needed.

resource "azurerm_storage_account" "senzii_deploy" {
  name                     = "${replace(local.name_prefix, "-", "")}deploy"
  resource_group_name      = azurerm_resource_group.senzii.name
  location                 = azurerm_resource_group.senzii.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"

  blob_properties {
    versioning_enabled = true
  }

  tags = {
    Project = "senzii"
    Env     = var.env
  }
}

resource "azurerm_storage_container" "deploy" {
  name                  = "deploy"
  storage_account_id    = azurerm_storage_account.senzii_deploy.id
  container_access_type = "private"
}

# ── Azure Files share for shared Caddy TLS cert storage (prod) ──────────────
# All VMSS instances mount this share at /var/lib/caddy/.local/share/caddy
# so they share the same Let's Encrypt cert store. Without this, multi-instance
# ACME validation fails (chicken-and-egg: no cert → HTTPS challenge fails → no cert).

resource "azurerm_storage_share" "caddy_certs" {
  count              = local.is_prod ? 1 : 0
  name               = "caddy-certs"
  storage_account_id = azurerm_storage_account.senzii_deploy.id
  quota              = 1
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "public_ip" {
  value       = local.is_prod ? azurerm_public_ip.senzii_lb_ip[0].ip_address : azurerm_public_ip.senzii_staging_ip[0].ip_address
  description = "Prod: LB public IP (point DNS here). Staging: VM public IP (SSH only, HTTP via Tailscale)."
}

output "lb_ip" {
  value       = local.is_prod ? azurerm_public_ip.senzii_lb_ip[0].ip_address : null
  description = "LB public IP (prod only)"
}

output "vmss_name" {
  value       = local.is_prod ? azurerm_linux_virtual_machine_scale_set.senzii_vmss[0].name : null
  description = "VMSS name (use with az vmss commands)"
}

output "resource_group" {
  value       = local.rg_name
  description = "Azure resource group name"
}

output "storage_account_name" {
  value       = azurerm_storage_account.senzii_deploy.name
  description = "Storage account for deploy artifacts"
}

output "storage_container_name" {
  value       = azurerm_storage_container.deploy.name
  description = "Blob container for deploy artifacts"
}

output "domain" {
  value = var.domain
}

output "azure_region" {
  value       = var.azure_region
  description = "Azure region"
}
