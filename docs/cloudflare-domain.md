# Connecting your domain through Cloudflare

Cloudflare provides the domain, the TLS edge and, optionally, a tunnel and a login layer. The server itself runs on your VPS, PC, Railway or Render. Cloudflare Workers cannot run this stack, and the Deploy to Cloudflare button does not apply.

## Option A: DNS only, Caddy issues the certificate

The simplest and recommended first setup.

1. Add your domain to Cloudflare and move the registrar's nameservers to Cloudflare if you have not already.
2. **DNS → Records → Add record**: type `A`, name `brain`, IPv4 address = your VPS public IP, **Proxy status: DNS only** (grey cloud), TTL Auto. Add an `AAAA` record if the VPS has IPv6.
3. On the VPS open ports 80 and 443: `sudo ufw allow 80,443/tcp`.
4. In `.env` set `BRAIN_DOMAIN=brain.yourdomain.com` and `PUBLIC_URL=https://brain.yourdomain.com`.
5. `docker compose up -d`. Caddy requests a Let's Encrypt certificate over HTTP-01 on port 80. Check with `docker compose logs caddy`.
6. Verify: `curl -I https://brain.yourdomain.com/healthz` returns `200`. Open `/admin`.
7. Give agents `https://brain.yourdomain.com/mcp` and their keys. GitHub webhooks go to `https://brain.yourdomain.com/webhooks/<project_id>/github`.

## Option B: proxied through Cloudflare (orange cloud)

Adds Cloudflare's DDoS protection and WAF in front of the server.

1. Complete Option A first so Caddy holds a valid certificate.
2. Switch the record's proxy status to **Proxied**.
3. **SSL/TLS → Overview**: set mode to **Full (strict)**.
4. Either keep Let's Encrypt in Caddy (renewals need port 80 reachable, which Cloudflare proxies) or install a **Cloudflare Origin Certificate** (SSL/TLS → Origin Server → Create Certificate) and reference it in the `Caddyfile`:

```
brain.yourdomain.com {
    tls /certs/origin.pem /certs/origin.key
    reverse_proxy brain-server:8000 {
        flush_interval -1
        transport http {
            read_timeout 24h
            write_timeout 24h
        }
    }
}
```

   Mount the certificate files into the Caddy container with an extra `volumes` entry.
5. **Caching → Cache Rules**: add a rule for hostname `brain.yourdomain.com` with **Bypass cache**, so SSE streams and API responses are never cached.
6. The server sends a heartbeat every 20 seconds, which keeps SSE connections alive under Cloudflare's 100-second idle limit. Nothing to configure.

## Option C: Cloudflare Tunnel, no open ports

The VPS or PC makes an outbound connection to Cloudflare; nothing listens publicly.

1. **Zero Trust → Networks → Tunnels → Create a tunnel**, name it `brain`, choose Cloudflared, copy the token.
2. Public hostname: subdomain `brain`, domain `yourdomain.com`, service type `HTTP`, URL `brain-server:8000`.
3. Add to `docker-compose.yml`:

```yaml
  cloudflared:
    image: cloudflare/cloudflared:latest
    restart: unless-stopped
    command: tunnel --no-autoupdate run --token ${CLOUDFLARE_TUNNEL_TOKEN}
    networks:
      - brain-net
```

4. Remove the `caddy` service and the `80:80` and `443:443` port mappings. Set `PUBLIC_URL=https://brain.yourdomain.com` in `.env` and `CLOUDFLARE_TUNNEL_TOKEN=<token>`.
5. `docker compose up -d`. TLS terminates at Cloudflare; the certificate is automatic.

### What the tunnel ID is

Every tunnel in your Cloudflare account has a tunnel ID, a random code such as `6ff42ae2-765d-4adf-a9e5-c1e3d0b9c1a2`. Cloudflare assigns it when the tunnel is created. The DNS record for your hostname is a `CNAME` whose target is `<tunnel ID>.cfargotunnel.com`; that is how Cloudflare knows which tunnel, and so which machine, answers for the name. The console's `/domain` command creates the tunnel, receives the ID, writes the DNS record and keeps the credentials file in `~/.cloudflared/<tunnel ID>.json`. You never type the ID; you can see it under **Zero Trust → Networks → Tunnels** or in the DNS record's target.

### Keeping the tunnel up

`/domain` starts the tunnel inside the console. To keep it up after the console closes and after a reboot, accept the console's offer to install the background service, or run `brain service install` (`sudo brain service install --system` on a VPS). The service runs `brain serve`, which starts the named tunnel from `.env` every time.

On a local PC, `brain serve --tunnel` does the same with a temporary `trycloudflare.com` hostname and no account setup, useful for testing remote agents.

## Option D: Cloudflare Access in front of the admin panel

Adds an email or SSO login before anyone reaches `/admin`, while agents keep calling `/mcp` directly.

1. **Zero Trust → Access → Applications → Add an application → Self-hosted**.
2. Application domain: `brain.yourdomain.com`, path `admin`. Add a second application for path `api/admin`.
3. Policy: **Allow**, include **Emails** with your team's addresses, or your identity provider's group.
4. Save. The MCP, events and webhook paths are outside both applications and unaffected.

## Separate admin subdomain

If you host the dashboard file on Cloudflare Pages:

1. Create a Pages project from a repository containing `templates/admin_dashboard.html` renamed to `index.html`.
2. **DNS**: `CNAME admin → <project>.pages.dev`, proxied.
3. On the server set `CORS_ORIGINS=https://admin.yourdomain.com` and restart.
4. Open `https://admin.yourdomain.com`, expand **Advanced: server URL**, enter `https://brain.yourdomain.com`, sign in with the master key.

## Custom domain on Railway or Render

1. In the platform dashboard add `brain.yourdomain.com` as a custom domain. It shows a CNAME target.
2. **DNS**: `CNAME brain → <target>`, DNS only until the platform reports the certificate as issued, then optionally proxied with Full (strict).
3. Set `PUBLIC_URL=https://brain.yourdomain.com` in the platform's environment variables.

## One domain, many projects

Projects do not need their own hostnames. The key selects the project. If a client wants a vanity address, add `CNAME acme.brain → brain.yourdomain.com`; the server ignores the hostname.

## Checks

```bash
dig +short brain.yourdomain.com
curl -sS https://brain.yourdomain.com/healthz
curl -N -H "Authorization: Bearer <agent key>" https://brain.yourdomain.com/events | head -5
```

The last command should print `event: ready` within a second and a `: ping` line every 20 seconds.
