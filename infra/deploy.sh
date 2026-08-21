#!/bin/bash
# Senzii App — Master Deployment CLI
# Dispatches a deployment to either Azure or Linode based on the first argument.
#
# Usage:
#   ./infra/deploy.sh azure   <flags...>   # deploy to Azure (VMSS / staging VM)
#   ./infra/deploy.sh linode  <flags...>   # deploy to Linode backends
#   ./infra/deploy.sh help                 # show this help
#
# Provider-specific flags are passed through to the underlying script.
# Examples:
#   ./infra/deploy.sh azure --target staging   # deploy to Azure staging
#   ./infra/deploy.sh azure --restart-only     # restart Azure services only
#   ./infra/deploy.sh linode --restart-only    # restart Linode services only
#
# Shortcuts:
#   ./infra/deploy.sh                # with no args, prints this help
#   ./infra/deploy.sh -h | --help    # same

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
    cat <<'EOF'
Senzii deployment CLI

USAGE
    ./infra/deploy.sh <provider> [provider flags...]

PROVIDERS
    azure     Deploy to Azure (VMSS behind Standard LB, or staging VM).
              See infra/deploy_azure.sh for flags.
    linode    Deploy to the Linode backends behind the NodeBalancer.
              See infra/deploy_linode.sh for flags.
    help      Show this help.

EXAMPLES
    ./infra/deploy.sh azure
    ./infra/deploy.sh azure --target staging
    ./infra/deploy.sh azure --restart-only
    ./infra/deploy.sh linode
    ./infra/deploy.sh linode --restart-only
EOF
}

if [ $# -eq 0 ]; then
    usage
    exit 1
fi

PROVIDER="$1"
shift

case "$PROVIDER" in
    azure)
        exec "$SCRIPT_DIR/deploy_azure.sh" "$@"
        ;;
    linode)
        exec "$SCRIPT_DIR/deploy_linode.sh" "$@"
        ;;
    help|-h|--help)
        usage
        exit 0
        ;;
    *)
        echo "ERROR: unknown provider '$PROVIDER'." >&2
        echo "Use one of: azure | linode | help" >&2
        exit 1
        ;;
esac
