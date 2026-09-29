# Deploying

The project is a **Python backend** (the governed copilot) plus a **static
frontend** (`public/index.html`). One Python server serves both — no Node,
no framework, no build step. Deploy on the free tier of **Render.com**:

## 1. Deploy (free, from GitHub)

1. Sign up at [render.com](https://render.com), connect this repo.
2. Click **Add Web Service**, pick this repo, and Render reads `render.yaml`:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn wsgi:app --workers 1 --threads 4 --timeout 300 --bind 0.0.0.0:$PORT`
   - Instance type: **Free** (Standard)
3. Deploy.

## 2. What happens on the first request

Render's filesystem is read-only apart from `/tmp`, so the copilot
bootstraps itself on the cold start of each warm instance:

| Step | Cost |
|---|---|
| Generate the synthetic dataset into `/tmp` (200 customers, 150 days) | ~2.0 s |
| Ingest CSVs into the sqlite warehouse, parse the policy corpus, create the 10 semantic views | ~0.5 s |
| Warm the detector on one customer | ~0.1 s |

Measured cold start: **2.6 s**. Warm requests: **<50 ms**.

Set `RISK_PROFILE=full` to generate the full 600-customer / 400-day dataset
instead of the compact demo profile (cold start rises to ~8 s).

## 3. Check the deployment

```bash
curl -s "$URL/api?action=health" | jq
curl -s -X POST "$URL/api" -H 'Content-Type: application/json' \
  -d '{"action":"ask","question":"which customers are structuring cash?"}' | jq .answer_text
```

Or just open `$URL` — the UI polls `health`, shows the runtime fingerprint and
lets you ask questions, run a portfolio scan and build the board and liquidity
packs.

## 4. API surface

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api?action=health` | readiness, cold-start time, runtime status |
| `GET` | `/api?action=status` | warehouse, corpus and configured thresholds |
| `GET` | `/api?action=examples` | suggested questions |
| `POST` | `{"action":"ask","question":"..."}` | governed answer + citations + SQL + guardrail |
| `POST` | `{"action":"detect","customer_id":"CUST-000123"}` | full explainable case |
| `POST` | `{"action":"scan","limit":200}` | portfolio scan, persists findings |
| `GET` | `/api?action=findings&limit=25` | ranked findings |
| `GET` | `/api?action=policy&q=structuring` | citable clauses |
| `POST` | `{"action":"file","customer_id":"...","filing_type":"SAR"}` | draft filing (always `PENDING_REVIEW`) |
| `POST` | `{"action":"approve","filing_id":"...","approver":"name@bank.com"}` | record human approval |
| `POST` | `{"action":"escalate","customer_id":"..."}` | MCP escalation ladder |
| `POST` | `{"action":"board_pack"}` / `{"action":"liquidity"}` | MIAR / prudential packs |
| `GET` | `/api?action=audit&limit=50` | audit trail |
| `GET` | `/api?action=mcp` | MCP tool manifest and connector status |

`GET` and `POST` are equivalent for every read action. State-changing actions
require `POST` and are written to the audit log.

## 5. Governance that holds in production

* Draft filings are **always** `PENDING_REVIEW`. `approve` rejects an approver
  named `bot`/`copilot`/`auto` or an empty string, so the four-eyes control
  (clause `FILE-2.1.1`) cannot be bypassed through the API.
* A filing with `citation_coverage < 1.0` is refused by the submission gateway.
* Answers below the abstention threshold refuse to assert and state the gap.
* Synthetic data only. No production or personal data reaches the bundle.

## 6. Local development

The server is fully runnable without any host:

```bash
pip install -r requirements.txt
PORT=5000 gunicorn wsgi:app --workers 1 --threads 4 --timeout 300 --bind 0.0.0.0:5000
```

Or exercise the exact handler code path without a server:

```bash
python scripts/verify_vercel.py    # exercises the exact handler code path
python scripts/verify_bundle.py    # builds the real upload bundle and re-tests it
```

Both scripts run a genuine cold start in a fresh `/tmp` directory, so a broken
import or a missing file fails locally rather than in production.