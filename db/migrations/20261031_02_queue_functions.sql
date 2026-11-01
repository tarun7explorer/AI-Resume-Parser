-- Resume-Parser: atomic queue functions.
-- All functions are SECURITY INVOKER (the default): they run with the caller's
-- privileges, so no privilege escalation is possible. Each pins search_path and
-- schema-qualifies every object. EXECUTE is revoked from PUBLIC.
--
-- Fencing rule: a worker may only change a job while
--   status = 'processing' AND lease_owner = <worker> AND attempts = <attempt>.
-- `attempts` increments on every claim/reclaim, so a worker whose lease was
-- taken over can never write over the newer attempt.

-- ---------------------------------------------------------------------------
-- claim_next_job: atomically claim one due job (queued, or processing with an
-- expired lease). Jobs whose lease expired with no attempts left are failed.
-- ---------------------------------------------------------------------------
CREATE FUNCTION public.claim_next_job(p_worker_id text, p_lease_seconds integer)
RETURNS SETOF public.screening_jobs
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_now timestamptz := clock_timestamp();
BEGIN
    IF p_worker_id IS NULL OR btrim(p_worker_id) = '' OR char_length(p_worker_id) > 128 THEN
        RAISE EXCEPTION 'invalid worker id' USING ERRCODE = '22023';
    END IF;
    IF p_lease_seconds IS NULL OR p_lease_seconds < 1 OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'lease seconds must be between 1 and 3600' USING ERRCODE = '22023';
    END IF;

    -- Reap: expired lease and no attempts left -> terminal failure.
    WITH reaped AS (
        UPDATE public.screening_jobs
           SET status = 'failed',
               error_code = 'lease_expired',
               error_message = 'Processing did not finish before the retry limit was reached.',
               completed_at = v_now,
               lease_owner = NULL,
               lease_expires_at = NULL
         WHERE id IN (
                SELECT s.id FROM public.screening_jobs s
                 WHERE s.status = 'processing'
                   AND s.lease_expires_at <= v_now
                   AND s.attempts >= s.max_attempts
                   FOR UPDATE SKIP LOCKED)
        RETURNING id, attempts
    )
    INSERT INTO public.audit_events (job_id, event_type, detail)
    SELECT id, 'job.failed',
           jsonb_build_object('reason', 'lease_expired', 'attempts', attempts)
      FROM reaped;

    -- Claim exactly one due job; concurrent claimers skip locked rows.
    RETURN QUERY
    WITH candidate AS (
        SELECT s.id, s.status AS prior_status
          FROM public.screening_jobs s
         WHERE s.attempts < s.max_attempts
           AND ((s.status = 'queued' AND s.next_attempt_at <= v_now)
             OR (s.status = 'processing' AND s.lease_expires_at <= v_now))
         ORDER BY s.next_attempt_at, s.created_at
         LIMIT 1
         FOR UPDATE SKIP LOCKED
    ), claimed AS (
        UPDATE public.screening_jobs j
           SET status = 'processing',
               attempts = j.attempts + 1,
               lease_owner = p_worker_id,
               lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
               started_at = v_now
          FROM candidate c
         WHERE j.id = c.id
        RETURNING j.*, c.prior_status
    ), audit AS (
        INSERT INTO public.audit_events (job_id, event_type, detail)
        SELECT cl.id, 'job.claimed',
               jsonb_build_object('worker', p_worker_id,
                                  'attempt', cl.attempts,
                                  'reclaimed', cl.prior_status = 'processing')
          FROM claimed cl
        RETURNING 1
    )
    SELECT cl.id, cl.status, cl.created_at, cl.updated_at, cl.started_at,
           cl.completed_at, cl.attempts, cl.max_attempts, cl.next_attempt_at,
           cl.lease_owner, cl.lease_expires_at, cl.finished_by, cl.finished_attempt,
           cl.error_code, cl.error_message, cl.result, cl.retention_expires_at
      FROM claimed cl;
END
$$;

-- ---------------------------------------------------------------------------
-- heartbeat_job: extend the lease; returns false if the caller lost the job.
-- ---------------------------------------------------------------------------
CREATE FUNCTION public.heartbeat_job(
    p_job_id uuid, p_worker_id text, p_attempt integer, p_lease_seconds integer)
RETURNS boolean
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_rows integer;
BEGIN
    IF p_lease_seconds IS NULL OR p_lease_seconds < 1 OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'lease seconds must be between 1 and 3600' USING ERRCODE = '22023';
    END IF;

    UPDATE public.screening_jobs
       SET lease_expires_at = clock_timestamp() + make_interval(secs => p_lease_seconds)
     WHERE id = p_job_id
       AND status = 'processing'
       AND lease_owner = p_worker_id
       AND attempts = p_attempt;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows = 1;
END
$$;

-- ---------------------------------------------------------------------------
-- complete_job: idempotent, fenced completion.
-- Returns 'completed' | 'already_completed' | 'stale' | 'missing'.
-- ---------------------------------------------------------------------------
CREATE FUNCTION public.complete_job(
    p_job_id uuid, p_worker_id text, p_attempt integer, p_result jsonb)
RETURNS text
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_rows   integer;
    v_status text;
    v_by     text;
    v_att    integer;
