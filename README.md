# AI Data Agent

A Vue 3 + FastAPI application for asking business questions about an explicitly authorized MySQL datasource. The backend uses an OpenAI-compatible model with tool calling, validates candidate SQL, executes read-only queries, and generates charts from actual query results.

## Features and scope

- Natural-language questions and follow-up conversations.
- Explicit table/view and column allowlists; single-table, read-only SQL validation.
- TLS certificate and hostname verification for MySQL.
- Query deadlines, bounded connection pools, row and result-size limits.
- Server-generated line, bar, pie, table and metric displays. The model cannot supply chart datasets or executable chart options.
- SQLite conversation storage. Charts are not restored when historical conversations are loaded.

This repository is a **deployment-source distribution**. It excludes private spreadsheets, database dumps, credentials, certificates, screenshots, internal reports, interview notes, customer-specific import tools and evaluation fixtures. It does not include a database or model subscription.

The default business terminology is for membership and consumption analysis. Adapting another domain requires reviewing `services/agent/config/business_terms.yaml` and the default terminology/examples in `services/agent/app/agent/business_terms.py`, in addition to configuring the schema allowlist.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/).
- Node.js **24 LTS** and npm for the frontend build.
- A reachable MySQL 8 instance, your own analysis views, a dedicated read-only account and a trusted TLS CA certificate.
- An OpenAI-compatible **Chat Completions** endpoint and model supporting `tools` and multi-turn tool results. A plain text-only completion endpoint is insufficient.

## Local setup

Clone your repository and install the locked dependencies:

```bash
git clone https://github.com/xk103295870-alt/new-AI-data.git
cd new-AI-data

cd services/agent
uv sync --frozen --no-dev
cd ../../apps/web
npm ci
cd ../..
```

### 1. Configure the backend

```bash
cp .env.example .env
chmod 600 .env
# Edit .env locally. Never commit it.
```

The backend reads **process environment variables**. Copying an environment file alone does not load it. The commands below explicitly export its values; use shell-compatible quoting, especially for JSON and passwords.

Important settings:

| Variable | Purpose |
|---|---|
| `DATASOURCE_ENABLED` | Set `true` after configuring your database. |
| `DATASOURCE_ID` | Stable datasource identifier, e.g. `demo_mysql`. |
| `DB_HOST`, `DB_PORT`, `DB_NAME` | MySQL hostname, port and analysis database. |
| `DB_USER`, `DB_PASSWORD` | Dedicated account with SELECT access only to approved views. |
| `DB_SSL_ENABLED`, `DB_SSL_CA_PATH` | Use `true` and an absolute path to a trusted CA. The hostname must match the server certificate. |
| `DB_ALLOWED_OBJECTS_JSON` | Exact object-to-column allowlist. Empty means no authorization. |
| `LLM_BASE_URL` | Provider base URL, typically ending in `/v1`; do not append `/chat/completions`. |
| `LLM_API_KEY`, `LLM_MODEL` | Your server-side key and tool-capable model identifier. |
| `APP_TIMEZONE` | Application timezone; default `Asia/Shanghai`. |
| `SESSION_DB_PATH` | Writable SQLite path; default `./data/agent_sessions.sqlite3`, relative to the backend working directory. |
| `BUSINESS_TERMS_PATH` | Default `config/business_terms.yaml`, relative to the backend working directory. |

For the default business examples, create analysis views from **your own data**, then authorize only the columns you intend to expose. For example, if your views expose these fields:

```bash
DB_ALLOWED_OBJECTS_JSON='{"trade_view":["trade_completed_at","trade_amount","store_no","store_name","district_name","payment_method","format_name","points_added"],"member_view":["gender","points","member_tag","registered_at"]}'
```

You may authorize a smaller scope matching your views. This JSON does not create tables or grant database permissions: your database administrator must separately configure the views and SELECT grants. Avoid exposing personal identifiers or unrestricted source tables.

Questions, authorized schema and limited query results are sent to your configured LLM provider. Configure only data you are permitted to send to that provider. Keep keys and database credentials on the backend; never use `VITE_*` variables for secrets.

### 2. Start the backend

From the repository root, in a POSIX-compatible shell:

