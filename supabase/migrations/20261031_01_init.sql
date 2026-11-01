-- Resume-Parser: base schema.
-- Ordinary PostgreSQL (13+). No Supabase roles, RPC, RLS or Storage objects.
-- Applied by app/migrate.py inside a single transaction.

-- ---------------------------------------------------------------------------
-- screening_jobs: durable queue + result record
-- ---------------------------------------------------------------------------
CREATE TABLE public.screening_jobs (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    status               text        NOT NULL DEFAULT 'queued',
    created_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at           timestamptz,
    completed_at         timestamptz,
    attempts             integer     NOT NULL DEFAULT 0,
    max_attempts         integer     NOT NULL DEFAULT 3,
    next_attempt_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_owner          text,
    lease_expires_at     timestamptz,
    -- Identity of the worker attempt that wrote the terminal state. Used for
    -- idempotent completion and to reject late/stale workers.
    finished_by          text,
    finished_attempt     integer,
    error_code           text,
    error_message        text,
    result               jsonb,
    retention_expires_at timestamptz NOT NULL,

    CONSTRAINT screening_jobs_status_valid
        CHECK (status IN ('queued', 'processing', 'completed', 'failed')),
    CONSTRAINT screening_jobs_attempts_valid
        CHECK (attempts >= 0 AND max_attempts >= 1 AND max_attempts <= 20
               AND attempts <= max_attempts),
    CONSTRAINT screening_jobs_error_code_format
        CHECK (error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,64}$'),
    CONSTRAINT screening_jobs_error_message_len
        CHECK (error_message IS NULL OR char_length(error_message) <= 500),
    CONSTRAINT screening_jobs_result_is_object
        CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
    CONSTRAINT screening_jobs_lease_owner_len
        CHECK (lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 128),
    -- Each state has a fixed "shape" so inconsistent rows cannot exist.
    CONSTRAINT screening_jobs_state_shape CHECK (
        (status = 'queued'
            AND lease_owner IS NULL AND lease_expires_at IS NULL
            AND result IS NULL AND completed_at IS NULL)
        OR (status = 'processing'
            AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
            AND started_at IS NOT NULL AND attempts >= 1
            AND result IS NULL AND completed_at IS NULL)
        OR (status = 'completed'
            AND result IS NOT NULL AND completed_at IS NOT NULL
            AND lease_owner IS NULL AND lease_expires_at IS NULL
            AND error_code IS NULL)
        OR (status = 'failed'
            AND error_code IS NOT NULL AND completed_at IS NOT NULL
            AND lease_owner IS NULL AND lease_expires_at IS NULL
            AND result IS NULL)
    )
);

-- Claim path: queued jobs that are due, oldest first.
CREATE INDEX screening_jobs_claim_queued_idx
    ON public.screening_jobs (next_attempt_at, created_at)
    WHERE status = 'queued';
-- Claim/reap path: processing jobs whose lease may have expired.
CREATE INDEX screening_jobs_lease_idx
    ON public.screening_jobs (lease_expires_at)
    WHERE status = 'processing';
-- Cleanup path: terminal jobs past retention.
CREATE INDEX screening_jobs_retention_idx
    ON public.screening_jobs (retention_expires_at)
    WHERE status IN ('completed', 'failed');

-- State-machine guard (defence in depth; the queue functions also enforce it).
--   queued     -> processing | failed
--   processing -> processing (lease renew / reclaim) | queued | completed | failed
--   completed / failed are terminal and immutable.
CREATE FUNCTION public.enforce_job_transition() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'screening_jobs.id is immutable'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.attempts < OLD.attempts THEN
        RAISE EXCEPTION 'screening_jobs.attempts cannot decrease'
            USING ERRCODE = 'check_violation';
    END IF;

    IF OLD.status IN ('completed', 'failed') THEN
        IF NEW.status IS DISTINCT FROM OLD.status
           OR NEW.result IS DISTINCT FROM OLD.result
           OR NEW.error_code IS DISTINCT FROM OLD.error_code
           OR NEW.attempts IS DISTINCT FROM OLD.attempts THEN
            RAISE EXCEPTION 'terminal job state (%) is immutable', OLD.status
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF OLD.status = 'queued' THEN
        IF NEW.status NOT IN ('queued', 'processing', 'failed') THEN
            RAISE EXCEPTION 'illegal job state transition % -> %', OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF OLD.status = 'processing' THEN
        IF NEW.status NOT IN ('processing', 'queued', 'completed', 'failed') THEN
            RAISE EXCEPTION 'illegal job state transition % -> %', OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END
