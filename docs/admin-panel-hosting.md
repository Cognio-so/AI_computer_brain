# Hosting the admin panel

## Default: inside the server

`templates/admin_dashboard.html` is served by the same process at `/admin`. Wherever the server runs, the panel is there:

| Server runs on | Panel URL |
|---|---|
| Your PC | `http://localhost:8000/admin` |
| VPS with Caddy | `https://brain.yourdomain.com/admin` |
| Railway | `https://<app>.up.railway.app/admin` |
| Render | `https://<service>.onrender.com/admin` |
| Fly.io | `https://<app>.fly.dev/admin` |

Nothing extra to deploy. The panel calls `/api/admin/*` on its own origin and authenticates with the master key you type at login. The key is kept in the browser's session storage and never written to disk by the server.

### Is a public admin URL a risk?

Anyone who guesses `brain.yourdomain.com/admin` sees a login screen and nothing else. Every admin API call returns 401 without the master key, and keys are never embedded in the page. Three settings close the remaining gap between "visible" and "reachable":

| Setting | Effect |
|---|---|
| `ADMIN_PATH=/admin-7f3a9c` | Serves the panel at a path nobody can guess. The root URL redirects there, so set it before sharing links. |
| `ADMIN_ALLOWED_IPS=203.0.113.0/24,198.51.100.7` | Only these addresses or ranges can load the panel or call the admin API. Everyone else gets 403. Works behind Caddy because the server honours forwarded client addresses. |
| `ADMIN_LOGIN_FAILURES_PER_MINUTE=10` | After this many wrong keys from one address in a minute, that address is locked out of the admin API for a minute. |

Agents are unaffected by all three: `/mcp`, `/sse` and `/events` authenticate with their own keys.

### Extra protection

Restrict the admin paths by IP in the `Caddyfile`:

```
brain.yourdomain.com {
    @admin path /admin* /api/admin*
    handle @admin {
        @allowed remote_ip 203.0.113.0/24 198.51.100.7
        handle @allowed {
            reverse_proxy brain-server:8000 { flush_interval -1 }
        }
        respond 403
    }
    reverse_proxy brain-server:8000 {
        flush_interval -1
        transport http {
            read_timeout 24h
            write_timeout 24h
        }
    }
}
```

Or put Cloudflare Access in front of `/admin` and `/api/admin`, see [cloudflare-domain.md](cloudflare-domain.md).

## Optional: host the panel on another origin

The dashboard is a single static file that only talks to the admin API and the events stream. It can live on Cloudflare Pages, Vercel, Netlify, GitHub Pages or a Railway static service while the server stays where it is.

On the server:

```
CORS_ORIGINS=https://admin.yourdomain.com
```

In the panel: expand **Advanced: server URL** on the login screen and enter the server's base URL, for example `https://brain.yourdomain.com`. The value is remembered in the browser. To bake it in, set the meta tag in the file:

```html
<meta name="brain-api-base" content="https://brain.yourdomain.com">
```

The master key is still typed by the administrator and sent as a bearer token. Never embed it in the static file.

### Deploying the static file

- **Cloudflare Pages**: a repository with the file as `index.html`, build command empty, output directory `/`.
- **Vercel or Netlify**: drag and drop the file, or a repository with the file as `index.html`.
- **Railway**: a static service pointing at a directory that contains `index.html`.
- **GitHub Pages**: a branch with the file as `index.html`.

### When this is worth it

- You want the panel behind an SSO provider or Cloudflare Access on its own domain while `/mcp` stays open to agents.
- You manage several brain servers and want one panel URL that can switch between them.

### What is not possible

The panel alone, without a running server. It has no backend of its own.

## Panel features

| Tab | What you can do |
|---|---|
| Keys | Create a key per agent with a one-time reveal and config snippets, revoke keys, see last use and sync |
| Files | Read the six files rendered, edit a section or a whole file with conflict detection |
| Activity | Live stream of every change with actor, target, summary and the Git diff |
| Agents | Live sessions and per-key sync health with stale flags |
| Approvals | Approve or reject writes from roles with `rw-approve` |
| Rules | Edit the Agent Rules, File Guide and Sync Policy served to agents |
| Settings | Display name, Git remote and branch, webhook secret and URL, export and import, delete |

The first time you sign in with no projects, a wizard creates the first project and key and shows the connection snippets.
