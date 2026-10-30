-- Recoverable retry, private-object cleanup, and retention workflows.

alter table public.resume_documents
    alter column retention_due_at set default (now() + interval '365 days');
update public.resume_documents
   set retention_due_at = created_at + interval '365 days'
 where retention_due_at is null and deleted_at is null;

-- Queueing begins inside the intake transaction, before the object upload can
-- finish. Replace the earlier claim RPC so it never leases awaiting uploads.
drop function if exists public.claim_screening_job(text, integer);
create function public.claim_screening_job(p_worker_id text, p_lease_seconds integer default 900)
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
       and (q.status in ('queued', 'retryable_failure')
         or (q.status = 'processing' and q.lease_expires_at <= now()));

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
           and ((q.status in ('queued', 'retryable_failure') and q.available_at <= now())
             or (q.status = 'processing' and q.lease_expires_at <= now()))
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
      join marked m on m.id = s.id;
end;
$$;
revoke all on function public.claim_screening_job(text, integer) from public, anon, authenticated;
grant execute on function public.claim_screening_job(text, integer) to service_role;

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
drop trigger if exists invalidate_ai_job_requirements on public.jobs;
create trigger invalidate_ai_job_requirements
before update of title, description, required_skills, preferred_skills,
    minimum_experience_years, qualifications, responsibilities on public.jobs
for each row execute function private.invalidate_ai_job_requirements();