$$;

CREATE TRIGGER screening_jobs_guard
    BEFORE UPDATE ON public.screening_jobs
    FOR EACH ROW EXECUTE FUNCTION public.enforce_job_transition();

-- ---------------------------------------------------------------------------
-- screening_inputs: uploaded PDFs / pasted JD text (strict MVP: stored in DB)
-- ---------------------------------------------------------------------------
CREATE TABLE public.screening_inputs (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          uuid        NOT NULL
                    REFERENCES public.screening_jobs (id) ON DELETE CASCADE,
    input_type      text        NOT NULL,
    position        integer     NOT NULL DEFAULT 0,
    candidate_label text,
    filename        text,
    content_type    text,
    size_bytes      integer     NOT NULL,
    sha256          char(64)    NOT NULL,
    content         bytea,
    text_content    text,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),

    CONSTRAINT screening_inputs_type_valid
        CHECK (input_type IN ('jd_text', 'jd_pdf', 'resume_pdf')),
    CONSTRAINT screening_inputs_position_valid CHECK (position >= 0),
    CONSTRAINT screening_inputs_label_len
        CHECK (candidate_label IS NULL OR char_length(candidate_label) <= 100),
    CONSTRAINT screening_inputs_filename_len
        CHECK (filename IS NULL OR char_length(filename) <= 255),
    CONSTRAINT screening_inputs_content_type_len
        CHECK (content_type IS NULL OR char_length(content_type) <= 100),
    -- Hard backstop (50 MiB); the application enforces much smaller limits.
    CONSTRAINT screening_inputs_size_valid
        CHECK (size_bytes > 0 AND size_bytes <= 52428800),
    CONSTRAINT screening_inputs_sha256_format CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT screening_inputs_payload_shape CHECK (
        (input_type = 'jd_text' AND text_content IS NOT NULL AND content IS NULL
            AND octet_length(text_content) = size_bytes)
        OR (input_type IN ('jd_pdf', 'resume_pdf') AND content IS NOT NULL
            AND text_content IS NULL AND octet_length(content) = size_bytes)
    ),
    CONSTRAINT screening_inputs_unique_position UNIQUE (job_id, input_type, position)
);

-- PDFs are already compressed; skip TOAST compression.
ALTER TABLE public.screening_inputs ALTER COLUMN content SET STORAGE EXTERNAL;

-- Exactly one job description per job.
CREATE UNIQUE INDEX screening_inputs_one_jd_per_job
    ON public.screening_inputs (job_id)
    WHERE input_type IN ('jd_text', 'jd_pdf');

-- ---------------------------------------------------------------------------
-- job_access_tokens: only SHA-256 hashes are stored, never raw tokens
-- ---------------------------------------------------------------------------
CREATE TABLE public.job_access_tokens (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id     uuid        NOT NULL
               REFERENCES public.screening_jobs (id) ON DELETE CASCADE,
    token_hash bytea       NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,

    CONSTRAINT job_access_tokens_hash_len CHECK (octet_length(token_hash) = 32),
    CONSTRAINT job_access_tokens_expiry_valid CHECK (expires_at > created_at),
    CONSTRAINT job_access_tokens_hash_unique UNIQUE (token_hash)
);
CREATE INDEX job_access_tokens_job_idx ON public.job_access_tokens (job_id);

-- ---------------------------------------------------------------------------
-- audit_events: minimal security/lifecycle events (no resume text, no secrets)
-- ---------------------------------------------------------------------------
CREATE TABLE public.audit_events (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id         uuid        REFERENCES public.screening_jobs (id) ON DELETE SET NULL,
    event_type     text        NOT NULL,
    correlation_id text,
    detail         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),

    CONSTRAINT audit_events_type_format CHECK (event_type ~ '^[a-z0-9_.]{1,64}$'),
    CONSTRAINT audit_events_correlation_len
        CHECK (correlation_id IS NULL OR char_length(correlation_id) <= 64),
    CONSTRAINT audit_events_detail_object CHECK (jsonb_typeof(detail) = 'object')
);
CREATE INDEX audit_events_job_idx ON public.audit_events (job_id, created_at);
CREATE INDEX audit_events_created_idx ON public.audit_events (created_at);