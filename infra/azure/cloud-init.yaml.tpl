#cloud-config

# ── Senzii App (Python/FastAPI) backend cloud-init ─────────────────────────
# Provisions: senzii user, Caddy, systemd services for web + MCP, UFW firewall.
# Database: Neon (external) — no PostgreSQL on the instance.
#
# Prod architecture (behind Azure Standard LB, TCP passthrough):
#   LB :443 (TCP passthrough) → Caddy :443 (TLS termination, auto-HTTPS)
#     ├─ :3000 FastAPI web app
#     └─ :3001 MCP server
#   LB :80 (TCP passthrough) → Caddy :80 (ACME challenges, redirect)
#
# Staging architecture (Tailscale only, no public HTTP):
#   Tailscale interface → Caddy :80 (plain HTTP, no TLS)
#     ├─ :3000 FastAPI web app
#     └─ :3001 MCP server
#   Public interface: SSH only (port 22). No public HTTP/HTTPS.

package_update: true
package_upgrade: true

packages:
  - caddy
  - ufw
  - curl
  - cifs-utils
  - python3
  - python3-venv
  - python3-pip
  - git

write_files:

  # ── Environment file (root-owned until runcmd chowns it) ─────────────────
  - path: ${app_dir}/.env
    owner: root:root
    permissions: "0640"
    content: |
      DATABASE_URL=${database_url}
      SESSION_SECRET=${session_secret}
      PORT=3000
      BASE_URL=${base_url}
      RESEND_API_KEY=${resend_api_key}
      MAIL_FROM=${mail_from}
      NOTIFY_EMAIL=${notify_email}
      MCP_PORT=${mcp_port}
      ORG_ID=${mcp_org_id}

  # ── systemd unit for Senzii web app ─────────────────────────────────────
  - path: /etc/systemd/system/senzii-app.service
    content: |
      [Unit]
      Description=Senzii FastAPI App
      After=network.target
      Wants=network-online.target

      [Service]
      Type=exec
      User=senzii
      Group=senzii
      WorkingDirectory=${app_dir}
      EnvironmentFile=${app_dir}/.env
      ExecStart=${app_dir}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 3000 --workers 2
      Restart=always
      RestartSec=5
      StandardOutput=journal
      StandardError=journal

      NoNewPrivileges=true
      ProtectSystem=strict
      ProtectHome=true
      PrivateTmp=true
      ReadWritePaths=${app_dir}

      [Install]
      WantedBy=multi-user.target

  # ── systemd unit for Senzii MCP server ───────────────────────────────────
  - path: /etc/systemd/system/senzii-mcp.service
    content: |
      [Unit]
      Description=Senzii MCP Server
      After=network.target senzii-app.service
      Wants=network-online.target

      [Service]
      Type=exec
      User=senzii
      Group=senzii
      WorkingDirectory=${app_dir}
      EnvironmentFile=${app_dir}/.env
      ExecStart=${app_dir}/.venv/bin/python -m mcp_server.main
      Restart=always
      RestartSec=5
      StandardOutput=journal
      StandardError=journal

      NoNewPrivileges=true
      ProtectSystem=strict
      ProtectHome=true
      PrivateTmp=true
      ReadWritePaths=${app_dir}

      [Install]
      WantedBy=multi-user.target

  # ── CIFS credentials for shared Caddy cert storage (prod only) ─────────────
  - path: /etc/cifs-credentials
    owner: root:root
    permissions: "0600"
    content: |
      username=${storage_account_name}
      password=${storage_account_key}

  # ── Caddyfile ──────────────────────────────────────────────────────────────
