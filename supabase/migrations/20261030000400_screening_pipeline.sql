-- Persist reusable extracted job requirements and provide a tightly scoped
-- service-role queue interface for the server-side screening worker.
alter table public.jobs
    add column ai_requirements jsonb,
    add column ai_requirements_version text;

alter table public.jobs
    add constraint jobs_ai_requirements_object_check
        check (ai_requirements is null or jsonb_typeof(ai_requirements) = 'object');

create or replace function private.invalidate_ai_job_requirements()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if row(new.title, new.description, new.required_skills, new.preferred_skills,
           new.minimum_experience_years, new.qualifications, new.responsibilities)
       is distinct from
       row(old.title, old.description, old.required_skills, old.preferred_skills,
           old.minimum_experience_years, old.qualifications, old.responsibilities) then
        new.ai_requirements := null;
        new.ai_requirements_version := null;
    end if;
    return new;
end;
$$;

create trigger invalidate_ai_job_requirements
before update of title, description, required_skills, preferred_skills,
    minimum_experience_years, qualifications, responsibilities on public.jobs
for each row execute function private.invalidate_ai_job_requirements();

create or replace function public.claim_screening_job(p_worker_id text, p_lease_seconds integer default 900)
returns table (
    processing_job_id uuid,
    screening_id uuid,
    organization_id uuid,
    job_id uuid,
    candidate_id uuid,
    resume_document_id uuid,
    storage_path text,
    content_sha256 text,
    job_title text,
    job_description text,
    required_skills text[],
    preferred_skills text[],
    minimum_experience_years numeric,
    qualifications text,
    responsibilities text,
    ai_requirements jsonb,
    ai_requirements_version text,
    attempt_count integer,
    max_attempts integer
)
language plpgsql
security definer
set search_path = ''
as $$
begin
    if (select auth.role()) is distinct from 'service_role'
       or p_worker_id is null
       or length(p_worker_id) not between 1 and 128
       or p_lease_seconds not between 60 and 3600 then
        raise exception using errcode = '42501', message = 'worker access denied';
    end if;

    update public.processing_jobs q
       set status = 'failed', completed_at = now(), claimed_at = null,
           lease_expires_at = null, worker_id = null, last_error_code = 'attempts_exhausted'
     where q.attempt_count >= q.max_attempts
       and (
           q.status in ('queued', 'retryable_failure')
           or (q.status = 'processing' and q.lease_expires_at <= now())
       );

    update public.screening_results s
       set status = 'failed'
     where s.id in (
         select q.screening_id from public.processing_jobs q
          where q.status = 'failed' and q.last_error_code = 'attempts_exhausted'
            and s.id = q.screening_id
     )
       and s.status in ('queued', 'processing', 'retryable_failure');

    return query
    with picked as (
        select q.id
          from public.processing_jobs q
         where q.attempt_count < q.max_attempts
           and exists (
               select 1
                 from public.screening_results eligible_s
                 join public.resume_documents eligible_r
                   on eligible_r.id = eligible_s.resume_document_id
                  and eligible_r.organization_id = eligible_s.organization_id
                where eligible_s.id = q.screening_id
                  and eligible_s.deleted_at is null
                  and eligible_r.status = 'available'
                  and eligible_r.deleted_at is null
           )
           and (
               (q.status in ('queued', 'retryable_failure') and q.available_at <= now())
               or (q.status = 'processing' and q.lease_expires_at <= now())
           )
         order by q.available_at, q.created_at
         for update skip locked
         limit 1
    ), claimed as (
        update public.processing_jobs q
           set status = 'processing', attempt_count = q.attempt_count + 1,
               claimed_at = now(), lease_expires_at = now() + make_interval(secs => p_lease_seconds),
               worker_id = p_worker_id, completed_at = null, last_error_code = null
          from picked
         where q.id = picked.id
        returning q.*
    ), marked as (
        update public.screening_results s
           set status = 'processing'
          from claimed c
         where s.id = c.screening_id
        returning s.id
    )
    select c.id, c.screening_id, c.organization_id, s.job_id, s.candidate_id,
           s.resume_document_id, r.storage_path, r.content_sha256,
           j.title, j.description, j.required_skills, j.preferred_skills,
           j.minimum_experience_years, j.qualifications, j.responsibilities,
           j.ai_requirements, j.ai_requirements_version, c.attempt_count, c.max_attempts
      from claimed c
      join public.screening_results s on s.id = c.screening_id
      join public.jobs j on j.id = s.job_id and j.organization_id = s.organization_id
      join public.resume_documents r on r.id = s.resume_document_id and r.organization_id = s.organization_id
      join marked m on m.id = s.id
     ;
end;
$$;

