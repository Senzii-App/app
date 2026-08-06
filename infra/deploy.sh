#!/bin/bash
# Senzii App Deploy Script
# Pushes code to all backend instances behind the NodeBalancer.
#
# Usage:
#   ./infra/deploy.sh                # deploy to all backends
#   ./infra/deploy.sh --build-only    # just build, no upload
#   ./infra/deploy.sh --restart-only  # just restart remote services
#
# Prerequisites:
#   - terraform output backend_ips shows instance IPs
#   - SSH key authorized on all backends
#   - Repo pushed to origin/main

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_DIR="$SCRIPT_DIR"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Get backend IPs from Terraform
BACKEND_IPS=$(terraform -chdir="$INFRA_DIR" output -json backend_ips 2>/dev/null | python3 -c "import sys,json; print(' '.join(json.load(sys.stdin)))" 2>/dev/null || echo "")

if [ -z "$BACKEND_IPS" ]; then
    echo "ERROR: Could not get backend IPs from Terraform. Run 'terraform apply' first."
    exit 1
fi

BUILD_ONLY=false
RESTART_ONLY=false

for arg in "$@"; do
    case $arg in
        --build-only) BUILD_ONLY=true ;;
        --restart-only) RESTART_ONLY=true ;;
    esac
done

echo "Backends: $BACKEND_IPS"

if [ "$RESTART_ONLY" = true ]; then
    for IP in $BACKEND_IPS; do
        echo "Restarting services on $IP..."
        ssh -o StrictHostKeyChecking=no root@"$IP" "systemctl restart senzii-app senzii-mcp && sleep 2 && curl -sf http://127.0.0.1:3000/health && echo ' OK' || echo ' FAIL'"
    done
    exit 0
fi

# ── Sync site static files from Senzii/site ───────────────────────────────────
SITE_DIR="$PROJECT_DIR/../site"
if [ -d "$SITE_DIR" ]; then
    echo "Syncing static site files..."
    for IP in $BACKEND_IPS; do
        rsync -az --delete --exclude='.git' --exclude='vercel.json' "$SITE_DIR/" root@"$IP":/tmp/senzii-site/
    done
else
    echo "WARNING: Site directory not found at $SITE_DIR — skipping static sync"
fi

# ── Sync app code ─────────────────────────────────────────────────────────────
echo "Syncing app code..."
for IP in $BACKEND_IPS; do
    echo "  → $IP"
    rsync -az --delete \
        --exclude='.git' \
        --exclude='.venv' \
        --exclude='.env' \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        --exclude 'infra/' \
        "$PROJECT_DIR/" root@"$IP":/opt/senzii/
done

if [ "$BUILD_ONLY" = true ]; then
    echo "Build-only mode — skipping remote commands."
    exit 0
fi

# ── Install deps and restart on each backend ─────────────────────────────────
for IP in $BACKEND_IPS; do
    echo "Installing deps and restarting on $IP..."
    ssh -o StrictHostKeyChecking=no root@"$IP" bash << 'REMOTEOF'
        cd /opt/senzii
        .venv/bin/pip install -r requirements.txt --quiet
        if [ -d /tmp/senzii-site ]; then
            rsync -a --delete --exclude='.git' --exclude='vercel.json' /tmp/senzii-site/ /opt/senzii/site/
        fi
        chown -R www-data:www-data /opt/senzii
        systemctl restart senzii-app
        systemctl restart senzii-mcp
        sleep 2
        curl -sf http://127.0.0.1:3000/health && echo " → health OK" || echo " → health FAIL"
REMOTEOF
done

echo "Deploy complete to all backends."