BEGIN
    IF p_result IS NULL OR jsonb_typeof(p_result) <> 'object' THEN
        RAISE EXCEPTION 'result must be a JSON object' USING ERRCODE = '22023';
    END IF;

    UPDATE public.screening_jobs
       SET status = 'completed',
           result = p_result,
           completed_at = clock_timestamp(),
           lease_owner = NULL,
           lease_expires_at = NULL,
           finished_by = p_worker_id,
           finished_attempt = p_attempt,
           error_code = NULL,
           error_message = NULL
     WHERE id = p_job_id
       AND status = 'processing'
       AND lease_owner = p_worker_id
       AND attempts = p_attempt;
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    IF v_rows = 1 THEN
        INSERT INTO public.audit_events (job_id, event_type, detail)
        VALUES (p_job_id, 'job.completed',
                jsonb_build_object('worker', p_worker_id, 'attempt', p_attempt));
        RETURN 'completed';
    END IF;

    SELECT status, finished_by, finished_attempt
      INTO v_status, v_by, v_att
      FROM public.screening_jobs WHERE id = p_job_id;
    IF NOT FOUND THEN
        RETURN 'missing';
    END IF;
    IF v_status = 'completed' AND v_by = p_worker_id AND v_att = p_attempt THEN
        RETURN 'already_completed';
    END IF;
    RETURN 'stale';
END
$$;

-- ---------------------------------------------------------------------------
-- fail_job: fenced failure. Retryable errors with attempts left re-queue the
-- job after a backoff; otherwise the job becomes terminally failed.
-- Returns 'requeued' | 'failed' | 'already_failed' | 'stale' | 'missing'.
-- ---------------------------------------------------------------------------
CREATE FUNCTION public.fail_job(
    p_job_id uuid, p_worker_id text, p_attempt integer,
    p_error_code text, p_error_message text,
    p_retryable boolean, p_backoff_seconds integer)
RETURNS text
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_now      timestamptz := clock_timestamp();
    v_attempts integer;
    v_max      integer;
    v_status   text;
    v_by       text;
    v_att      integer;
    v_message  text := left(coalesce(p_error_message, ''), 500);
    v_backoff  integer := greatest(0, least(coalesce(p_backoff_seconds, 0), 86400));
BEGIN
    IF p_error_code IS NULL OR p_error_code !~ '^[a-z0-9_]{1,64}$' THEN
        RAISE EXCEPTION 'invalid error code' USING ERRCODE = '22023';
    END IF;

    SELECT attempts, max_attempts
      INTO v_attempts, v_max
      FROM public.screening_jobs
     WHERE id = p_job_id
       AND status = 'processing'
       AND lease_owner = p_worker_id
       AND attempts = p_attempt
       FOR UPDATE;

    IF NOT FOUND THEN
        SELECT status, finished_by, finished_attempt
          INTO v_status, v_by, v_att
          FROM public.screening_jobs WHERE id = p_job_id;
        IF NOT FOUND THEN
            RETURN 'missing';
        END IF;
        IF v_status = 'failed' AND v_by = p_worker_id AND v_att = p_attempt THEN
            RETURN 'already_failed';
        END IF;
        RETURN 'stale';
    END IF;

    IF coalesce(p_retryable, false) AND v_attempts < v_max THEN
        UPDATE public.screening_jobs
           SET status = 'queued',
               lease_owner = NULL,
               lease_expires_at = NULL,
               next_attempt_at = v_now + make_interval(secs => v_backoff),
               error_code = p_error_code,
               error_message = v_message
         WHERE id = p_job_id;
        INSERT INTO public.audit_events (job_id, event_type, detail)
        VALUES (p_job_id, 'job.requeued',
                jsonb_build_object('worker', p_worker_id, 'attempt', p_attempt,
                                   'error_code', p_error_code));
        RETURN 'requeued';
    END IF;

    UPDATE public.screening_jobs
       SET status = 'failed',
           completed_at = v_now,
           lease_owner = NULL,
           lease_expires_at = NULL,
           finished_by = p_worker_id,
           finished_attempt = p_attempt,
           error_code = p_error_code,
           error_message = v_message
     WHERE id = p_job_id;
    INSERT INTO public.audit_events (job_id, event_type, detail)
    VALUES (p_job_id, 'job.failed',
            jsonb_build_object('worker', p_worker_id, 'attempt', p_attempt,
                               'error_code', p_error_code));
    RETURN 'failed';
END
$$;

-- ---------------------------------------------------------------------------
-- Permissions: nobody but the owner by default.
-- If the optional roles exist (see db/optional_roles.sql) grant the worker role.
-- ---------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.claim_next_job(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.heartbeat_job(uuid, text, integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.complete_job(uuid, text, integer, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.fail_job(uuid, text, integer, text, text, boolean, integer) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'resume_worker') THEN
        GRANT EXECUTE ON FUNCTION public.claim_next_job(text, integer) TO resume_worker;
        GRANT EXECUTE ON FUNCTION public.heartbeat_job(uuid, text, integer, integer) TO resume_worker;
        GRANT EXECUTE ON FUNCTION public.complete_job(uuid, text, integer, jsonb) TO resume_worker;
        GRANT EXECUTE ON FUNCTION public.fail_job(uuid, text, integer, text, text, boolean, integer) TO resume_worker;
    END IF;
END
$$;