# AI Resume Screening & Candidate Intelligence

FastAPI recruiter application with Supabase Auth, organization-scoped PostgreSQL/RLS persistence, private PDF storage, and an opt-in durable screening worker. The worker extracts PDF text/layout with PyMuPDF, uses bounded Tesseract OCR for scanned pages, sends contact-redacted text to Groq for structured evidence extraction, validates cited quotes against source pages, then applies the deterministic versioned Python rubric. It persists candidate reports, scores, and safe processing error codes. Scores and integrity signals are provisional review aids; this system does not make hiring decisions.

## Requirements and local startup

Use Python 3.12–3.14 and [uv](https://docs.astral.sh/uv/). From `D:\Resume-Parser` in Windows CMD:

```bat
winget install --id astral-sh.uv -e
uv lock
uv sync --frozen
copy .env.example .env
uv run uvicorn app.main:app --reload --no-access-log --host 127.0.0.1 --port 8000
```

`uv lock` generates the lockfile after dependency changes; review and commit it. A checkout with no `uv.lock` is not reproducible and cannot use `uv sync --frozen`. Open `http://127.0.0.1:8000/`; `/health` is process liveness and does not establish database health. Run tests with `uv run pytest`.

The `.env.example` file lists all application settings. Locally, `APP_ENV=development`, `SCREENING_WORKER_ENABLED=false`, and empty Supabase/provider keys allow the UI and health check to start; authenticated data workflows require a Supabase project. Enable real screening only after setting Supabase URL, anon key, service-role key, Groq key, and `SCREENING_WORKER_ENABLED=true`. The service-role key and Groq key belong only in local `.env` or Render Dashboard secret environment settings.

## Supabase project setup

Install the Supabase CLI and Docker Desktop, then from the workspace root:

```bat
supabase start
supabase status
supabase db reset
supabase test db
```

`db reset` recreates the **local** database and applies timestamp-ordered migrations in `supabase/migrations/`; do not use it against a linked/remote project. `supabase test db` runs the PostgreSQL pgTAP tenant-isolation tests in `supabase/tests/tenant_rls.test.sql`. The committed `supabase/schema.sql` is a readable concatenation snapshot; migrations are the source of ordered changes.

For a hosted project, create the Supabase project, then run `supabase login`, `supabase link --project-ref YOUR_PROJECT_REF`, inspect the linked target, and apply reviewed migrations with `supabase db push`. Verify migration status with `supabase migration list`. These remote commands require a project and CLI credentials; do not place access tokens in source or chat.

Public signup is disabled in `supabase/config.toml`. Provision each recruiter through the Supabase Auth dashboard or a trusted administrator process, with email confirmation enabled. To establish an organization, an authorized project administrator must create its `public.organizations` row and the first `public.organization_memberships` row with role `owner`; this bootstrap is intentionally unavailable to ordinary clients. Add later members through the trusted admin workflow. Roles are `owner`, `admin`, `recruiter`, and `viewer`; application routes and RLS check them independently.

## Environment and credentials

Copy `.env.example` to `.env`. Set `SUPABASE_URL` and `SUPABASE_ANON_KEY` from the project API settings; the app uses the public key with the verified recruiter bearer token for Auth, PostgREST, and Storage. Both are required together, and production requires HTTPS. `APP_ENV`, `LOG_LEVEL`, `PORT`, and `CORS_ORIGINS` control runtime configuration. `RESUME_MAX_BYTES` may be at most 10 MiB to match the database and bucket; `SIGNED_URL_TTL_SECONDS` is limited to 30–300 seconds.

The background queue worker is disabled by default for safe local startup. To process real uploaded resumes, install Tesseract OCR and English language data. The upstream Tesseract project points Windows users to the third-party UB Mannheim installer; with winget, run `winget install --id UB-Mannheim.TesseractOCR -e`. Confirm `tesseract --version` works in a new CMD window. Set `TESSERACT_CMD` only if the executable is not on `PATH` (commonly `C:\Program Files\Tesseract-OCR\tesseract.exe`). Configure `SUPABASE_SERVICE_ROLE_KEY`, `GROQ_API_KEY`, and set `SCREENING_WORKER_ENABLED=true` only for a real integration. The service role is used only by server-side worker RPCs and private-object download; it is never sent to browser code or logged. `GROQ_MODEL` defaults to `openai/gpt-oss-20b`; use a currently available Groq model that supports JSON mode. `NVIDIA_API_KEY` is optional and unused. `.env` and credential files are ignored by Git, and `.env.example` contains blank placeholders only.

### Environment variables

| Variable | Requirement | Purpose |
| --- | --- | --- |
| `APP_ENV` | Required in Render; defaults to `development` locally | Use `production` in Render; production validation is strict. |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Required for sign-in and persisted workflows | Project URL and client-safe anon/publishable key. The backend verifies user tokens and RLS applies. |
| `SUPABASE_SERVICE_ROLE_KEY` | Required when the worker is enabled and in production | Server-only worker access; set in local `.env` or Render Dashboard secrets. |
| `GROQ_API_KEY` | Required when the worker is enabled and in production | Server-only structured AI extraction. |
| `SCREENING_WORKER_ENABLED` | Required as `true` in production; optional locally | Starts queue and retention processing. |
| `CORS_ORIGINS` | Optional for same-origin hosting | JSON array of exact origins, e.g. `["https://recruiter.example"]`; wildcards are rejected. |
| `PORT` | Supplied by Render; defaults to `8000` locally | Runtime bind port. |
| `LOG_LEVEL` | Optional; defaults to `INFO` | Structured log threshold. |
| `RESUME_MAX_BYTES`, `SIGNED_URL_TTL_SECONDS` | Optional bounded defaults | Upload cap (maximum 10 MiB) and private-link expiry (30–300 sec). |
| `PDF_MAX_PAGES`, `OCR_MAX_PAGES`, `OCR_TIMEOUT_SECONDS`, `OCR_LANGUAGE`, `TESSERACT_CMD` | Optional bounded defaults | PDF/OCR resource limits and executable configuration. |
| `AI_TIMEOUT_SECONDS`, `AI_MAX_ATTEMPTS`, `GROQ_MODEL` | Optional bounded defaults | Provider timeout, bounded retries, and model. |
| `CLASSIFICATION_CONSIDERED_MIN`, `CLASSIFICATION_PRIORITY_MIN` | Optional provisional defaults | Review bands only, not validated hiring thresholds. |
| `NVIDIA_API_KEY` | Optional and unused | Reserved by the reference stack; no NVIDIA integration is implemented. |

`PDF_MAX_PAGES`, `OCR_MAX_PAGES`, and `OCR_TIMEOUT_SECONDS` bound document processing. `OCR_LANGUAGE` selects installed Tesseract language data; `TESSERACT_CMD` can point to the executable on Windows. Scanned PDFs fail with an actionable error if the Python OCR wrapper, Pillow, the Tesseract executable, or required language data is unavailable. No OCR executable or real provider credential is bundled with the app.

The scoring rubric version is `resume-rubric-v1-2026-10`, using the provisional reference weights: required skills 30%, experience/projects 25%, responsibilities 20%, qualifications 10%, evidence quality 15%. Categories without explicit job requirements are excluded and the remaining applicable weights are normalized. Supported explicit evidence receives full credit; inference, partial, uncertain, missing, and absent evidence receive lower or zero credit under deterministic rules in `app/scoring.py`. Classification bands are configurable through `CLASSIFICATION_CONSIDERED_MIN` and `CLASSIFICATION_PRIORITY_MIN` and are not validated hiring thresholds.

## Auth, persistence, and file access

`POST /api/v1/auth/sign-in` exchanges email/password with Supabase Auth; `POST /api/v1/auth/refresh` exchanges a refresh token. The browser holds the returned session and sends the access token as `Authorization: Bearer ...`. Every protected request verifies it through Supabase Auth `/auth/v1/user`; user IDs and roles come from that verified response or tenant-scoped database records, never client headers or request bodies. Keep tokens in the browser session only as required by the client; never log them.

Create jobs at `POST /api/v1/organizations/{organization_id}/jobs` with an `Idempotency-Key`. Submit each PDF to `POST /api/v1/organizations/{organization_id}/jobs/{job_id}/screenings` using multipart fields `file`, optional `candidate_name`, optional `candidate_email`, and a per-file `Idempotency-Key` UUID. Intake validates bounded bytes and PDF markers, atomically creates the candidate/resume/screening/processing records, then uploads to the private `private-resumes` bucket. Repeating the same key and payload returns the original screening; a different payload conflicts. The worker only claims resumes whose private upload has reached `available` state.

The migration provisions a non-public, PDF-only bucket and tenant-aware Storage policies. Reads require an authorized membership, a non-deleted/non-expired resume, and an available record. The API creates same-project signed URLs with a short expiry; it never returns public file URLs. Resume deletion first soft-deletes and scrubs the report transactionally, then enqueues private-object removal. A service-role worker deletes objects idempotently, including treating an already-missing object as complete. Available resumes default to a 365-day retention period; the worker periodically queues expired resumes and stale incomplete uploads for cleanup. Adjust the retention period in the next migration before applying it if the client policy requires a different period.

## Queue recovery, history, and deletion

`processing_jobs` is the durable source of attempt counts, backoff time, worker lease, and safe error code. Claims use `FOR UPDATE SKIP LOCKED`; expired leases are reclaimed, reports are written only by the active lease holder, transient failures receive at most three attempts, and invalid input/configuration/permission failures are terminal. `queued`, `processing`, `retryable_failure`, `completed`, and `failed` states are stored on the screening and shown in the recruiter UI. Retry controls only expedite a still-eligible automatic retry and do not reset or extend its attempt budget.

Batch intake submits each file independently and retains an outcome per file; invalid or failed files do not cancel successful siblings. Screening history supports server-side candidate/email/job search, status/job/date filters, 50-row pagination, and polling of current page statuses. Recruiters can compare two to four completed reports. CSV export is tenant-authorized, formula-escaped, limited to 5,000 rows per export, and includes only candidate name/email, job, status, scores, creation time, and screening ID. Owners/admins schedule deletion; private files are erased by the cleanup worker. Cleanup failures are bounded and leave a durable failed cleanup record for operator follow-up.

## Resume processing behavior

The worker claims screening rows through lease-protected service-role RPCs, downloads only the recorded private Storage path, and checks the intake SHA-256 before parsing. PyMuPDF validates document structure and extracts page text and layout. Sparse/image pages use bounded OCR. Encrypted, corrupt, excessive-page, unreadable, or over-limit documents fail with a stable error code. OCR, network calls, and AI retries are bounded. The worker stores only safe error codes in processing records and does not log resume text, AI output, credentials, or contact details.

Groq receives contact-redacted page text and job criteria as untrusted data. Prompts explicitly reject embedded instructions and tool use; the model has no tool interface. Output must match strict Pydantic schemas, and every assessment quote must occur on the cited page. Invalid output is retried once (configurable up to three attempts) then the job fails without a report. Candidate contact details are extracted locally. External profile URLs are only parsed and are never fetched. Layout flags have explanations and uncertainty; they are not findings of misconduct.

Migrations add worker claim/cache/completion/failure RPCs and recoverable resume cleanup. Apply reviewed migrations in order with `supabase db reset` for local Supabase or `supabase db push` for a linked hosted project. Worker execution requires a real Supabase project with Auth, migrations, private bucket, RLS, and server credentials plus a valid Groq key. Without them, the web UI and intake may be exercised, but real processing cannot complete.

## RLS and integration verification

RLS is enabled on every business table. Composite foreign keys prevent cross-organization relationships; policies scope reads and mutations by `auth.uid()` and membership role. Definer functions use a fixed empty `search_path`, explicit grants, and caller identity checks. The app uses user-scoped requests; ordinary application requests do not use service-role bypass.

After `supabase db reset`, run `supabase test db` to verify tenant isolation, competing queue claims, lease recovery, attempt exhaustion, deletion scrubbing, and durable cleanup scheduling. Static and isolated API/worker checks run with `uv run pytest`. To test the hosted integration, configure credentials locally, provision a confirmed Auth user and organization, then verify sign-in, cross-tenant denial, private upload/download, worker processing, cleanup, and retention using non-production test data. Local unit tests and SQL source inspection do not count as hosted integration verification.

## Deployment

`render.yaml` selects Docker. Render builds the root `Dockerfile`; its locked dependency step is `uv sync --frozen --no-dev --no-install-project`. The image installs Tesseract and English language data, then runs Uvicorn on `0.0.0.0:$PORT`; health check path is `/health`. The Blueprint sets `APP_ENV=production`, `SCREENING_WORKER_ENABLED=true`, and `LOG_LEVEL=INFO`. It prompts for `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, and `GROQ_API_KEY` because those values use `sync: false`; enter them in the Render Dashboard, never the YAML. Same-origin hosting needs no CORS entry; configure exact HTTPS origins only for a separate browser origin. Confirm migrations, Auth users, private bucket, and RLS tests before deployment. No deployment has been performed or claimed.

The Docker base image pins Python and uv versions, but the Debian package repository is not snapshot-pinned. The worker runs in the web container; Render restarts and free-instance sleep can delay processing. Current rate limits are bounded and process-local, so separate replicas do not share counters. Hosted Supabase/Groq/OCR verification is still required before release.
