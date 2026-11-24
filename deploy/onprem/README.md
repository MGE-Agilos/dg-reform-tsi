# TSI × CSR Explorer — self-hosted installation (Linux)

> **Status: first release, steps 1 to 9 ready and tested.** Database, data transfer, PostgREST, application service,
> web page and users work as described (tested end to end on PostgreSQL 16, PostgREST 12.2.3 and Node.js 22).
> Still **(planned)**: the assistant on GPT@EC and the automatic monthly data update (steps 10 and 11); the explorer
> works without them in the meantime. Sign-in with your identity provider is tested at the installation.
>
> The repository is public: no GitHub account is needed. The server only needs HTTPS access to `github.com`
> (or download the code as a ZIP and copy it to the server).

Server preparation (to do first): [REQUIREMENTS.md](REQUIREMENTS.md).

## Contents

- [Status of the components](#status-of-the-components)
- [Architecture](#architecture)
- [Decisions to confirm](#decisions-to-confirm)
- [Values used in this procedure](#values-used-in-this-procedure)
- Steps:
  [1. Check the server](#step-1--check-the-server) ·
  [2. Create the database roles](#step-2--create-the-database-roles) ·
  [3. Get the code](#step-3--get-the-code) ·
  [4. Install the database schema](#step-4--install-the-database-schema) ·
  [4b. Load the current data](#step-4b--load-the-current-data) ·
  [5. Install PostgREST](#step-5--install-postgrest) ·
  [6. Install the application service](#step-6--install-the-application-service-nodejs) ·
  [7. Publish the web page](#step-7--publish-the-web-page) ·
  [8. Configure nginx](#step-8--configure-nginx) ·
  [9. Users and administrators](#step-9--users-and-administrators) ·
  [10. First data load](#step-10--first-data-load) ·
  [11. Monthly update](#step-11--monthly-update) ·
  [12. Final checks](#step-12--final-checks)
- [Operations: backups, upgrades, logs](#operations)
- [Optional: Haystack Enterprise](#optional-haystack-enterprise)

## Status of the components

| Component | Status |
|---|---|
| Data pipeline (projects, publications, recommendations, recitals, country reports, links, anomalies) | Ready. Runs with GPT@EC (`llm_provider: openai`). |
| Web page (`web/`) with `deploy/onprem/config.js` | Ready. Signs in through the application service. Tested end to end on this stack. |
| Database schema (`deploy/onprem/sql/000_compat.sql`, then `supabase/migrations/001` to `010`) | Ready. Tested on PostgreSQL 16: schema, row-level security, search and dashboard functions. |
| Transfer of the current data (SQL file provided by Agilos) | Ready. Tested: same rows and same content as the cloud database. |
| Application service (`server/`): sign-in, data API, administration | Ready. Tested end to end with the test sign-in; OpenID Connect tested against a simulated identity provider only. |
| Assistant on GPT@EC (questions, impact analyses, briefings, related search terms) | **(planned)**. Until then the page works without it (the assistant answers "not available yet"). |
| Data load into a self-hosted database (`supabase-sync` to a local PostgREST) | **(planned)** |
| Linux update script and service files (`deploy/onprem/`) | **(planned)** |
| Export to Haystack Enterprise (`prod-export`, `deploy/prod/`) | Ready. |

## Architecture

```
 Users' browsers ──HTTPS 443──▶ Linux application server ──TCP 5432──▶ PostgreSQL server (tsi_explorer)
                                 │  nginx: web page (static files), /api ──▶ application service (Node.js, 127.0.0.1:3000)
                                 │  application service ──▶ PostgREST (127.0.0.1:3001) ──▶ database
                                 │  monthly data update (Python, cron) ──▶ PostgREST ──▶ database
                                 └──HTTPS 443──▶ *.europa.eu (data sources), GPT@EC (AI model)
```

- **nginx** serves the page and forwards `/api/` to the application service. Nothing else is exposed.
- **The application service** signs users in, checks that they are authorised users, gives the page a short-lived
  token and forwards its data requests to PostgREST. It also runs the assistant (questions, impact analyses,
  briefings, related search terms) with GPT@EC.
- **PostgREST** turns the page's data requests into SQL. It listens on `127.0.0.1` only. The database's row-level
  security (RLS) policies decide what each request may read or write, exactly as today.
- **The monthly update** downloads the public sources, uses GPT@EC to extract and link them, and replaces the
  public data in the database. Users' reviews, usage data and impact analyses run from the page are kept.

With GPT@EC, the impact analysis has no web search: it relies on the explorer's data (project, publications,
recommendations, recitals, country reports).

## Decisions to confirm

| Decision | Assumed in this document | Alternative |
|---|---|---|
| Sign-in | OpenID Connect with your identity provider (for example EU Login or Microsoft Entra ID): users sign in with their usual account. | Windows integrated sign-in (Kerberos) through nginx; heavier on Linux. |
| Authorised users | A list of e-mail addresses in the database, managed by the explorer's administrators. | An identity-provider group. |
| Database role names | `anon`, `authenticated`, `service_role` (the names used by the schema). | Other names, if they clash with roles already in your PostgreSQL cluster (roles are cluster-wide). |
| Schema of the tables | `public`. | Another schema, for example `tsi_explorer`: adapted copies of the scripts ([step 4](#step-4--install-the-database-schema)). |
| `service_role` attribute `BYPASSRLS` | Given by a superuser. | Without a superuser: one rule per table gives the same access ([step 4](#step-4--install-the-database-schema), `900_service_role.sql`). |

## Values used in this procedure

Replace them with yours.

| Placeholder | Example | Meaning |
|---|---|---|
| `<dns>` | `tsi-explorer.example.eu` | DNS name of the site |
| `<db-host>` | `pg01.example.eu` | PostgreSQL server |
| `<db-owner>` | `tsi_owner` | owner account of the database |
| `<schema>` | `public` or `tsi_explorer` | schema of the explorer's tables |
| `<release>` | `v2026.12.0` | release tag of the code |

Secrets (database passwords, token secret, GPT@EC key, OpenID Connect client secret) are only written in files
readable by the `tsi` account (`chmod 600`), never in Git, never in the web folder.

---

## Step 1 — Check the server

As an administrator on the application server:

```bash
node --version            # v22.x
python3 --version         # 3.12 or later
nginx -v
psql --version
id tsi && ls -ld /opt/tsi-explorer
curl -sI https://reform-support.ec.europa.eu | head -1        # HTTP/2 200 (or 30x)
curl -sI https://api.tech.ec.europa.eu | head -1              # any HTTP answer: the gateway is reachable
psql "host=<db-host> dbname=tsi_explorer user=<db-owner>" -c "select version();"
```

Every command must answer. If not, see [REQUIREMENTS.md](REQUIREMENTS.md).

## Step 2 — Create the database roles

On the PostgreSQL server, as a superuser (or by your DBA). Generate two strong passwords first (for example
`openssl rand -base64 32`).

```sql
-- Roles used by the schema's grants and row-level security policies (no login).
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;

-- Login role of PostgREST: it switches to one of the roles above for each request.
create role tsi_authenticator login noinherit password '<authenticator-password>';
grant anon, authenticated, service_role to tsi_authenticator;

-- Owner of the database (if not created yet) and database.
create role tsi_owner login password '<owner-password>';
create database tsi_explorer owner tsi_owner;
```

Allow the application server in `pg_hba.conf` for `tsi_authenticator` and `tsi_owner` on `tsi_explorer`
(`hostssl` preferred), then reload PostgreSQL.

`bypassrls` needs a superuser. On a managed PostgreSQL without one, create `service_role` without it
(`create role service_role nologin;`) and run `900_service_role.sql` at step 4: it gives the same access. If the
attribute is added later (`alter role service_role bypassrls;`), nothing else changes.

If the tables must be in a schema other than `public` (for example `tsi_explorer`), create it now, owned by the
owner account: `create schema tsi_explorer authorization tsi_owner;`, then follow the variant of step 4.

## Step 3 — Get the code

As `tsi` on the application server:

```bash
cd /opt/tsi-explorer
git clone https://github.com/MGE-Agilos/dg-reform-tsi.git app
cd app
git checkout <release>
```

Without access to `github.com` from the server: download
<https://github.com/MGE-Agilos/dg-reform-tsi/archive/refs/heads/main.zip> on a workstation, copy it to the server
and unpack it in `/opt/tsi-explorer/app` (`git` is then not needed; the upgrades are done the same way).

## Step 4 — Install the database schema

As `tsi` on the application server, with the owner account:

```bash
cd /opt/tsi-explorer/app
export PGHOST=<db-host> PGDATABASE=tsi_explorer PGUSER=<db-owner>   # the password is asked, or read from ~/.pgpass
psql -v ON_ERROR_STOP=1 -f deploy/onprem/sql/000_compat.sql         # see below
for f in supabase/migrations/0*.sql; do
  echo "== $f"; psql -v ON_ERROR_STOP=1 -f "$f" || break
done
psql -v ON_ERROR_STOP=1 -f deploy/onprem/sql/900_service_role.sql   # only if service_role has no BYPASSRLS
psql -c "notify pgrst, 'reload schema';"
```

**Variant: schema other than `public`.** The scripts are written for `public`. Write adapted copies (only the
schema references change; the script stops if it meets one it does not know), then run the copies instead:

```bash
python3 deploy/onprem/set_schema.py tsi_explorer --out ~/tsi-sql
for f in ~/tsi-sql/0*.sql; do
  echo "== $f"; psql -v ON_ERROR_STOP=1 -f "$f" || break
done
psql -v ON_ERROR_STOP=1 -f ~/tsi-sql/900_service_role.sql   # only if service_role has no BYPASSRLS
psql -c "notify pgrst, 'reload schema';"
```

Do not replace `public` by hand: the grants also name the role `PUBLIC` (`revoke ... from public`), which must
stay. In step 5, set `db-schemas` to the same schema.

`000_compat.sql` provides, on a plain PostgreSQL, the few objects the schema expects from the cloud service: the
`auth` schema with `auth.uid()`, `auth.jwt()` and `auth.role()` (read from the token claims that PostgREST sets
for each request) and an `auth.users` view of the signed-in user. The migrations then run unchanged.

Check:

```bash
psql -c "select count(*) from information_schema.tables where table_schema = '<schema>';"   # 15 or more
psql -c "select count(*) from pg_policies where schemaname = '<schema>' and policyname = 'service_role_all';"
# 14 if 900_service_role.sql was run, 0 otherwise
```

`900_service_role.sql` must be run again after every upgrade that adds tables (see [Operations](#operations)).

### Step 4b — Load the current data

Agilos sends a file `tsi-explorer-data.sql` (about 32 MB, in a zip) with all the current data of the explorer:
projects, recommendations, recitals, country reports, links, anomalies, impact analyses, and also the users, their
reviews and the usage data. It contains personal data (users' e-mail addresses): it is sent through a secure
channel and must not be stored in a shared folder.

As `tsi`, with the owner account (same `PG*` variables as above):

```bash
psql -v ON_ERROR_STOP=1 -f tsi-explorer-data.sql
```

With a schema other than `public`, adapt the file first and load the copy:

```bash
python3 deploy/onprem/set_schema.py tsi_explorer --out ~/tsi-sql --data tsi-explorer-data.sql
psql -v ON_ERROR_STOP=1 -f ~/tsi-sql/tsi-explorer-data.sql
```

Do not use a plain search and replace on this file: the texts of some projects contain "public.", which must stay.

It runs in one transaction (about 10 seconds): it empties the explorer's tables, loads every row, sets the id
counters and ends with a table of row counts, where `rows` must equal `expected` on every line. It can be run
again: the previous rows are replaced. Delete the file afterwards.

With this file, the database is complete without running the first data load (step 10): the monthly update
(step 11) then only adds what changed. The file is produced by Agilos with
`python deploy/onprem/export_data.py --out tsi-explorer-data.sql`.

## Step 5 — Install PostgREST

As an administrator on the application server:

```bash
cd /tmp
curl -fsSLO https://github.com/PostgREST/postgrest/releases/download/v12.2.3/postgrest-v12.2.3-linux-static-x64.tar.xz
tar -xJf postgrest-v12.2.3-linux-static-x64.tar.xz
sudo install -m 755 postgrest /usr/local/bin/postgrest
postgrest --version
```

Generate the token secret shared by PostgREST and the application service (at least 32 characters):

```bash
sudo install -d -o tsi -g tsi -m 700 /etc/tsi-explorer
openssl rand -base64 48 | tr -d '\n' | sudo -u tsi tee /etc/tsi-explorer/jwt-secret >/dev/null
openssl rand -base64 48 | tr -d '\n' | sudo -u tsi tee /etc/tsi-explorer/session-secret >/dev/null   # step 6
sudo chmod 600 /etc/tsi-explorer/jwt-secret /etc/tsi-explorer/session-secret
```

`/etc/tsi-explorer/postgrest.conf` (owner `tsi`, mode `600`):

```ini
db-uri = "postgres://tsi_authenticator:<authenticator-password>@<db-host>:5432/tsi_explorer?sslmode=require"
db-schemas = "<schema>"
db-anon-role = "anon"
server-host = "127.0.0.1"
server-port = 3001
jwt-secret = "@/etc/tsi-explorer/jwt-secret"
jwt-aud = "authenticated"
db-pool = 10
```

`/etc/systemd/system/tsi-postgrest.service`:

```ini
[Unit]
Description=TSI Explorer - PostgREST
After=network-online.target
Wants=network-online.target

[Service]
User=tsi
ExecStart=/usr/local/bin/postgrest /etc/tsi-explorer/postgrest.conf
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tsi-postgrest
curl -s http://127.0.0.1:3001/projects?limit=1     # [] or 401: PostgREST answers; no data for anonymous requests
```

## Step 6 — Install the application service (Node.js)

The service is in `server/` of the repository (Node.js, one dependency: `openid-client`). It signs users in with
your identity provider (OpenID Connect, authorization code with PKCE), keeps the session in an HttpOnly cookie,
gives the page a token valid 15 minutes for its data requests and forwards them to PostgREST.

As `tsi`:

```bash
cd /opt/tsi-explorer/app/server
npm ci --omit=dev
```

`/etc/tsi-explorer/server.env` (owner `tsi`, mode `600`):

```ini
PORT=3000
PUBLIC_URL=https://<dns>
POSTGREST_URL=http://127.0.0.1:3001
JWT_SECRET_FILE=/etc/tsi-explorer/jwt-secret
SESSION_SECRET_FILE=/etc/tsi-explorer/session-secret   # 32 characters or more, like jwt-secret
SESSION_HOURS=10                                       # a user signs in again after 10 hours

# Sign-in (OpenID Connect): register the site with your identity provider, redirect URI
# https://<dns>/api/auth/callback, scopes openid email profile
AUTH_MODE=oidc
OIDC_ISSUER=https://<identity-provider>/...            # its discovery document: <issuer>/.well-known/openid-configuration
OIDC_CLIENT_ID=<client id>
OIDC_CLIENT_SECRET=<client secret>
# OIDC_EMAIL_CLAIM=email                               # claim of the ID token that holds the e-mail address

# Assistant (planned): address of the assistant service once installed
# ASSISTANT_URL=http://127.0.0.1:3002

# Outbound proxy, if any
# HTTPS_PROXY=http://<proxy>:<port>
```

`/etc/systemd/system/tsi-explorer.service`:

```ini
[Unit]
Description=TSI Explorer - application service
After=network-online.target tsi-postgrest.service
Wants=network-online.target

[Service]
User=tsi
WorkingDirectory=/opt/tsi-explorer/app/server
EnvironmentFile=/etc/tsi-explorer/server.env
ExecStart=/usr/bin/node index.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tsi-explorer
curl -s http://127.0.0.1:3000/api/health      # {"ok":true,"database":true,"assistant":false,"auth":"oidc"}
```

## Step 7 — Publish the web page

As an administrator:

```bash
sudo install -d -m 755 /var/www/tsi-explorer
sudo rsync -a --delete /opt/tsi-explorer/app/web/ /var/www/tsi-explorer/
```

Then replace the page's `config.js` with the one of the self-hosted installation, which points the page to the
application service (`/api` on the same site) instead of the cloud service:

```bash
sudo cp /opt/tsi-explorer/app/deploy/onprem/config.js /var/www/tsi-explorer/config.js
```

The page holds no key and no secret: it signs in through the application service.

## Step 8 — Configure nginx

`/etc/nginx/sites-available/tsi-explorer` (on RHEL: `/etc/nginx/conf.d/tsi-explorer.conf`):

```nginx
server {
    listen 80;
    server_name <dns>;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name <dns>;

    ssl_certificate     /etc/ssl/certs/<dns>.crt;
    ssl_certificate_key /etc/ssl/private/<dns>.key;

    root /var/www/tsi-explorer;
    index index.html;

    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    add_header X-Frame-Options DENY always;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 300s;          # impact analyses and briefings take up to a few minutes
        client_max_body_size 2m;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/tsi-explorer /etc/nginx/sites-enabled/   # Debian/Ubuntu only
sudo nginx -t && sudo systemctl reload nginx
curl -sI https://<dns>/ | head -1        # HTTP/2 200
```

## Step 9 — Users and administrators

Only the e-mail addresses in the `testers` table can open the explorer. The data file of step 4b brings the
current users and administrators; migration 007 alone only creates a default administrator (Agilos). Add your
administrator, then remove the others if you wish.

```bash
psql -c "insert into public.testers (email, role) values ('first.admin@example.eu', 'admin')
         on conflict (email) do update set role = 'admin';"
```

The administrators then add the other users from the page (**Admin** menu). E-mail addresses are stored in lower
case.

## Step 10 — First data load

As `tsi`. The first load takes about 3 to 4 hours (polite download of the Commission's pages, at least 2 seconds
between two requests); the monthly updates take a few minutes to half an hour.

```bash
cd /opt/tsi-explorer/app
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
cp deploy/updater/settings.updater.yaml config/settings.yaml   # llm_provider: openai, GPT@EC endpoint
```

`/opt/tsi-explorer/app/.env` (owner `tsi`, mode `600`):

```ini
OPENAI_API_KEY=<GPT@EC key>
# Database load through the local PostgREST (planned settings)
TSI_REST_URL=http://127.0.0.1:3001
TSI_JWT_SECRET_FILE=/etc/tsi-explorer/jwt-secret
```

Check the AI model, then run the steps in this order:

```bash
.venv/bin/python -m tsi_agent llm-check                 # one short call to GPT@EC
.venv/bin/python -m tsi_agent run --recheck-all         # projects (about 2 hours the first time)
.venv/bin/python -m tsi_agent deliverables              # publications (about 1 hour the first time)
.venv/bin/python -m tsi_agent csr-fetch                 # recommendations (ECFIN database)
.venv/bin/python -m tsi_agent csr-build                 # recommendation files and 2026 recitals
.venv/bin/python -m tsi_agent country-reports           # 2026 country reports
.venv/bin/python -m tsi_agent csr-link                  # policy areas, then project -> recommendation links
.venv/bin/python -m tsi_agent anomalies
.venv/bin/python -m tsi_agent supabase-sync             # load into the database (planned: through TSI_REST_URL)
```

Each step can be run again: it resumes where it stopped and only processes what changed. Logs are in `logs/`.

If the database was loaded from Agilos's file (step 4b), the database is already complete; the update still needs
its working files (`output/`) to know what is new. Agilos provides them with the data file (unpack them in
`/opt/tsi-explorer/app/output/`), or run the steps above once: they rebuild the files and reload the same data.

## Step 11 — Monthly update

**(planned)** `deploy/onprem/update.sh` runs the steps of step 10 in order (without `--recheck-all`), stops on
errors that would publish incomplete data, and writes a summary in `logs/`. It is the Linux version of the
Windows updater described in [deploy/updater/README.md](../updater/README.md).

As `tsi`, `crontab -e`:

```cron
# First Sunday of the month, 02:00
0 2 1-7 * * [ "$(date +\%u)" = 7 ] && /opt/tsi-explorer/app/deploy/onprem/update.sh >> /opt/tsi-explorer/app/logs/cron.log 2>&1
```

## Step 12 — Final checks

| Check | Expected |
|---|---|
| `systemctl status tsi-postgrest tsi-explorer` | both `active (running)` |
| `https://<dns>/` in a browser | sign-in page of your identity provider, then the Dashboard |
| A user who is not in `testers` | "Access not authorised" |
| Dashboard figures | about 2,150 projects, 1,050 recommendations |
| **Projects** tab: search `wildfire` | results, with related terms |
| **CSRs** tab: views *Recitals 2026* and *Country reports 2026* | results for `water` |
| Assistant: "Which projects support energy efficiency in Romania?" | an answer citing projects |
| **Admin** menu (administrator) | user list and usage statistics |

## Operations

**Backups.** Back up the `tsi_explorer` database daily. `/etc/tsi-explorer/` (configuration and secrets) should be
backed up with the server. `output/` and `.cache/` can be rebuilt by the update.

**Upgrades.** As `tsi`:

```bash
cd /opt/tsi-explorer/app
git fetch --tags && git checkout <new-release>
# new migrations only, as listed in the release notes (schema other than public: from the copies of
# set_schema.py, written again after the checkout); then 900_service_role.sql if service_role has no BYPASSRLS:
psql -v ON_ERROR_STOP=1 -f supabase/migrations/0NN_<name>.sql && psql -c "notify pgrst, 'reload schema';"
(cd server && npm ci --omit=dev)
.venv/bin/pip install -e .
```

Then as an administrator: `sudo rsync -a --delete --exclude config.js /opt/tsi-explorer/app/web/ /var/www/tsi-explorer/`
and `sudo systemctl restart tsi-explorer`.

**Logs.**

| What | Where |
|---|---|
| Application service | `journalctl -u tsi-explorer` |
| PostgREST | `journalctl -u tsi-postgrest` |
| nginx | `/var/log/nginx/` |
| Data update | `/opt/tsi-explorer/app/logs/` |

## Local test (Agilos)

The whole stack can run on a workstation, with the test sign-in (any e-mail address, no password; refused unless
`PUBLIC_URL` is a localhost address) and the application service serving the page itself (no nginx):

```bash
# PostgreSQL with steps 2, 4 and 4b done, PostgREST running with the configuration of step 5 (port 3001)
cp -r web /tmp/site && cp deploy/onprem/config.js /tmp/site/config.js
cd server && npm ci
PORT=8080 PUBLIC_URL=http://localhost:8080 AUTH_MODE=dev POSTGREST_URL=http://127.0.0.1:3001 \
  JWT_SECRET_FILE=<jwt-secret file> SESSION_SECRET=<32+ characters> STATIC_DIR=/tmp/site node index.js
```

Then open <http://localhost:8080/>. Tests of the service: `cd server && npm test`.

## Optional: Haystack Enterprise

The update can also produce the corpus of your Haystack Enterprise instance (chatbot and impact pipelines):

```bash
.venv/bin/python -m tsi_agent prod-export
```

Upload and pipelines: [deploy/prod/README.md](../prod/README.md).
