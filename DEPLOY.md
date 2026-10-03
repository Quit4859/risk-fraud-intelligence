# Deploying the Risk, Fraud & Regulatory Intelligence Copilot

The app is a single WSGI service: `wsgi.py` serves the JSON API and the static
console from one process. There is no build step for the frontend, no Node
toolchain, and no framework. Everything runs on the Python standard library plus
three small packages.

- [Deployment shape](#deployment-shape)
- [Render (the deployed target)](#render-the-deployed-target)
- [Running locally](#running-locally)
- [Snowflake](#snowflake)
- [Keeping a free instance awake](#keeping-a-free-instance-awake)
- [What is where](#what-is-where)

---

## Deployment shape

```
browser ──▶ gunicorn wsgi:app ──▶ /healthz            (instant, never builds)
                               ├─▶ /api?action=...    (JSON)
                               └─▶ /*                 (public/, allowlisted)
```

`wsgi.py` is a plain WSGI callable, so it runs on Render, Fly.io, Railway,
Cloud Run, Heroku or anything else that can start a process.

**Everything degrades.** With no Snowflake credentials the service starts
normally, selects the local sqlite engine, and the UI header reads
`Fallback mode: local engine`. Cortex features that are unreachable are labelled
individually rather than failing the whole app.

---

## Render (the deployed target)

`render.yaml` is a valid [Render Blueprint](https://render.com/docs/blueprint-spec).
Point Render at the repo and it fills the form automatically.

| Setting | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn wsgi:app --preload --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:$PORT` |
| Health check path | `/healthz` |
| Instance | Free |

### Why `--preload`

Without `--preload` each gunicorn worker builds its own warehouse, racing on the
same sqlite file. With `--preload` the engine is built once in the master before
workers fork. `--workers 1` is deliberate: the engine holds a single sqlite
connection, and one worker with 8 threads is both simpler and faster for this
workload than several processes contending for it.

### Why the health check matters

`/healthz` answers **immediately** and never triggers a build. A health check
that called the full engine status would start a multi-second warehouse build on
every probe, and Render would kill the instance for being slow to respond. While
the engine builds, the endpoint returns `{"status": "warming_up"}` and the UI
shows a warm-up screen with an elapsed counter.

### Secrets

Secrets are **not** in `render.yaml`. Set them in the Render dashboard under
*Environment*, or they will end up in git:

```
SNOWFLAKE_ACCOUNT   SNOWFLAKE_USER   SNOWFLAKE_TOKEN
SNOWFLAKE_PRIVATE_KEY_PATH   SNOWFLAKE_PRIVATE_KEY_PASSPHRASE
JIRA_BASE_URL  JIRA_EMAIL  JIRA_API_TOKEN
SLACK_WEBHOOK_URL
RISK_ALLOWED_ORIGIN     # your deployed origin, e.g. https://risk-fraud-intelligence.onrender.com
```

`.env.example` documents every variable. `.env` itself is gitignored, and CI
fails the build if one is ever committed.

---

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# optional: a gitignored .env with Snowflake credentials
cp .env.example .env

# generate the dataset and start the console
python scripts/generate_synthetic_data.py --customers 200 --horizon-days 150
gunicorn wsgi:app --preload --workers 1 --threads 8 --bind 0.0.0.0:8000
```

Then open <http://localhost:8000>.

Verify the install:

```bash
curl -s localhost:8000/healthz | python3 -m json.tool
python3 -m pytest tests/ -q
```

### Common commands

```bash
python scripts/generate_synthetic_data.py --help      # dataset options
python scripts/evaluate.py                            # detection metrics
python -m backend.cli --help                          # CLI surface
python -m backend.cli scan --limit 200                # run a portfolio scan
```

---

## Snowflake

The warehouse is optional. Supply credentials through the environment and the
engine factory selects Snowflake automatically; leave them unset and it selects
sqlite. Selection is reported at `/api?action=health` under `runtime.engine` as
`snowflake` or `local-fallback`.

Auth methods, in order of preference:

1. **Programmatic OAuth token** — `SNOWFLAKE_TOKEN`. Shortest lived.
2. **Key-pair** — `SNOWFLAKE_PRIVATE_KEY_PATH` (a `.p8` file) plus
   `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`.
3. **Password** — `SNOWFLAKE_PASSWORD`. Demo accounts only; the UI labels which
   method is active.

See `docs/PLAN.md` §9 and `docs/SNOWFLAKE_RUNBOOK.md` for provisioning order.

---

## Keeping a free instance awake

Render's free tier sleeps an idle instance after 15 minutes, so the first visit
after a gap pays a cold start. `/healthz` returns instantly and is the right
target for a keep-alive ping.

The cheapest option is a free scheduled job (GitHub Actions, cron-job.org):

```yaml
# .github/workflows/keepalive.yml
name: keepalive
on:
  schedule: [{cron: "*/10 * * * *"}]
  workflow_dispatch:
jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - run: curl -fsS --max-time 20 "${{ secrets.KEEPALIVE_URL }}/healthz"
```

Set `KEEPALIVE_URL` to the deployed origin in repository secrets.

A second option is a `Vercel`/`cron-job.org` GET against `/healthz` every 10
minutes. Note that the **warm-up screen** handles the sleep gracefully either
way; the keep-alive is an optimisation, not a requirement.

---

## What is where

| Path | Purpose |
|---|---|
| `wsgi.py` | WSGI entrypoint: `/healthz`, `/api`, static files |
| `api/index.py` | HTTP routing, pagination, CORS, RBAC |
| `api/_runtime.py` | Engine bootstrap, boot scan, warm-up state |
| `backend/warehouse.py` | Local sqlite warehouse |
| `backend/engine/` | Warehouse interface, Snowflake implementation |
| `backend/governance/` | Filing lifecycle, four-eyes approval, RBAC |
| `backend/agents/` | Detectors, router, executors, retrieval, composer |
| `backend/orchestration/` | Copilot workflow, guardrails, audit log, MCP |
| `config/settings.yaml` | Every decision threshold, reviewable in a diff |
| `sql/` | Snowflake DDL: schema, dynamic tables, tasks, semantic views |
| `public/` | Static console (vanilla JS/CSS, no build step) |
| `tests/` | pytest suites |
| `docs/` | Plan, CoCo log, demo script, evidence |

### Removed

The Next.js scaffold (`package.json`, `next.config.js`, `pages/`), `vercel.json`,
`.vercelignore`, `Procfile` and the `verify_vercel.py` / `verify_bundle.py`
scripts are gone. They described a Vercel deployment this project no longer
targets, and their presence made it ambiguous which entrypoint was real.