create table public.storage_cleanup_jobs (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete restrict,
    resume_document_id uuid not null unique references public.resume_documents(id) on delete restrict,
    storage_path text not null,
    status text not null default 'queued'
        check (status in ('queued', 'processing', 'retryable_failure', 'completed', 'failed')),
    attempt_count integer not null default 0 check (attempt_count >= 0),
    max_attempts integer not null default 8 check (max_attempts between 1 and 12),
    available_at timestamptz not null default now(),
    claimed_at timestamptz,
    lease_expires_at timestamptz,
    worker_id text,
    last_error_code text check (last_error_code is null or last_error_code ~ '^[a-z0-9_]{1,64}$'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz,
    check ((status = 'processing') = (claimed_at is not null)),
    check ((status = 'processing') = (lease_expires_at is not null)),
    check ((status in ('completed', 'failed')) = (completed_at is not null))
);

create index storage_cleanup_claim_idx
    on public.storage_cleanup_jobs(available_at, created_at)
    where status in ('queued', 'retryable_failure');
create index storage_cleanup_lease_idx
    on public.storage_cleanup_jobs(lease_expires_at)
    where status = 'processing';

alter table public.storage_cleanup_jobs enable row level security;
revoke all on table public.storage_cleanup_jobs from public, anon, authenticated;
grant all on table public.storage_cleanup_jobs to service_role;
create trigger storage_cleanup_jobs_touch_updated_at
    before update on public.storage_cleanup_jobs
    for each row execute function private.touch_updated_at();

create or replace function private.queue_resume_cleanup(p_resume_id uuid, p_actor_id uuid)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_doc public.resume_documents%rowtype;
begin
    select * into v_doc from public.resume_documents d
     where d.id = p_resume_id
     for update;
    if not found then
        return false;
    end if;

    if v_doc.deleted_at is null then
        update public.resume_documents
           set status = 'deleted', deleted_at = now()
         where id = p_resume_id;
        update public.screening_results
           set status = 'failed', job_match_score = null, classification_score = null,
               report_json = null, scoring_version = null, ai_provider = null, ai_model = null,
               deleted_at = now()
         where resume_document_id = p_resume_id and deleted_at is null;
        update public.processing_jobs
           set status = 'failed', completed_at = now(), last_error_code = 'resume_deleted',
               claimed_at = null, lease_expires_at = null, worker_id = null
         where screening_id in (
             select s.id from public.screening_results s where s.resume_document_id = p_resume_id
         ) and status in ('queued', 'retryable_failure', 'processing');
    end if;

    insert into public.storage_cleanup_jobs(organization_id, resume_document_id, storage_path)
    values (v_doc.organization_id, v_doc.id, v_doc.storage_path)
    on conflict (resume_document_id) do nothing;

    if not exists (
        select 1 from public.resume_documents d
         where d.candidate_id = v_doc.candidate_id and d.deleted_at is null
    ) then
        update public.candidates
           set full_name = null, contact_email = null, deleted_at = coalesce(deleted_at, now())
         where id = v_doc.candidate_id;
    end if;
    return true;
end;
$$;
revoke all on function private.queue_resume_cleanup(uuid, uuid) from public, anon, authenticated;
revoke all on function public.delete_resume_document(uuid) from authenticated;
revoke all on function public.fail_resume_upload(uuid) from authenticated;

create or replace function public.schedule_resume_deletion(p_organization_id uuid, p_resume_id uuid)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_doc public.resume_documents%rowtype;
    v_status text;
begin
    if (select auth.uid()) is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    select * into v_doc from public.resume_documents d
     where d.id = p_resume_id and d.organization_id = p_organization_id
     for update;
    if not found or not private.has_org_role(p_organization_id, array['owner', 'admin']) then
        raise exception using errcode = '42501', message = 'resume access denied';
    end if;
    perform private.queue_resume_cleanup(v_doc.id, (select auth.uid()));
    select q.status into v_status from public.storage_cleanup_jobs q
     where q.resume_document_id = p_resume_id;
    return coalesce(v_status, 'queued');
end;
$$;

create or replace function public.enqueue_expired_resume_cleanup(p_limit integer default 100)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_doc record;
    v_count integer := 0;
begin
    if (select auth.role()) is distinct from 'service_role' or p_limit not between 1 and 500 then
        raise exception using errcode = '42501', message = 'worker access denied';
    end if;
    for v_doc in
        select d.id, d.organization_id
          from public.resume_documents d
         where d.deleted_at is null and (
             (d.status = 'available' and d.retention_due_at <= now())
             or (d.status = 'awaiting_upload' and d.created_at <= now() - interval '60 minutes')
         )
         order by coalesce(d.retention_due_at, d.created_at), d.created_at
         for update skip locked
         limit p_limit
    loop
        perform private.queue_resume_cleanup(v_doc.id, null);
        v_count := v_count + 1;
    end loop;
    return v_count;
end;
$$;

create or replace function public.claim_storage_cleanup_job(p_worker_id text, p_lease_seconds integer default 300)
returns table(cleanup_job_id uuid, storage_path text, attempt_count integer, max_attempts integer)
language plpgsql
security definer
set search_path = ''
as $$
begin
    if (select auth.role()) is distinct from 'service_role'
       or p_worker_id is null or length(p_worker_id) not between 1 and 128
       or p_lease_seconds not between 60 and 1800 then
        raise exception using errcode = '42501', message = 'worker access denied';
    end if;
    update public.storage_cleanup_jobs q
       set status = 'failed', completed_at = now(), claimed_at = null,
           lease_expires_at = null, worker_id = null, last_error_code = 'attempts_exhausted'
     where q.attempt_count >= q.max_attempts
       and (q.status in ('queued', 'retryable_failure')
         or (q.status = 'processing' and q.lease_expires_at <= now()));

    return query
    with picked as (
        select q.id from public.storage_cleanup_jobs q
         where q.attempt_count < q.max_attempts and (
             (q.status in ('queued', 'retryable_failure') and q.available_at <= now())
             or (q.status = 'processing' and q.lease_expires_at <= now())
         )
         order by q.available_at, q.created_at
         for update skip locked limit 1
    ), claimed as (
        update public.storage_cleanup_jobs q
           set status = 'processing', attempt_count = q.attempt_count + 1,
               claimed_at = now(), lease_expires_at = now() + make_interval(secs => p_lease_seconds),
               worker_id = p_worker_id, completed_at = null, last_error_code = null
          from picked where q.id = picked.id
        returning q.id, q.storage_path, q.attempt_count, q.max_attempts
    )
    select * from claimed;
end;
$$;

create or replace function public.complete_storage_cleanup_job(p_cleanup_job_id uuid, p_worker_id text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
    if (select auth.role()) is distinct from 'service_role' then
        raise exception using errcode = '42501', message = 'worker access denied';
    end if;
    update public.storage_cleanup_jobs
       set status = 'completed', completed_at = now(), claimed_at = null,
           lease_expires_at = null, worker_id = null
     where id = p_cleanup_job_id and status = 'processing'
       and worker_id = p_worker_id and lease_expires_at > now();
    if not found then
        raise exception using errcode = '40001', message = 'cleanup lease expired';
    end if;
end;
$$;

create or replace function public.fail_storage_cleanup_job(
    p_cleanup_job_id uuid, p_worker_id text, p_error_code text, p_retryable boolean
)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_state text;
begin
    if (select auth.role()) is distinct from 'service_role'
       or p_error_code !~ '^[a-z0-9_]{1,64}$' then
        raise exception using errcode = '42501', message = 'worker access denied';
    end if;
    update public.storage_cleanup_jobs q
       set status = case when p_retryable and q.attempt_count < q.max_attempts then 'retryable_failure' else 'failed' end,
           completed_at = case when p_retryable and q.attempt_count < q.max_attempts then null else now() end,
           available_at = case when p_retryable and q.attempt_count < q.max_attempts
               then now() + make_interval(secs => least(1800, power(2::numeric, least(q.attempt_count, 10))::double precision))
               else q.available_at end,
           claimed_at = null, lease_expires_at = null, worker_id = null,
           last_error_code = p_error_code
     where q.id = p_cleanup_job_id and q.status = 'processing' and q.worker_id = p_worker_id
       and q.lease_expires_at > now()
     returning q.status into v_state;
    if not found then
        raise exception using errcode = '40001', message = 'cleanup lease expired';
    end if;
    return v_state;
end;
$$;

create or replace function public.retry_screening_job(p_organization_id uuid, p_screening_id uuid)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_status text;
begin
    if (select auth.uid()) is null
       or not private.has_org_role(p_organization_id, array['owner', 'admin', 'recruiter']) then
        raise exception using errcode = '42501', message = 'screening access denied';
    end if;
    update public.processing_jobs q set available_at = now()
      from public.screening_results s
     where q.screening_id = s.id and q.organization_id = p_organization_id
       and s.id = p_screening_id and s.organization_id = p_organization_id
       and s.status = 'retryable_failure' and q.status = 'retryable_failure'
       and q.attempt_count < q.max_attempts
    returning q.status into v_status;
    if not found then
        raise exception using errcode = 'P0002', message = 'screening is not retryable';
    end if;
    return v_status;
end;
$$;

revoke all on function public.schedule_resume_deletion(uuid, uuid) from public, anon;
revoke all on function public.enqueue_expired_resume_cleanup(integer) from public, anon, authenticated;
revoke all on function public.claim_storage_cleanup_job(text, integer) from public, anon, authenticated;
revoke all on function public.complete_storage_cleanup_job(uuid, text) from public, anon, authenticated;
revoke all on function public.fail_storage_cleanup_job(uuid, text, text, boolean) from public, anon, authenticated;
revoke all on function public.retry_screening_job(uuid, uuid) from public, anon;
grant execute on function public.schedule_resume_deletion(uuid, uuid) to authenticated;
grant execute on function public.enqueue_expired_resume_cleanup(integer) to service_role;
grant execute on function public.claim_storage_cleanup_job(text, integer) to service_role;
grant execute on function public.complete_storage_cleanup_job(uuid, text) to service_role;
grant execute on function public.fail_storage_cleanup_job(uuid, text, text, boolean) to service_role;
grant execute on function public.retry_screening_job(uuid, uuid) to authenticated;