```bash
set -a
. ./.env
set +a
cd services/agent
uv run --no-dev uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 3. Start the frontend

In another terminal:

```bash
cd apps/web
npm run dev
```

Open **http://127.0.0.1:5173/**. Vite proxies `/api` and `/health` to port 8000. Click **Check** to inspect the authorized datasource, then ask a question.

Without database/model configuration, the application still starts and `/health` remains available, but analytical chat is unavailable. Health is a process check, not a database or model readiness check.

Example questions, after adapting the views and dates to your data:

> How many transaction records are in the authorized consumption view?

> Show total consumption amount by month from January 1, 2025, inclusive, to April 1, 2025, exclusive. Display a bar chart in chronological order. The currency unit is unconfirmed.

## Server deployment

This version has **no built-in user authentication or tenant isolation**. Use a trusted network or an authenticated reverse proxy. Do not expose the backend directly to the public Internet. CORS is not authentication.

1. Install backend dependencies with `uv sync --frozen --no-dev`.
2. Run `npm ci && npm run build` in `apps/web`.
3. Serve `apps/web/dist` with your web server. Vite's development proxy does **not** exist in the built files.
4. Proxy `/api/` and `/health` to the backend, retaining their path prefixes. Use HTTPS and access control for the complete site.
5. Start one backend process under your service manager, using `services/agent` as its working directory. Keep environment files and CA material outside the public web directory.
6. Give the service account write access to its SQLite data directory. Back up that directory if conversations must survive redeployment.

Example Nginx server body (replace the domain, build path and authentication file; terminate HTTPS on this server or on your ingress):

```nginx
server {
    listen 80;
    server_name analytics.example.com;
    root /opt/new-ai-data/apps/web/dist;
    index index.html;

    auth_basic "Private analytics";
    auth_basic_user_file /etc/nginx/analytics.htpasswd;

    location / {
        try_files $uri $uri/ /index.html;
    }
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_read_timeout 180s;
    }
    location = /health {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

Do not send passwords over plain HTTP: enable HTTPS before allowing remote access. Use your platform's secret/environment management for the backend. Set `CORS_ORIGINS` to the actual frontend origin when cross-origin access is required; same-origin reverse proxying is preferred.

Typical service command, with environment values injected by the service manager:

```bash
cd /opt/new-ai-data/services/agent
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

## Verification and troubleshooting

```bash
curl --fail http://127.0.0.1:8000/health

cd apps/web
npm run typecheck
npm run build
```

- **Chat unavailable:** verify datasource enablement, a nonempty object/column allowlist, all three LLM settings and database access. Do not print the full environment when diagnosing.
- **TLS connection fails:** check the CA path, certificate trust and hostname. Correct the configuration; do not disable verification to work around a failed certificate check.
- **Model unavailable / timeout:** verify provider availability, model tool support and network access. Defaults are `LLM_TIMEOUT_SECONDS=90`, `AGENT_REQUEST_TIMEOUT_SECONDS=150`, `MAX_TOOL_CALLS=8`. Reverse-proxy deadlines must accommodate the request budget.
- **SQL policy blocked:** the query may reference unapproved fields, unsupported functions or joins. Rephrase within the supported scope; do not broaden permissions merely to make a demonstration succeed.
- **No results:** check the actual data's date range. Use explicit absolute dates for reproducible demonstrations.
- **Frontend works but API fails after deployment:** ensure your production server proxies both `/api/` and `/health`; frontend requests use relative URLs.
- **SQLite write failure:** check the service working directory, directory permissions and configured path.

## Current limitations

- One configured datasource; single-table queries. JOIN, CTE, subqueries, writes and arbitrary SQL submission are unsupported.
- Complex relative-date exclusions/comparisons are not reliably interpreted. Use explicit absolute ranges, or clarify the requested scope before relying on a result.
- Record counts do not establish unique customer/order counts. Currency and net-revenue rules require business confirmation; the application does not infer them from field names.
- The query normally returns at most 200 rows, the model normally receives at most 50 rows, and a chart shows at most 200 rows. These are separate limits. Ask for a server-side aggregate rather than summing a partial sample.
- One chart per reply, from the most recent successful query. Large or imprecise numeric values may fall back to a lossless table.
- Model responses and latency can vary; a successful SQL query does not guarantee the provider's final response will succeed.
- This public distribution omits internal tests, evaluation tooling and customer-specific import scripts. Source configuration may still contain optional development/test script entries; those internal suites are not shipped here.

## Roadmap and contributing

Planned work includes reusable domain Skills, additional datasource adapters, enterprise identity and tenant integration, richer auditing, and container/desktop packaging. These are roadmap items, not capabilities of the current deployment snapshot.

Issues and pull requests are welcome, particularly for datasource adapters, domain semantics and query-safety improvements. Contribution guidelines will be added by the maintainers; this distribution does not include a separate contributing guide.

## License and project background

As stated in the upstream project introduction, the license is to be determined by the maintainers before official release, including review of third-party dependencies. No project license grant is asserted by this snapshot.

The upstream project introduction credits **Weino Technology (微诺科技)** and describes its origin in commerce, property and membership systems. This README retains that attribution while documenting the behavior implemented in this source distribution.
