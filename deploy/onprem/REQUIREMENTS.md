# TSI × CSR Explorer — server requirements (self-hosted, Linux)

What to prepare **before** the installation. The installation itself is described step by step in
[README.md](README.md).

## Architecture

```
 Users' browsers ──HTTPS 443──▶ Linux application server ──TCP 5432──▶ PostgreSQL server
                                 │  nginx: web page (static files), /api ──▶ Node.js service (127.0.0.1:3000)
                                 │  Node.js service ──▶ PostgREST (127.0.0.1:3001) ──▶ database
                                 │  monthly data update (Python, cron)
                                 └──HTTPS 443──▶ internet: *.europa.eu (data sources), GPT@EC (AI model)
```

The web server serves the page and forwards `/api` to a small Node.js service. The Node.js service signs users in
and is the only entry point to the database; it reaches it through PostgREST, which runs locally on the same
server and is never exposed. A monthly data update runs on the same server. Agilos installs and configures the
application; we only need the server ready and the access below.

## 1. Server

| Item | Requirement |
|---|---|
| Operating system | Ubuntu 24.04 LTS (or Debian 12 / RHEL 9). A virtual machine is fine. |
| Resources | 2 vCPU, 4 GB RAM and 20 GB disk available. A server shared with other applications is fine. |
| Web server | nginx (or Apache), with TLS. |
| Node.js | 22 LTS. |
| Python | 3.12 or later, with `python3-venv`. It runs the monthly update. |
| Tools | `git`, `curl`, `tar`, and the PostgreSQL client `psql` (to install the schema). |
| Time | Time synchronisation (NTP) enabled. |

## 2. Accounts and access

- **Service account:** a system account `tsi` with no interactive login. It runs the services and the cron job.
- **Folder:** `/opt/tsi-explorer`, owned by `tsi`.
- **Services:** permission to create systemd services (`tsi-explorer`, `tsi-postgrest`), or someone who can create
  them from our files.
- **SSH access for Agilos:** public key provided on request, with `sudo` during the installation (limited to
  nginx, systemd and package installation if needed).

## 3. Network

- **Inbound:**
  - 443 (HTTPS) from the users' network;
  - 80, only to redirect to 443.
- **Outbound to the database:** TCP 5432 (or the port in use) to the PostgreSQL server.
- **Outbound to the internet** (HTTPS 443):
  - `*.europa.eu`: data sources (projects, recommendations, reports) and the GPT@EC gateway
    (`api.tech.ec.europa.eu`);
  - for the installation: `registry.npmjs.org`, `pypi.org`, `files.pythonhosted.org`, `github.com`,
    `objects.githubusercontent.com`.
- **Proxy:** if there is one, please send its address and port.
- **DNS name:** for example `tsi-explorer.<domain>`, pointing to the server.
- **TLS certificate** valid for that name (Let's Encrypt, or an internal certificate trusted by the users'
  browsers).

## 4. PostgreSQL database (on the other server)

- **Version:** PostgreSQL 14 or later.
- **Dedicated database:** for example `tsi_explorer`. About 100 MB today; plan for 2 GB. No extension is required.
- **Owner account** for this database: it creates the tables, functions and row-level security (RLS) policies when
  the schema is installed.
- **Roles:** permission to create the application roles (script in [README.md](README.md), step 2), or a DBA who
  runs it.
- **Network access:** allow the application server's IP in `pg_hba.conf`. TLS is preferred.
- **Daily backup** of the database. Users' link reviews, usage data and impact analyses cannot be rebuilt; the rest
  is rebuilt by the monthly update.

## 5. AI model (GPT@EC)

- A GPT@EC API key for a service account, and the endpoint URL (today `https://api.tech.ec.europa.eu/ecgpt/v1`).
- A model with function calling and structured (JSON schema) output, for example `gpt-5.1`.
- Quota: the monthly update uses about 5 to 20 million tokens, depending on the number of changed projects; the
  assistant uses about 20,000 to 100,000 tokens per question, briefing or impact analysis.

## 6. Please send back

- **Server:** DNS name and IP address.
- **SSH:** user and port.
- **Database:** host, port, database name and account names. Please send the passwords through a separate channel,
  not in the same e-mail.
- **Checks run from the server** (output of these commands):
  - `curl -I https://reform-support.ec.europa.eu`
  - `curl -I https://registry.npmjs.org`
  - `psql "host=<db-host> dbname=tsi_explorer user=<owner>" -c "select version();"`
