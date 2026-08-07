# ── Linode ────────────────────────────────────────────────────────────────────
variable "linode_token" {
  description = "Linode API token"
  type        = string
  sensitive   = true
}

# ── App ──────────────────────────────────────────────────────────────────────
variable "domain" {
  description = "Primary domain for the app (e.g. app.senzii.com)"
  type        = string
  default     = "app.senzii.com"
}

variable "mcp_domain" {
  description = "Domain for the MCP server (e.g. mcp.senzii.com)"
  type        = string
  default     = "mcp.senzii.com"
}

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
  description = "PostgreSQL connection string (e.g. postgresql://user:pass@host/db?sslmode=require)"
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
  default    = "Senzii <noreply@senzii.com>"
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
  description = "Default org ID for MCP server"
  type        = string
  default     = ""
}

# ── TLS cert (for NodeBalancer TLS termination) ────────────────────────────────
variable "ssl_cert" {
  description = "PEM-encoded TLS certificate (full chain). Get with: certbot certonly --manual --preferred-challenges dns -d app.senzii.com -d mcp.senzii.com"
  type        = string
  default    = ""
  sensitive  = true
}

variable "ssl_key" {
  description = "PEM-encoded TLS private key"
  type        = string
  default    = ""
  sensitive  = true
}

# ── Infra ─────────────────────────────────────────────────────────────────────
variable "backend_count" {
  description = "Number of backend instances (behind NodeBalancer)"
  type        = number
  default     = 2
}

variable "region" {
  description = "Linode region"
  type        = string
  default     = "us-east"
}

variable "instance_type" {
  description = "Linode instance type"
  type        = string
  default     = "g6-nanode-1"
}

variable "ssh_public_key" {
  description = "SSH public key for instance access"
  type        = string
}