# ── Azure ────────────────────────────────────────────────────────────────────
# (No provider token needed — azurerm uses `az login` credentials.)

# ── Environment ──────────────────────────────────────────────────────────────
variable "env" {
  description = "Deployment environment: staging or prod"
  type        = string
  default     = "prod"
}

variable "azure_region" {
  description = "Azure region for all resources"
  type        = string
  default     = "North Central US"
}

variable "domain" {
  description = "Domain name for the app (e.g. app.senzii.com or staging.senzii.com)"
  type        = string
  default     = "app.senzii.com"
}

# ── App ──────────────────────────────────────────────────────────────────────
variable "repo_url" {
  description = "Git clone URL for the app repo"
  type        = string
}

variable "repo_branch" {
  description = "Git branch to deploy"
  type        = string
  default     = "main"
}

# ── Database ──────────────────────────────────────────────────────────────────
variable "database_url" {
  description = "PostgreSQL connection string (e.g. postgresql://user:***@host/db?sslmode=require)"
  type        = string
  sensitive   = true
}

# ── App secrets ───────────────────────────────────────────────────────────────
variable "session_secret" {
  description = "Random string for session cookie signing"
  type        = string
  sensitive   = true
}

variable "resend_api_key" {
  description = "Resend API key for email"
  type        = string
  default     = ""
  sensitive   = true
}

variable "mail_from" {
  description = "From address for outgoing email"
  type        = string
  default     = "Senzii <noreply@senzii.com>"
}

variable "base_url" {
  description = "Public base URL for the app"
  type        = string
  default     = "https://app.senzii.com"
}

variable "notify_email" {
  description = "Email to receive trial registration notifications"
  type        = string
  default     = ""
}

# ── MCP ───────────────────────────────────────────────────────────────────────
variable "mcp_port" {
  description = "Port for the MCP server"
  type        = number
  default     = 3001
}

variable "mcp_org_id" {
  description = "Default org ID for MCP server (integer, or null to omit)"
  type        = number
  default     = null
}

# ── Infra ─────────────────────────────────────────────────────────────────────
variable "instance_count" {
  description = "Number of backend instances. Staging uses 1, prod uses 2."
  type        = number
  default     = 2
}

variable "instance_type" {
  description = "Azure VM size for backend instances"
  type        = string
  default     = "Standard_B2ls_v2"
}

variable "ssh_public_key" {
  description = "SSH public key for instance access"
  type        = string
}

# ── Staging only ──────────────────────────────────────────────────────────────
variable "tailscale_auth_key" {
  description = "Tailscale auth key for staging server join. Required when env=staging."
  type        = string
  default     = ""
  sensitive   = true
}
