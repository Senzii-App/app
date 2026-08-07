# Senzii App — Linode Deployment

2 nanodes behind a NodeBalancer, with TLS terminated at the NodeBalancer and Python/FastAPI + MCP server.

## Architecture

```
Internet → NodeBalancer :443 (TLS termination, round-robin)
                ↓ plain HTTP
    ┌─────────┴─────────┐
  Backend 1           Backend 2
  uvicorn :80          uvicorn :80    (FastAPI app, 2 workers)
  MCP :3001            MCP :3001     (MCP server)
```

**TLS**: Terminated at the NodeBalancer. You provide a cert (Let's Encrypt via certbot manual DNS), upload it to the NodeBalancer via Terraform. Renew every 90 days.

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