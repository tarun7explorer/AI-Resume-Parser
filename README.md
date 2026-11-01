# Resume-Parser

FastAPI resume screening service with a vanilla HTML/CSS/JavaScript interface, a separate durable PostgreSQL-backed worker, evidence-verified quotations, and deterministic scoring. Scores support human review; they are not hiring probabilities or automated decisions. OCR is not implemented.

## Local development

Requires Python 3.12+, `uv`, and PostgreSQL 13+. Copy `.env.example` to `.env`, set `DATABASE_URL`, `AI_PROVIDER`, `AI_MODEL`, and the matching provider API key. Set a random `TOKEN_PEPPER` (at least 32 characters for production).

```powershell
Move-Item uv.lock "$env:TEMP\resume-parser-uv.lock.empty"
uv lock --python 3.14
uv sync --python 3.14
uv run --python 3.14 --env-file .env python -m app.migrate
uv run --python 3.14 --env-file .env uvicorn app.main:app --reload
uv run --python 3.14 --env-file .env python -m app.worker
```

Run the web service and worker in separate terminals. The migration command is a one-time/release step. `GET /health` is liveness; `GET /ready` checks PostgreSQL.

## Deploy to Render

Create services from `render.yaml`, set a model supported by your selected provider and its key in the Render dashboard, review the PostgreSQL plan and region, then deploy. The web service applies versioned migrations with its pre-deploy command. The cron service performs bounded retention cleanup. Keep secrets in Render environment settings; never commit `.env`.

The MVP stores uploaded PDFs in PostgreSQL. Configure retention for your privacy needs and keep access tokens only in the browser's in-memory session. For local tests, use synthetic PDFs and mock hosted AI calls.
