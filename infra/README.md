# Senzii App — Linode Deployment

2 nanodes behind a NodeBalancer, with Caddy (DNS-01 TLS via Namecheap) and Python/FastAPI + MCP server.

## Architecture

```
Internet → NodeBalancer :443/:80 (TCP passthrough, round-robin)
                ↓
    ┌─────────┴─────────┐
  Backend 1           Backend 2
  Caddy :443          Caddy :443       ← TLS via DNS-01 (Namecheap API)
    └─ uvicorn :3000     └─ uvicorn :3000
    └─ MCP :3001        └─ MCP :3001
```

**Why DNS-01 instead of HTTP-01**: With TCP passthrough + round-robin, Let's Encrypt's HTTP-01 validation hits a random backend — both backends' Caddy instances fail to obtain certs, hit the rate limit, and the site goes down. DNS-01 puts the challenge in Namecheap DNS, so both backends can solve it independently.

**Cost**: NodeBalancer (~$10/mo) + 2× Nanode 1GB ($5/mo each) = **~$20/mo total**.

## Prerequisites

### 1. External PostgreSQL database

The app uses asyncpg — you need a PostgreSQL database accessible from the Linode backends. Use Neon, Supabase, or any managed Postgres:

```bash
# Neon free tier works fine for starting out
# Create a database at https://neon.tech and copy the connection string
```

The connection string goes in `database_url` in `terraform.tfvars`.

### 2. GitHub repo for the app code

The app code needs to be in a public GitHub repo (or private with a deploy token). Create one at `github.com/Senzii-App/app` and push the code:

```bash
cd Senzii/app
git remote add origin https://github.com/Senzii-App/app.git
git push -u origin main
```

### 3. Namecheap API access (for TLS certs)

1. Log in to Namecheap → Profile → Tools → API Access
2. Enable API access
3. Whitelist the Linode backend IPs (or allow all — less secure)
4. Copy your API username and key

### 4. DNS records

Point these A records to the **NodeBalancer IP** (output after `terraform apply`):

```
app.senzii.com  → <NodeBalancer IP>
mcp.senzii.com  → <NodeBalancer IP>
```

**Important**: DNS must point to the NodeBalancer IP *before* applying Terraform, so Caddy can solve the DNS-01 challenge on first boot.

### 5. SSH key

You need an SSH key pair for accessing the backend instances:

```bash
# If you don't have one:
ssh-keygen -t ed25519 -C "senzii-deploy"
# Copy the public key for terraform.tfvars:
cat ~/.ssh/id_ed25519.pub
```

### 6. Session secret

Generate a random string for session signing:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(50))'
```

## Deploy

```bash
cd infra/
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with all your values

terraform init
terraform plan
terraform apply
```

Wait 4-5 minutes for cloud-init to complete. Then verify:

```bash
# Get the NodeBalancer IP
terraform output nodebalancer_ip

# Health check (after DNS propagates)
curl https://app.senzii.com/health
# → {"status":"healthy"}
```

## Updating the app

```bash
# Push code to GitHub first, then:
./infra/deploy.sh                # sync code + restart on all backends
./infra/deploy.sh --restart-only # just restart without code sync
```

The deploy script:
1. rsyncs app code from local → all backends (excluding .venv, .env, .git)
2. rsyncs static site files from `../site/` → all backends
3. Installs any new pip dependencies
4. Restarts senzii-app and senzii-mcp services
5. Verifies health check

## Scaling

Change `backend_count` in `terraform.tfvars` and apply:

```bash
# Scale to 3 backends
sed -i 's/backend_count = 2/backend_count = 3/' terraform.tfvars
terraform apply
```

Terraform provisions the new instance, runs cloud-init, and registers it with the NodeBalancer automatically.

## Troubleshooting

```bash
# SSH to a backend (get IPs from Terraform)
terraform output backend_ips
ssh root@<backend-ip>

# Check service status
systemctl status senzii-app
systemctl status senzii-mcp
systemctl status caddy

# Check logs
journalctl -u senzii-app -n 50
journalctl -u senzii-mcp -n 50
journalctl -u caddy -n 50

# Cloud-init status
cloud-init status --long
cat /var/log/cloud-init-output.log

# Verify the app responds locally
curl http://127.0.0.1:3000/health

# Verify Caddy is proxying
curl -k https://localhost/health
```

## Files

```
infra/
  versions.tf              — Provider config
  variables.tf             — Input variables
  main.tf                  — NodeBalancer + instances + firewall
  cloud-init.yaml.tpl      — Server bootstrap template
  terraform.tfvars.example — Template for your secrets
  deploy.sh                — Code update script
  gen_cloudinit.py         — Template generator (handles $ escaping)
  .gitignore               — Excludes state/secrets
  README.md                — This file
```