create or replace function public.save_job_requirements(
    p_job_id uuid, p_organization_id uuid, p_requirements jsonb, p_version text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_result jsonb;
begin
    if (select auth.role()) is distinct from 'service_role'
       or jsonb_typeof(p_requirements) <> 'object'
       or length(p_version) not between 1 and 64 then
        raise exception using errcode = '42501', message = 'worker access denied';
    end if;
    update public.jobs j
       set ai_requirements = coalesce(j.ai_requirements, p_requirements),
           ai_requirements_version = coalesce(j.ai_requirements_version, p_version)
     where j.id = p_job_id and j.organization_id = p_organization_id and j.deleted_at is null
     returning j.ai_requirements into v_result;
    if not found then
        raise exception using errcode = 'P0002', message = 'job not found';
    end if;
    return v_result;
end;
$$;

create or replace function public.complete_screening_job(
    p_processing_job_id uuid,
    p_worker_id text,
    p_report jsonb,
    p_job_match_score numeric,
    p_classification_score numeric,
    p_scoring_version text,
    p_ai_provider text,
    p_ai_model text,
    p_candidate_name text,
    p_candidate_email text
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_screening_id uuid;
    v_candidate_id uuid;
    v_organization_id uuid;
begin
    if (select auth.role()) is distinct from 'service_role'
       or jsonb_typeof(p_report) <> 'object'
       or p_job_match_score not between 0 and 10
       or p_classification_score not between 0 and 100
       or length(p_scoring_version) not between 1 and 64
       or length(p_ai_provider) not between 1 and 32
       or length(p_ai_model) not between 1 and 128 then
        raise exception using errcode = '22023', message = 'invalid screening result';
    end if;

    update public.processing_jobs q
       set status = 'completed', completed_at = now(), claimed_at = null,
           lease_expires_at = null, worker_id = null
     where q.id = p_processing_job_id and q.status = 'processing'
       and q.worker_id = p_worker_id and q.lease_expires_at > now()
     returning q.screening_id, q.organization_id into v_screening_id, v_organization_id;
    if not found then
        raise exception using errcode = '40001', message = 'processing lease expired';
    end if;

    update public.screening_results s
       set status = 'completed', job_match_score = p_job_match_score,
           classification_score = p_classification_score, report_json = p_report,
           scoring_version = p_scoring_version, ai_provider = p_ai_provider, ai_model = p_ai_model
     where s.id = v_screening_id and s.organization_id = v_organization_id
       and s.status = 'processing'
     returning s.candidate_id into v_candidate_id;
    if not found then
        raise exception using errcode = '40001', message = 'screening state changed';
    end if;

    update public.candidates c
       set full_name = coalesce(c.full_name, nullif(btrim(p_candidate_name), '')),
           contact_email = coalesce(c.contact_email, nullif(lower(btrim(p_candidate_email)), ''))
     where c.id = v_candidate_id and c.organization_id = v_organization_id and c.deleted_at is null;
end;
$$;

create or replace function public.fail_screening_job(
    p_processing_job_id uuid, p_worker_id text, p_error_code text, p_retryable boolean
)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_screening_id uuid;
    v_state text;
begin
    if (select auth.role()) is distinct from 'service_role'
       or p_error_code !~ '^[a-z0-9_]{1,64}$' then
        raise exception using errcode = '42501', message = 'worker access denied';
    end if;
    update public.processing_jobs q
       set status = case when p_retryable and q.attempt_count < q.max_attempts then 'retryable_failure' else 'failed' end,
           completed_at = case when p_retryable and q.attempt_count < q.max_attempts then null else now() end,
           available_at = case
               when p_retryable and q.attempt_count < q.max_attempts
               then now() + make_interval(secs => least(300, power(2::numeric, least(q.attempt_count, 8))::double precision))
               else q.available_at end,
           claimed_at = null, lease_expires_at = null, worker_id = null,
           last_error_code = p_error_code
     where q.id = p_processing_job_id and q.status = 'processing' and q.worker_id = p_worker_id
       and q.lease_expires_at > now()
     returning q.screening_id, q.status into v_screening_id, v_state;
    if not found then
        raise exception using errcode = '40001', message = 'processing lease expired';
    end if;
    update public.screening_results s set status = v_state where s.id = v_screening_id;
    return v_state;
end;
$$;

revoke all on function public.claim_screening_job(text, integer) from public, anon, authenticated;
revoke all on function public.save_job_requirements(uuid, uuid, jsonb, text) from public, anon, authenticated;
revoke all on function public.complete_screening_job(uuid, text, jsonb, numeric, numeric, text, text, text, text, text) from public, anon, authenticated;
revoke all on function public.fail_screening_job(uuid, text, text, boolean) from public, anon, authenticated;
grant execute on function public.claim_screening_job(text, integer) to service_role;
grant execute on function public.save_job_requirements(uuid, uuid, jsonb, text) to service_role;
grant execute on function public.complete_screening_job(uuid, text, jsonb, numeric, numeric, text, text, text, text, text) to service_role;
grant execute on function public.fail_screening_job(uuid, text, text, boolean) to service_role;
