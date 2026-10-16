# Deploying on a VPS

A 1 vCPU, 1 GB VPS is enough for dozens of agents. Ubuntu 22.04 or 24.04 is assumed; any Linux with Docker works.

## 1. Prepare the server

```bash
sudo apt update && sudo apt install -y git ufw
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable
```

## 2. DNS

Create an `A` record `brain.yourdomain.com` pointing at the VPS public IP. With Cloudflare, set proxy status to **DNS only** for the first start so Caddy can complete the certificate challenge. Details and the proxied, Tunnel and Access variants are in [cloudflare-domain.md](cloudflare-domain.md).

## 3. Configure and start

```bash
git clone https://github.com/Cognio-so/AI_computer_brain && cd AI_computer_brain
cp .env.example .env
```

Edit `.env`:

```
BRAIN_DOMAIN=brain.yourdomain.com
PUBLIC_URL=https://brain.yourdomain.com
ADMIN_MASTER_KEY=<long random string, e.g. openssl rand -base64 36>
```

Then:

```bash
docker compose up -d
docker compose logs -f caddy      # wait for "certificate obtained"
curl -I https://brain.yourdomain.com/healthz
```

Open `https://brain.yourdomain.com/admin`, sign in with the master key, and follow the wizard.

## What runs

| Container | Role |
|---|---|
| `ai-computer-brain` | The server: MCP, admin API, dashboard, webhooks, Git worker |
| `brain-redis` | Pub/sub and locks for multi-worker setups. Harmless with one worker. |
| `brain-caddy` | HTTPS termination with automatic certificates and unbuffered SSE |

Data lives in the named volume `brain_data` (metadata database) and the bind mount `./brain_files` (one Git repository per project).

## Updating

```bash
git pull
docker compose up -d --build
```

Schema changes are applied on startup. Vaults are untouched by upgrades.

## PostgreSQL

Native install:

```bash
sudo apt install -y postgresql
sudo -u postgres psql -c "CREATE USER brain WITH PASSWORD 'change-me';"
sudo -u postgres psql -c "CREATE DATABASE second_brain OWNER brain;"
```

Allow the Docker network in `postgresql.conf` (`listen_addresses = '*'`) and `pg_hba.conf` (`host second_brain brain 172.16.0.0/12 scram-sha-256`), restart PostgreSQL, then in `.env`:

```
DATABASE_URL=postgresql://brain:change-me@host.docker.internal:5432/second_brain
```

Add `extra_hosts: ["host.docker.internal:host-gateway"]` to the `brain-server` service if your Docker version does not provide it. Restart with `docker compose up -d`.

Container alternative: add a `postgres:16` service with a volume and point `DATABASE_URL` at `postgres:5432`.

## More than one worker

Set `REDIS_URL=redis://redis:6379/0` (already the compose default) and start with `brain serve --workers 2`, for example by overriding the container command. Locks and events then span workers.

## Backups

- Set a Git remote per project in **Settings** so every change is pushed off the box.
- Nightly metadata dump:

```bash
docker compose exec brain-server sh -c 'sqlite3 /data/brain_metadata.db ".backup /data/backup.db"'
docker run --rm -v ai-computer-brain_brain_data:/data -v $PWD:/out alpine tar czf /out/brain-data-$(date +%F).tgz /data
```

With PostgreSQL use `pg_dump second_brain`.

- `brain vault export <project> --out vault.zip` for a portable snapshot of one project.

## Monitoring

`GET /healthz` returns `{"ok": true}` when the database answers. `brain doctor` inside the container checks Git, templates, the database, Redis and the public URL. Container logs are JSON-free single lines suitable for any log shipper.

## Hardening

- Restrict `/admin` and `/api/admin` by IP in the `Caddyfile`, or put Cloudflare Access in front of those paths.
- Rotate the master key by changing `ADMIN_MASTER_KEY` and restarting; agent keys are unaffected.
- Keep `GIT_PUSH_ENABLED=true` only when remotes use deploy tokens with the narrowest scope.
- Review `ACCESS_PERMISSIONS.md` per project; consider `rw-approve` on pricing and permissions sections.

## Without Docker

Install the package (`pipx install ai-computer-brain` or `uv tool install .` from a clone), create the working directory with `.env` (`brain init` or the console wizard), then register the boot-time service:

```bash
sudo brain service install --system      # systemd system service, runs as your user
brain service status                     # or: systemctl status ai-computer-brain
brain service logs                       # data/logs/brain.log
```

The service runs `brain serve`, which also starts the Cloudflare tunnel when `CLOUDFLARE_TUNNEL` is set, so `/domain` from the console gives a VPS its HTTPS hostname without Caddy or open ports. On a desktop or laptop, `brain service install` without `--system` registers a user service (Linux), a launchd agent (macOS) or a logon scheduled task (Windows).
