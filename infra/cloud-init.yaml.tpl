#cloud-config
# Senzii App — Python/FastAPI + MCP server on 2 nanodes behind NodeBalancer
# TLS terminated at NodeBalancer — backends serve plain HTTP on :80

# ── Packages ──────────────────────────────────────────────────────────────────
packages:
  - python3
  - python3-venv
  - python3-pip
  - postgresql-client
  - git
  - curl
  - ufw

package_update: true
package_upgrade: true

# ── Write config files ─────────────────────────────────────────────────────────
write_files:
  - path: ${app_dir}/.env
    content: |
      DATABASE_URL=${database_url}
      PORT=80
      SESSION_SECRET=${session_secret}
      RESEND_API_KEY=${resend_api_key}
      MAIL_FROM=${mail_from}
      BASE_URL=${base_url}
      NOTIFY_EMAIL=${notify_email}
      STRIPE_SECRET_KEY=${stripe_secret_key}
      STRIPE_WEBHOOK_SECRET=${stripe_webhook_secret}
      STRIPE_PRICE_ID=${stripe_price_id}
      MCP_PORT=${mcp_port}
      ORG_ID=${mcp_org_id}

  - path: /etc/systemd/system/senzii-app.service
    content: |
      [Unit]
      Description=Senzii FastAPI App
      After=network.target

      [Service]
      Type=exec
      User=www-data
      Group=www-data
      WorkingDirectory=${app_dir}
      EnvironmentFile=${app_dir}/.env
      ExecStart=${app_dir}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 80 --workers 2
      Restart=always
      RestartSec=5

      [Install]
      WantedBy=multi-user.target

  - path: /etc/systemd/system/senzii-mcp.service
    content: |
      [Unit]
      Description=Senzii MCP Server
      After=network.target senzii-app.service

      [Service]
      Type=exec
      User=www-data
      Group=www-data
      WorkingDirectory=${app_dir}
      EnvironmentFile=${app_dir}/.env
      ExecStart=${app_dir}/.venv/bin/python -m mcp_server.main
      Restart=always
      RestartSec=5

      [Install]
      WantedBy=multi-user.target

  - path: /usr/local/bin/senzii-deploy
    permissions: '0755'
    content: |
      #!/bin/bash
      set -euo pipefail
      BRANCH=$${1:-main}
      APP_DIR=${app_dir}
      echo "Deploying Senzii (branch: $${BRANCH})..."
      cd $${APP_DIR}
      git fetch origin
      git reset --hard "origin/$${BRANCH}"
      .venv/bin/pip install -r requirements.txt --quiet
      systemctl restart senzii-app
      systemctl restart senzii-mcp
      sleep 2
      curl -sf http://127.0.0.1:80/health || echo "WARNING: health check failed"
      echo "Deploy complete."

# ── Run commands ───────────────────────────────────────────────────────────────
runcmd:
  # Create app user
  - useradd -r -s /bin/bash -d ${app_dir} www-data 2>/dev/null || true

  # Create app directory and move .env aside for git clone
  - mkdir -p ${app_dir}
  - chown www-data:www-data ${app_dir}
  - mv ${app_dir}/.env /tmp/senzii.env 2>/dev/null || true

  # Clone the app repo
  - |
    git clone --branch "${repo_branch}" "${repo_url}" ${app_dir} || echo "Clone failed — will retry on deploy"

  # Restore .env
  - mv /tmp/senzii.env ${app_dir}/.env 2>/dev/null || true

  # Create Python venv and install dependencies
  - |
    cd ${app_dir}
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip --quiet
    .venv/bin/pip install -r requirements.txt --quiet

  # Clone the site repo (static files)
  - |
    git clone --depth 1 https://github.com/Senzii-App/site.git /tmp/senzii-site || echo "Site clone failed"
    if [ -d /tmp/senzii-site ]; then
      mkdir -p ${app_dir}/site
      rsync -a --delete --exclude=.git /tmp/senzii-site/ ${app_dir}/site/
    fi

  # Set ownership
  - chown -R www-data:www-data ${app_dir}

  # Enable and start services
  - |
    systemctl daemon-reload
    systemctl enable senzii-app senzii-mcp
    systemctl start senzii-app
    systemctl start senzii-mcp

  # Wait and verify
  - |
    sleep 3
    curl -sf http://127.0.0.1:80/health || echo "WARNING: app health check failed"

  # Clean up
  - rm -rf /tmp/senzii-site
