# TalentLens AI Resume Screening

TalentLens is a FastAPI application with a responsive vanilla-JavaScript dashboard. The public dashboard accepts a pasted or PDF job description and up to five PDF resumes. It extracts bounded PDF text, sends contact-redacted text to Groq for structured requirements and source evidence, validates that output against the models and extracted page text, then calculates versioned scores in Python. Groq does not calculate scores or make hiring decisions.

The result is provisional decision support. AI extraction can miss or misinterpret information. Review the cited page excerpts and original resume; never use the score as an automatic hiring decision.

## Run locally

Requirements: Python 3.12–3.14 and `uv`.

```powershell
cd D:\Resume-Parser
uv sync --frozen
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Set `GROQ_API_KEY` in the ignored local `.env`, then start without reload:

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8010 --no-access-log
```

Open `http://127.0.0.1:8010`; check `http://127.0.0.1:8010/health`. Choose another free port if 8010 is occupied. Run tests with `uv run pytest`.

## Public demo privacy and limits

The demo is stateless: uploaded PDFs, extracted text, and reports are not saved to Supabase or a file store. The endpoint accepts 1–5 resumes, each up to 10 MiB, and applies a per-client request limit. Users must confirm they are authorized to process the resumes. Email addresses and phone numbers are redacted before text is sent to Groq; other resume and job-description content is sent to the configured provider. Review the provider's current terms before submitting real candidate data.

PyMuPDF extracts selectable text. Scanned resumes may use bounded OCR for at most one page with a five-second page timeout when Tesseract and its Python dependencies are available. Profile links are detected but never fetched. PDF layout signals are only human-review cues.

## Configuration and optional integrations

The public demo requires the server-side `GROQ_API_KEY`; `GROQ_MODEL` defaults to `openai/gpt-oss-20b`. `.env.example` contains blank placeholders only. Keep `.env` and credentials out of Git.

The existing protected tenant APIs, Supabase persistence, and durable worker remain available. Supabase URL and anon key must be configured together for those APIs. Enabling `SCREENING_WORKER_ENABLED` also requires the Supabase service-role key and Groq key. The worker is disabled by default. NVIDIA configuration is unused.

PDF limits and OCR are configured by `RESUME_MAX_BYTES`, `PDF_MAX_PAGES`, `OCR_MAX_PAGES`, `OCR_TIMEOUT_SECONDS`, `OCR_LANGUAGE`, and `TESSERACT_CMD`. `AI_TIMEOUT_SECONDS` and `AI_MAX_ATTEMPTS` bound provider calls and repair attempts.

## Deployment

`render.yaml` deploys the FastAPI app with the worker disabled. Add `GROQ_API_KEY` in Render's secret environment settings for public screening. Render supplies `PORT`; the health check is `/health`. Configure Supabase credentials only if using the protected tenant APIs or worker.

The ordered SQL migrations under `supabase/migrations/` remain the database setup source. `supabase/schema.sql` is a generated snapshot and should not be applied in addition to those migrations.
