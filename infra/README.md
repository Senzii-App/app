# Senzii App — Deployment

Two supported deployment targets, both driven from a single CLI at `infra/deploy.sh`:

- **Linode** — 2 nanodes behind a NodeBalancer (see below).
- **Azure** — VMSS behind a Standard Load Balancer, or a single staging VM (see "Azure Deployment").

```
./infra/deploy.sh azure   <flags...>   # deploy to Azure (VMSS / staging VM)
./infra/deploy.sh linode  <flags...>   # deploy to Linode backends
./infra/deploy.sh help                 # list providers & flags
```

---

## Linode Deployment

2 nanodes behind a NodeBalancer, with TLS terminated at the NodeBalancer and Python/FastAPI + MCP server.

## Architecture

```
Internet → NodeBalancer :443 (TLS termination, round-robin, source-IP stickiness)
                ↓ plain HTTP
    ┌─────────┴─────────┐
  Backend 1           Backend 2
  Caddy :80           Caddy :80       (routes by Host header)
  ├─ app.senzii.com → uvicorn :3000   (FastAPI app, 2 workers)
  └─ mcp.senzii.com → MCP :3001       (MCP server)
```

**TLS**: Terminated at the NodeBalancer. You provide a cert (Let's Encrypt via certbot manual DNS), upload it to the NodeBalancer via Terraform. Renew every 90 days. The cert must cover both `app.senzii.com` and `mcp.senzii.com`.

**Stickiness**: `stickiness = "table"` (source-IP) on the :443 config — required so MCP sessions (kept in-memory per backend instance) stay pinned to one backend across `initialize`/`tools/list` calls.

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

The app code needs to be in a public GitHub repo. Create one and push the code:

```bash
cd Senzii/app
git remote add origin https://github.com/Senzii-App/app.git
git push -u origin main
```

### 3. TLS certificate

Get a Let's Encrypt cert using manual DNS verification (works with any DNS provider, no API needed):

```bash
# On your local machine:
sudo certbot certonly --manual --preferred-challenges dns \
  -d app.senzii.com -d mcp.senzii.com

# Certbot will ask you to add TXT records to your DNS — add them in Namecheap manually.
# After verification, the certs are at:
#   /etc/letsencrypt/live/app.senzii.com/fullchain.pem
#   /etc/letsencrypt/live/app.senzii.com/privkey.pem
```

Put the cert contents in `terraform.tfvars`:

```hcl
ssl_cert = file("/etc/letsencrypt/live/app.senzii.com/fullchain.pem")
ssl_key  = file("/etc/letsencrypt/live/app.senzii.com/privkey.pem")
```

### 4. DNS records

Point these A records in Namecheap to the **NodeBalancer IP** (output after `terraform apply`):

```
app.senzii.com  → <NodeBalancer IP>
mcp.senzii.com  → <NodeBalancer IP>
```

### 5. SSH key

```bash
# If you don't have one:
ssh-keygen -t ed25519 -C "senzii-deploy"
cat ~/.ssh/id_ed25519.pub
```

### 6. Session secret

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
terraform output nodebalancer_ip
curl https://app.senzii.com/health
# → {"status":"healthy"}
```

## TLS renewal

Certificates expire every 90 days. To renew:

```bash
# 1. Renew with certbot (re-run the manual DNS challenge)
sudo certbot renew

# 2. Update the cert in Terraform
terraform apply -var="ssl_cert=$(sudo cat /etc/letsencrypt/live/app.senzii.com/fullchain.pem)" \
                -var="ssl_key=$(sudo cat /etc/letsencrypt/live/app.senzii.com/privkey.pem)"
```

## Updating the app (Linode)

```bash
./infra/deploy.sh linode                # sync code + restart on all backends
./infra/deploy.sh linode --restart-only # just restart without code sync
```

The Linode deploy script (`deploy_linode.sh`):
1. rsyncs app code from local → all backends (excluding .venv, .env, .git, infra/)
2. Installs any new pip dependencies
3. Restarts senzii-app and senzii-mcp services
4. Verifies health check

## Azure Deployment

The app runs on Azure, mirroring the senzii-rust Azure deployment resource-for-resource:

- **Prod**: a Virtual Machine Scale Set (VMSS) of N× `Standard_B2ls_v2` instances behind a Standard Load Balancer (TCP passthrough). Each instance runs Caddy → FastAPI app (`:3000`) + MCP server (`:3001`).
- **Staging**: a single VM reachable only via Tailscale (no public HTTP/HTTPS).

```
Internet → Azure Standard LB :443/:80 (TCP passthrough, health probes)
                    ↓
        VMSS — N× B2ls_v2 instances (auto-registered with backend pool)
          each running: Caddy :443/:80 → :3000 FastAPI / :3001 MCP
                    ↓
        Neon PostgreSQL (external)
```

**TLS**: Terminated on Caddy (auto-HTTPS via Let's Encrypt). All VMSS instances
share one cert store via an Azure Files share (`caddy-certs`), avoiding the
multi-instance ACME race.

**BAA**: Automatic — included in the Microsoft Product Terms you accept when
creating a pay-as-you-go subscription. No separate signing step.

### Prerequisites

```bash
# 1. Azure account (pay-as-you-go) + Azure CLI
brew install azure-cli
az login

# 2. External PostgreSQL (Neon/Supabase/managed Postgres) — same as Linode
#    Set DATABASE_URL in terraform.tfvars

# 3. SSH key
ssh-keygen -t ed25519 -C "senzii-deploy"
cat ~/.ssh/id_ed25519.pub   # → ssh_public_key in terraform.tfvars
```

### Provision

```bash
cd infra/azure
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with all your values

terraform init
terraform plan
terraform apply
```

Wait 4-5 minutes for cloud-init to complete. Then verify:

```bash
terraform output public_ip
curl http://<lb-ip>/health
# → {"status":"healthy"}
```

Point DNS A records (`app.senzii.com`, `mcp.senzii.com`) at the LB public IP.

### Deploy (code updates)

```bash
./infra/deploy.sh azure                  # deploy to prod (default workspace)
./infra/deploy.sh azure --target staging # deploy to staging
./infra/deploy.sh azure --restart-only   # just restart services
./infra/deploy.sh azure --branch <name>  # deploy a specific git branch
```

The Azure deploy script (`deploy_azure.sh`):
1. Selects the Terraform workspace (`default` = prod, `staging` = staging)
2. Prod: runs a git pull + pip install + restart on every VMSS instance via `az vmss run-command` (no SSH/public IPs needed)
3. Staging: runs the same via direct SSH to the staging VM
4. Verifies health check

### Scaling (Azure)

Change `instance_count` in `terraform.tfvars` and apply. The VMSS auto-registers
new instances with the LB backend pool:

```bash
sed -i 's/instance_count = 2/instance_count = 3/' terraform.tfvars
terraform apply
```

### Azure files

```
infra/azure/
  versions.tf              — Provider config
  variables.tf             — Input variables
  main.tf                  — RG, VNet, LB, VMSS, staging VM, storage, outputs
  cloud-init.yaml.tpl      — Server bootstrap (Caddy, systemd, venv, UFW)
  terraform.tfvars.example — Template for your secrets
  staging.tfvars.example   — Staging environment template
```

## Scaling (Linode)

Change `backend_count` in `terraform.tfvars` and apply:

```bash
sed -i 's/backend_count = 2/backend_count = 3/' terraform.tfvars
terraform apply
```

## Troubleshooting

```bash
# SSH to a backend
terraform output backend_ips
ssh root@<backend-ip>

# Check service status
systemctl status senzii-app
systemctl status senzii-mcp

# Check logs
journalctl -u senzii-app -n 50
journalctl -u senzii-mcp -n 50

# Cloud-init status
cloud-init status --long
cat /var/log/cloud-init-output.log

# Verify the app responds locally
curl http://127.0.0.1:80/health
```

## Files

```
infra/
  versions.tf              — Provider config (Linode)
  variables.tf             — Input variables (Linode)
  main.tf                  — NodeBalancer + instances + firewall (Linode)
  cloud-init.yaml.tpl      — Server bootstrap template (Linode)
  terraform.tfvars.example — Template for your secrets (Linode)
  deploy.sh                — Master CLI: dispatches to azure | linode
  deploy_linode.sh         — Linode code-update script (rsync + restart)
  deploy_azure.sh          — Azure deploy script (git pull + restart via run-command/SSH)
  azure/                   — Azure Terraform + cloud-init (VMSS, LB, staging VM)
  gen_cloudinit.py         — Template generator (handles $ escaping)
  .gitignore               — Excludes state/secrets
  README.md                — This file
```