%{ if env == "staging" }
  # Staging: plain HTTP on all interfaces (Tailscale provides the encrypted tunnel).
  # No public HTTPS — the server is only reachable via Tailscale.
  - path: /etc/caddy/Caddyfile
    content: |
      :80 {
        reverse_proxy localhost:3000

        header {
          X-Content-Type-Options nosniff
          X-Frame-Options DENY
          X-XSS-Protection "1; mode=block"
          Referrer-Policy strict-origin-when-cross-origin
        }

        @hashed path /css/*.*.*.css /js/*.*.*.js
        header @hashed Cache-Control "public, max-age=31536000, immutable"

        @static path *.css *.js *.png *.jpg *.svg *.ico *.woff *.woff2
        header @static Cache-Control "public, max-age=86400"

        handle /health {
          reverse_proxy localhost:3000
        }

        handle /mcp* {
          reverse_proxy localhost:3001
        }
      }
%{ else }
  # Prod: auto-HTTPS via Let's Encrypt (Azure LB passes TCP through).
  - path: /etc/caddy/Caddyfile
    content: |
      ${domain} {
        reverse_proxy localhost:3000

        header {
          X-Content-Type-Options nosniff
          X-Frame-Options DENY
          X-XSS-Protection "1; mode=block"
          Referrer-Policy strict-origin-when-cross-origin
          Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
        }

        @hashed path /css/*.*.*.css /js/*.*.*.js
        header @hashed Cache-Control "public, max-age=31536000, immutable"

        @static path *.css *.js *.png *.jpg *.svg *.ico *.woff *.woff2
        header @static Cache-Control "public, max-age=86400"

        handle /health {
          reverse_proxy localhost:3000
        }

        handle /mcp* {
          reverse_proxy localhost:3001
        }
      }
%{ endif }

runcmd:
  # ── 1. Create senzii user and fix permissions ────────────────────────────
  - ['useradd', '-r', '-s', '/bin/false', '-d', '${app_dir}', 'senzii']
  - ['mkdir', '-p', '${app_dir}/static']
  - ['chown', '-R', 'senzii:senzii', '${app_dir}']
  - ['chmod', '640', '${app_dir}/.env']

  # ── 2a. Staging: install and join Tailscale ──────────────────────────────
%{ if env == "staging" }
  - ['sh', '-c', 'curl -fsSL https://tailscale.com/install.sh | sh']
  - ['sh', '-c', 'tailscale up --authkey=${tailscale_auth_key} --hostname=senzii-staging']
%{ endif }

  # ── 2b. Mount shared Caddy cert storage (prod only) ──────────────────────
%{ if env != "staging" }
  - ['sh', '-c', 'mkdir -p /var/lib/caddy/.local/share/caddy']
  - ['sh', '-c', 'mount -t cifs //${storage_account_name}.file.core.windows.net/caddy-certs /var/lib/caddy/.local/share/caddy -o credentials=/etc/cifs-credentials,vers=3.0,uid=999,gid=988,dir_mode=0755,file_mode=0644,serverino,noforceuid,noforcegid']
  - ['sh', '-c', 'echo "//${storage_account_name}.file.core.windows.net/caddy-certs /var/lib/caddy/.local/share/caddy cifs credentials=/etc/cifs-credentials,vers=3.0,uid=999,gid=988,dir_mode=0755,file_mode=0644,serverino,noforceuid,noforcegid,_netdev 0 0" >> /etc/fstab']
  - ['sh', '-c', 'chown -R caddy:caddy /var/lib/caddy/.local/share/caddy']
%{ endif }

  # ── 2c. Start Caddy ──────────────────────────────────────────────────────
  - ['systemctl', 'enable', 'caddy']
  - ['systemctl', 'restart', 'caddy']

  # ── 3. Clone the app repo and set up the Python venv ─────────────────────
  # .env is written above; move it aside so git clone into an empty dir works,
  # then restore it (it's gitignored so it won't be overwritten by the clone).
  - ['sh', '-c', 'mv ${app_dir}/.env /tmp/senzii.env 2>/dev/null || true']
  - ['sh', '-c', 'git clone --branch "${repo_branch}" "${repo_url}" ${app_dir} || echo "Clone failed — will retry on deploy"']
  - ['sh', '-c', 'mv /tmp/senzii.env ${app_dir}/.env 2>/dev/null || true']
  - ['sh', '-c', 'cd ${app_dir} && python3 -m venv .venv && .venv/bin/pip install --upgrade pip --quiet && .venv/bin/pip install -r requirements.txt --quiet']
  - ['chown', '-R', 'senzii:senzii', '${app_dir}']

  # ── 4. Enable services ───────────────────────────────────────────────────
  - ['systemctl', 'daemon-reload']
  - ['systemctl', 'enable', 'senzii-app']
  - ['systemctl', 'enable', 'senzii-mcp']
  - ['systemctl', 'start', 'senzii-app']
  - ['systemctl', 'start', 'senzii-mcp']

  # ── 5. Configure firewall ────────────────────────────────────────────────
%{ if env == "staging" }
  - ['ufw', 'allow', '22/tcp']
  - ['ufw', 'allow', 'in', 'on', 'tailscale0']
  - ['ufw', 'deny', '80/tcp']
  - ['ufw', 'deny', '443/tcp']
  - ['ufw', '--force', 'enable']
  - ['ufw', 'status', 'verbose']
%{ else }
  - ['ufw', 'allow', '22/tcp']
  - ['ufw', 'allow', '80/tcp']
  - ['ufw', 'allow', '443/tcp']
  - ['ufw', '--force', 'enable']
  - ['ufw', 'status', 'verbose']
%{ endif }

  # ── 6. Done ───────────────────────────────────────────────────────────────
  - ['echo', 'Senzii ${env} backend provisioned. Deploy code with: ./infra/deploy.sh azure']
