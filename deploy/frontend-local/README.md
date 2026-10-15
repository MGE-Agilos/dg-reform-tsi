# Web explorer on a local server

The explorer (`web/`: projects, CSRs, anomalies, *Chat* and *Impact* tabs) is published on GitHub Pages
(<https://mge-agilos.github.io/dg-reform-tsi/>). This folder lets you also serve it from a PC or an internal
server, with its own configuration. The data stays in Supabase: only the page is served locally.

| File | Role |
|---|---|
| `config.local.example.js` | configuration template, to copy to `config.local.js` (not in Git) |
| `docker-compose.yml`, `nginx.conf` | Docker mode: nginx serves `web/`, with `config.local.js` instead of `config.js` |
| `serve.ps1` | mode without Docker: Python's HTTP server on a copy of `web/` (`dist/`, not in Git) |

## 1. Configuration

```powershell
Copy-Item deploy\frontend-local\config.local.example.js deploy\frontend-local\config.local.js
notepad deploy\frontend-local\config.local.js
```

- `SUPABASE_URL` and `SUPABASE_ANON_KEY`: Supabase project (by default the same as GitHub Pages). The `anon` key
  is public by nature; **never** put the `service_role` key there.
- `PROXY_URL`: service behind the *Chat* and *Impact* tabs. Keep only one of the two lines:
  - `tsi-assistant`: Claude (Anthropic), on the Supabase data, as on GitHub Pages;
  - `haystack-proxy`: the `tsi-chat` and `tsi-impact` services of Haystack Enterprise (the client's LLM). The
    function must be deployed with its secrets (root README, section « Chat et impact (lot 2) », in French).

## 2. Start

**With Docker** (Docker Desktop or Docker Engine):

```powershell
cd deploy\frontend-local
docker compose up -d        # http://localhost:8080/
docker compose down         # stop
```

For another port, change `"8080:80"` in `docker-compose.yml`. After a change to `web/` or to `config.local.js`,
reloading the page is enough (the files are mounted, and `config.js` is never cached).

Create `config.local.js` **before** `docker compose up`: if the file is missing, Docker creates an empty
**folder** named `config.local.js` instead, and the page does not find its configuration. In that case: run
`docker compose down`, delete this folder, copy the example, and start again.

**Without Docker** (Python 3 is enough, the one in `.venv` if it exists):

In the PowerShell console (not ISE), first allow scripts in the window:
`Set-ExecutionPolicy -Scope Process Bypass` (or run `powershell -ExecutionPolicy Bypass -File
.\deploy\frontend-local\serve.ps1`). If you got the repository as a ZIP: run
`Get-ChildItem -Recurse -Filter *.ps1 | Unblock-File` once.

```powershell
.\deploy\frontend-local\serve.ps1                    # http://localhost:8000/
.\deploy\frontend-local\serve.ps1 -Port 8001
.\deploy\frontend-local\serve.ps1 -Bind 0.0.0.0      # reachable from the other computers of the network
```

The script copies `web/` to `dist/` each time it starts: start it again after a change. Ctrl+C to stop. Python's
server is fine for trials and a few testers; for a permanent service, prefer Docker.

## 3. Supabase: allow the local address

Without these settings, sign-in or the *Chat* and *Impact* tabs fail.

1. **Redirect URLs.** *Authentication > URL Configuration > Redirect URLs*: add the address of the page followed by
   `/**`, for example `http://localhost:8080/**` (Docker), `http://localhost:8000/**` (`serve.ps1`) or
   `https://<server>:<port>/**`. The magic link takes the user back to the address from which they requested it.
2. **Allowed origins of the Edge Functions.** By default, the `tsi-assistant` and `haystack-proxy` functions only
   accept `https://mge-agilos.github.io` and `http://localhost:8000`. For another address, set the
   `ALLOWED_ORIGINS` secret (*Edge Functions > Secrets*) with **all** the origins, separated by commas (the value
   replaces the default list), for example:
   `https://mge-agilos.github.io,http://localhost:8000,http://localhost:8080`. An origin is the scheme, the host and
   the port, without a path or a trailing `/`.

`http://localhost:8000` and `http://127.0.0.1:8000` are two different origins for the browser and for Supabase:
open the page at the address shown by the script (`http://localhost:<port>/`), the one declared above, and not
another way of writing the same machine.

## 4. On a real server

- Put **HTTPS** in front of it (reverse proxy: IIS, nginx, Caddy, Traefik… with a certificate). Over HTTP on an
  address other than `localhost`, the browser blocks some features (copying the impact report to the clipboard)
  and the magic link lands on an insecure page.
- Open the chosen port in the server's firewall only if the page must be reachable from other computers.

## Testers

Access is still managed in Supabase, as for GitHub Pages: invitation in *Authentication > Users*, then the e-mail
added to the `testers` table (root README, section « Explorateur web », in French). A local server gives access to
no data unless a tester signs in.
