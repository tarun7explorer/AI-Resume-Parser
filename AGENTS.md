# Repository Guidelines

## Source of Truth and Workspace

Before architectural decisions, read the complete `Overview.txt` and `Structure.txt`; they define product scope, stack, and intended layout. Treat them as authoritative. If they conflict or leave a consequential choice unresolved, document it and ask before choosing. Keep all project work within `D:\Resume-Parser`. The `ai-resume-screening/` tree in `Structure.txt` is relative to this workspace root. Preserve the reference documents and working functionality; inspect files before changing them.

## Architecture and Implementation

Follow the prescribed layout: FastAPI modules in `app/`, browser pages/assets in `static/`, Supabase schema and policies in `supabase/`, tests in `tests/`, and maintenance scripts in `scripts/`. Implement complete, integrated features across API, persistence, worker, and UI as applicable; avoid disconnected demos and placeholder functions. Keep scoring deterministic and versioned, validate evidence references and structured AI output, and never let an LLM independently determine final scores or hiring decisions.

Use production practices for security, validation, safe error handling, privacy-aware logging, maintainability, and performance. Treat resumes and job descriptions as untrusted input. Enforce upload limits, authorization and RLS, private file access, bounded retries, idempotency, and SSRF protection for URL checks. Validate database access, external API integrations, uploads, and AI outputs. Keep secrets in server-side environment variables; `.env.example` must contain placeholders only. Never expose credentials or resume contents in logs or frontend code. Avoid unnecessary dependencies.

## Development and Tests

The planned Python toolchain is `uv` with `pyproject.toml` and `uv.lock`; the frontend is HTML, CSS, and vanilla JavaScript. Use four-space Python indentation, PEP 8, `snake_case` names, and pytest files named `test_<area>.py`. Add meaningful unit, integration, security, parsing, scoring, and workflow tests with each feature, then run them. Never claim a feature, test, or deployment works without evidence.

Once scaffolded, baseline commands are `uv sync --frozen`, `uv run pytest`, and `uv run uvicorn app.main:app --reload`. Follow the actual README/configuration if it gives more precise commands. Verify Render build/start settings, port handling, and health checks against the deployment configuration before calling deployment ready.

## Changes and Handover

Preserve useful instructions and inspect configuration, dependencies, and tests before changing them. Use concise imperative commit subjects; no Git history is available to establish a convention. Pull requests should describe behavior and risks, link related work, list commands/results, and include UI screenshots when relevant. Document required credentials and manual account setup; never commit secrets.
