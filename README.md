# TalentLens AI Resume Screening

FastAPI and vanilla-JavaScript resume screening demo. The public dashboard accepts a job description and up to five PDF resumes without an account. It parses PDFs, optionally runs bounded OCR, asks Groq for structured requirement and evidence extraction, validates evidence against source pages, then calculates deterministic versioned scores in Python.

Scores and integrity signals are provisional review aids. The system does not make hiring decisions.

## Local setup

Use Python 3.12–3.14 and `uv`. If `uv` is not installed, run `winget install --id astral-sh.uv -e` from an elevated or permitted Windows terminal, then open a new CMD window. Set `GROQ_API_KEY` in your ignored local `.env` if you want to run real analysis. From Windows CMD:

```bat
cd /d D:\Resume-Parser
if not exist uv.lock uv lock
uv sync --frozen
if not exist .env copy .env.example .env
notepad .env
uv run uvicorn app.main:app --reload --no-access-log --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. `/health` is a process-liveness check. Run the application tests with:

```bat
uv run pytest
```

## Public demo privacy and limits

The demo endpoint is stateless. Resume PDFs, parsed text, and reports are not written to Supabase, a server-side session, or a file store. Each response goes only to the browser request that submitted those files; results remain in that page until it is refreshed or closed. The endpoint accepts at most five resumes per request, each up to 10 MiB, and limits requests by the server-observed client address.

Before submitting, the user must confirm they are authorized to process the resumes. The worker does not run for this flow. Contact details are redacted from resume text before text is sent to Groq, but other resume and job-description content is sent to the configured Groq API for analysis. Configure and review the provider account and terms before using real candidate data. If `GROQ_API_KEY` is missing or the provider is unreachable, analysis fails visibly; the application does not invent a report.

Profile URLs are parsed but never fetched. PDF layout signals are shown for human review and do not prove misconduct. The scores are calculated by deterministic Python rules after AI output and evidence have been validated.

## Environment variables

Copy `.env.example` to `.env`. The public demo needs `GROQ_API_KEY` and uses `GROQ_MODEL` (default `openai/gpt-oss-20b`). The Supabase variables are optional for the public demo and are still used by the existing tenant-scoped API and durable worker. Keep every real key in `.env`; never place it in browser code or commit it.

`SUPABASE_URL` and `SUPABASE_ANON_KEY` must be configured together for the protected tenant API. `SUPABASE_SERVICE_ROLE_KEY` is server-only and is required when the durable worker is enabled. `SCREENING_WORKER_ENABLED` defaults to `false`. `NVIDIA_API_KEY` is reserved and unused.

PDF limits and OCR settings are bounded through `RESUME_MAX_BYTES`, `PDF_MAX_PAGES`, `OCR_MAX_PAGES`, `OCR_TIMEOUT_SECONDS`, `OCR_LANGUAGE`, and `TESSERACT_CMD`. Scanned PDFs need PyMuPDF, pytesseract, Pillow, and the Tesseract executable with the requested language data.

## Existing protected tenant API

The public demo has no login, session, or account-management endpoints. The established multi-tenant jobs, history, report, deletion, and worker APIs remain available only through verified Supabase Auth bearer tokens and existing RLS policies; their authorization stays in place to protect persisted organization data. They are not used by the public demo dashboard. Resume storage remains private; no table or storage policy was made public. The public demo has no user account, organization, persistence, or cross-request result lookup.

The ordered SQL migrations under `supabase/migrations/` remain the source for database setup. `supabase/schema.sql` is a generated snapshot and must not be applied in addition to those migrations. No migration is applied by this application.

## Tests and verification

```bat
py -m compileall -q app
py -m unittest discover -s tests -v
```

These tests use isolated HTTP mocks for provider integrations. They do not verify a live Supabase or Groq account.
