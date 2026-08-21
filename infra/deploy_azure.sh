#!/usr/bin/env bash
# ── deploy_azure.sh — Deploy Senzii App (Python) to Azure ────────────────────
#
# Usage:
#   ./infra/deploy_azure.sh                    # Deploy to prod (default workspace)
#   ./infra/deploy_azure.sh --target staging   # Deploy to staging environment
#   ./infra/deploy_azure.sh --restart-only     # Just restart remote services (no code pull)
#   ./infra/deploy_azure.sh --branch <name>    # Deploy a specific git branch
#
# Terraform workspaces: "default" = prod, "staging" = staging.
# The script selects the workspace before reading terraform outputs.
#
# Prod deploy method: az vmss run-command (no SSH, no public IPs on instances)
#   1. Trigger a git pull + pip install + restart on all VMSS instances
#   2. The instances pull the latest code from the repo directly
#
# Staging deploy method: direct SSH to the staging VM (has public IP)
#
# Prerequisites:
#   - az CLI authenticated (az login)
#   - terraform workspace selected with outputs available
#   - The app repo is reachable from the instances (public GitHub, or SSH key)
#
# Note: this is called by the master ./infra/deploy.sh when the first
# argument is 'azure'. You can also invoke it directly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$SCRIPT_DIR/azure"
SSH_USER="azureuser"

# ── Parse flags ──────────────────────────────────────────────────────────────
RESTART_ONLY=false
TF_WORKSPACE="default"
BRANCH="main"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      shift
      TF_WORKSPACE="${1:-staging}"
      ;;
    --target=*)
      TF_WORKSPACE="${1#*=}"
      ;;
    --restart-only) RESTART_ONLY=true ;;
    --branch)
      shift
      BRANCH="${1:-main}"
      ;;
    --branch=*)
      BRANCH="${1#*=}"
      ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
  shift
done

# ── Select terraform workspace before reading outputs ─────────────────────
if ! (cd "$TF_DIR" && terraform workspace select "$TF_WORKSPACE" &>/dev/null); then
  echo "Error: Terraform workspace '$TF_WORKSPACE' does not exist."
  echo "  Available workspaces:"
  (cd "$TF_DIR" && terraform workspace list 2>/dev/null | sed 's/^/    /')
  exit 1
fi

# ── Get terraform outputs ───────────────────────────────────────────────────
get_tf_output() {
  (cd "$TF_DIR" && terraform output -raw "$1" 2>/dev/null || echo "")
}

RG_NAME="$(get_tf_output resource_group)"
VMSS_NAME="$(get_tf_output vmss_name)"
STAGING_IP="$(get_tf_output public_ip)"

# ── Check prerequisites ─────────────────────────────────────────────────────
if ! command -v az &>/dev/null; then
  echo "Error: Azure CLI (az) is not installed."
  echo "  Install: brew install azure-cli"
  echo "  Then: az login"
  exit 1
fi

if ! az account show &>/dev/null; then
  echo "Error: Not authenticated to Azure. Run: az login"
  exit 1
fi

echo "═══════════════════════════════════════════════════════════"
echo "  Senzii Deploy → $TF_WORKSPACE (branch: $BRANCH)"
echo "═══════════════════════════════════════════════════════════"

# ── Build the remote deploy script ─────────────────────────────────────────
# For the Python app, "deploy" = git pull + pip install + restart.
# The instances clone from the repo at provision time; subsequent deploys
# just pull the latest code.

DEPLOY_SCRIPT=""
if [[ "$RESTART_ONLY" == false ]]; then
  DEPLOY_SCRIPT="
cd /opt/senzii
git fetch origin
git checkout --force origin/${BRANCH}
.venv/bin/pip install -r requirements.txt --quiet
chown -R senzii:senzii /opt/senzii
"
fi

RESTART_CMDS="systemctl restart senzii-app && systemctl restart senzii-mcp"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT}
${RESTART_CMDS}
sleep 2
curl -sf http://127.0.0.1:3000/health && echo ' → health OK' || echo ' → health FAIL'
"

# ── Deploy ─────────────────────────────────────────────────────────────────
if [[ "$TF_WORKSPACE" != "staging" ]]; then
  # ── Prod: run-command on VMSS ────────────────────────────────────────────
  echo ""
  echo "[1/2] Running deploy script on VMSS instances via run-command..."
  echo "       (This may take 1-2 minutes per instance)"

  INSTANCE_IDS=$(az vmss list-instances \
    --resource-group "$RG_NAME" \
    --name "$VMSS_NAME" \
    --query "[].instanceId" \
    --output tsv 2>/dev/null)

  if [[ -z "$INSTANCE_IDS" ]]; then
    echo "Error: No VMSS instances found."
    exit 1
  fi

  for iid in $INSTANCE_IDS; do
    echo "       → Instance $iid..."
    az vmss run-command invoke \
      --resource-group "$RG_NAME" \
      --name "$VMSS_NAME" \
      --instance-id "$iid" \
      --command-id RunShellScript \
      --scripts "$DEPLOY_SCRIPT" 2>&1 | tail -5
  done

  echo ""
  echo "[2/2] Done!"
  echo ""
  echo "═══════════════════════════════════════════════════════════"
  LB_IP="$(get_tf_output lb_ip)"
  echo "  Deployed to VMSS! Test at: http://$LB_IP/health"
  echo "  (Before DNS cutover, use the LB IP directly)"
  echo "═══════════════════════════════════════════════════════════"

else
  # ── Staging: direct SSH ────────────────────────────────────────────────
  echo ""
  echo "[1/2] Running deploy script on $SSH_USER@$STAGING_IP..."
  ssh -o StrictHostKeyChecking=no "$SSH_USER@$STAGING_IP" "sudo bash -c '$DEPLOY_SCRIPT'"

  echo ""
  echo "[2/2] Done!"
  echo ""
  echo "═══════════════════════════════════════════════════════════"
  echo "  Deployed to staging! Test via Tailscale."
  echo "═══════════════════════════════════════════════════════════"
fi
