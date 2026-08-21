#!/bin/bash
# Senzii App — Linode Deployment
# Pushes code to all backend instances behind the NodeBalancer.
#
# Usage:
#   ./infra/deploy_linode.sh                # deploy to all backends
#   ./infra/deploy_linode.sh --build-only    # just build, no upload
#   ./infra/deploy_linode.sh --restart-only  # just restart remote services
#
# Note: this is called by the master ./infra/deploy.sh when the first
# argument is 'linode'. You can also invoke it directly.
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
        ssh -o StrictHostKeyChecking=no root@"$IP" "systemctl restart senzii-app senzii-mcp caddy && sleep 2 && curl -sf -H 'Host: app.senzii.com' http://127.0.0.1:80/health && echo ' OK' || echo ' FAIL'"
    done
    exit 0
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
        cd /opt/senzii/app
        .venv/bin/pip install -r requirements.txt --quiet
        chown -R www-data:www-data /opt/senzii/app
        systemctl restart senzii-app
        systemctl restart senzii-mcp
        systemctl restart caddy
        sleep 2
        curl -sf -H 'Host: app.senzii.com' http://127.0.0.1:80/health && echo " → health OK" || echo " → health FAIL"
REMOTEOF
done

echo "Deploy complete to all backends."