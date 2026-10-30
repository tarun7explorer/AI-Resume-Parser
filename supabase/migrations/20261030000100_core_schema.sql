-- Core tenant-scoped business entities. Auth users are provisioned separately
-- by Supabase Auth; application tables only reference auth.users.

create schema if not exists private;
revoke all on schema private from public, anon, authenticated;

create table public.organizations (
    id uuid primary key default gen_random_uuid(),
    name text not null check (length(btrim(name)) between 1 and 200),
    created_by uuid references auth.users (id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    archived_at timestamptz
);

create table public.organization_memberships (
    organization_id uuid not null references public.organizations (id) on delete cascade,
    user_id uuid not null references auth.users (id) on delete cascade,
    role text not null check (role in ('owner', 'admin', 'recruiter', 'viewer')),
    invited_by uuid references auth.users (id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (organization_id, user_id)
);

create index organization_memberships_user_idx
    on public.organization_memberships (user_id, organization_id);

create table public.jobs (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete restrict,
    title text not null check (length(btrim(title)) between 1 and 200),
    description text not null check (length(btrim(description)) between 1 and 100000),
    status text not null default 'active' check (status in ('active', 'archived')),
    created_by uuid references auth.users (id) on delete set null,
    idempotency_key uuid,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz,
    unique (id, organization_id)
);

create unique index jobs_idempotency_idx
    on public.jobs (organization_id, created_by, idempotency_key)
    where idempotency_key is not null;

create index jobs_org_status_created_idx
    on public.jobs (organization_id, status, created_at desc)
    where deleted_at is null;

create table public.candidates (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete restrict,
    full_name text check (full_name is null or length(btrim(full_name)) between 1 and 200),
    contact_email text check (contact_email is null or length(contact_email) <= 254),
    created_by uuid references auth.users (id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz,
    unique (id, organization_id)
);

create index candidates_org_created_idx
    on public.candidates (organization_id, created_at desc)
    where deleted_at is null;

create table public.resume_documents (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete restrict,
    candidate_id uuid not null,
    storage_path text not null unique,
    original_filename text not null check (length(btrim(original_filename)) between 1 and 255),
    content_type text not null default 'application/pdf' check (content_type = 'application/pdf'),
    file_size_bytes bigint not null check (file_size_bytes between 1 and 10485760),
    content_sha256 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
    status text not null default 'awaiting_upload'
        check (status in ('awaiting_upload', 'available', 'deleted')),
    created_by uuid references auth.users (id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    retention_due_at timestamptz,
    deleted_at timestamptz,
    unique (id, organization_id),
    foreign key (candidate_id, organization_id)
        references public.candidates (id, organization_id) on delete restrict,
    check (storage_path = organization_id::text || '/' || candidate_id::text || '/' || id::text || '.pdf'),
    check ((status = 'deleted') = (deleted_at is not null))
);

create index resume_documents_org_status_created_idx
    on public.resume_documents (organization_id, status, created_at desc)
    where deleted_at is null;

create index resume_documents_retention_idx
    on public.resume_documents (retention_due_at)
    where retention_due_at is not null and deleted_at is null;

create table public.screening_results (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete restrict,
    job_id uuid not null,
    candidate_id uuid not null,
    resume_document_id uuid not null,
    status text not null default 'queued'
        check (status in ('queued', 'processing', 'completed', 'retryable_failure', 'failed')),
    job_match_score numeric(4, 2) check (job_match_score between 0 and 10),
    classification_score numeric(5, 2) check (classification_score between 0 and 100),
    report_json jsonb check (report_json is null or jsonb_typeof(report_json) = 'object'),
    scoring_version text,
    ai_provider text,
    ai_model text,
    created_by uuid references auth.users (id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz,
    unique (id, organization_id),
    unique (job_id, resume_document_id),
    foreign key (job_id, organization_id)
        references public.jobs (id, organization_id) on delete restrict,
    foreign key (candidate_id, organization_id)
        references public.candidates (id, organization_id) on delete restrict,
    foreign key (resume_document_id, organization_id)
        references public.resume_documents (id, organization_id) on delete restrict,
    check (
        status <> 'completed'
        or (job_match_score is not null and classification_score is not null and report_json is not null and scoring_version is not null)
    )
);

create index screening_results_org_job_created_idx
    on public.screening_results (organization_id, job_id, created_at desc)
    where deleted_at is null;

create index screening_results_candidate_idx
    on public.screening_results (organization_id, candidate_id, created_at desc)
    where deleted_at is null;

create table public.processing_jobs (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete restrict,
    screening_id uuid not null unique,
    requested_by uuid not null references auth.users (id) on delete restrict,
    idempotency_key uuid not null,
    status text not null default 'queued'
        check (status in ('queued', 'processing', 'completed', 'retryable_failure', 'failed')),
    attempt_count integer not null default 0 check (attempt_count >= 0),
    max_attempts integer not null default 3 check (max_attempts between 1 and 10),
    available_at timestamptz not null default now(),
    claimed_at timestamptz,
    lease_expires_at timestamptz,
    worker_id text,
    last_error_code text check (last_error_code is null or last_error_code ~ '^[a-z0-9_]{1,64}$'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz,
    unique (organization_id, requested_by, idempotency_key),
    foreign key (screening_id, organization_id)
        references public.screening_results (id, organization_id) on delete restrict,
    check ((status = 'processing') = (claimed_at is not null)),
    check ((status = 'processing') = (lease_expires_at is not null)),
    check ((status in ('completed', 'failed')) = (completed_at is not null))
);

create index processing_jobs_claim_idx
    on public.processing_jobs (available_at, created_at)
    where status in ('queued', 'retryable_failure');

create index processing_jobs_expired_lease_idx
    on public.processing_jobs (lease_expires_at)
    where status = 'processing';

create table public.audit_events (
    id bigint generated always as identity primary key,
    organization_id uuid not null references public.organizations (id) on delete restrict,
    actor_user_id uuid references auth.users (id) on delete set null,
    action text not null check (action in ('insert', 'update', 'delete')),
    entity_type text not null,
    entity_id uuid not null,
    metadata jsonb not null default '{}'::jsonb check (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz not null default now()
);

create index audit_events_org_created_idx
    on public.audit_events (organization_id, created_at desc);

create index audit_events_entity_idx
    on public.audit_events (organization_id, entity_type, entity_id, created_at desc);

create or replace function private.touch_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

create or replace function private.prevent_tenant_change()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if to_jsonb(new) ->> 'id' is distinct from to_jsonb(old) ->> 'id'
       or to_jsonb(new) ->> 'organization_id' is distinct from to_jsonb(old) ->> 'organization_id'
       or to_jsonb(new) ->> 'user_id' is distinct from to_jsonb(old) ->> 'user_id'
       or (
           to_jsonb(new) ->> 'created_by' is distinct from to_jsonb(old) ->> 'created_by'
           and to_jsonb(new) ->> 'created_by' is not null
       ) then
        raise exception using errcode = '42501', message = 'immutable ownership fields';
    end if;
    return new;
end;
$$;

create trigger organizations_touch_updated_at
    before update on public.organizations
    for each row execute function private.touch_updated_at();
create trigger memberships_touch_updated_at
    before update on public.organization_memberships
    for each row execute function private.touch_updated_at();
create trigger jobs_touch_updated_at
    before update on public.jobs
    for each row execute function private.touch_updated_at();
create trigger candidates_touch_updated_at
    before update on public.candidates
    for each row execute function private.touch_updated_at();
create trigger resumes_touch_updated_at
    before update on public.resume_documents
    for each row execute function private.touch_updated_at();
create trigger screenings_touch_updated_at
    before update on public.screening_results
    for each row execute function private.touch_updated_at();
create trigger processing_jobs_touch_updated_at
    before update on public.processing_jobs
    for each row execute function private.touch_updated_at();

create trigger organizations_prevent_tenant_change
    before update on public.organizations
    for each row execute function private.prevent_tenant_change();
create trigger memberships_prevent_tenant_change
    before update on public.organization_memberships
    for each row execute function private.prevent_tenant_change();
create trigger jobs_prevent_tenant_change
    before update on public.jobs
    for each row execute function private.prevent_tenant_change();
create trigger candidates_prevent_tenant_change
    before update on public.candidates
    for each row execute function private.prevent_tenant_change();
create trigger resumes_prevent_tenant_change
    before update on public.resume_documents
    for each row execute function private.prevent_tenant_change();
create trigger screenings_prevent_tenant_change
    before update on public.screening_results
    for each row execute function private.prevent_tenant_change();
create trigger processing_jobs_prevent_tenant_change
    before update on public.processing_jobs
    for each row execute function private.prevent_tenant_change();

alter table public.organizations enable row level security;
alter table public.organization_memberships enable row level security;
alter table public.jobs enable row level security;
alter table public.candidates enable row level security;
alter table public.resume_documents enable row level security;
alter table public.screening_results enable row level security;
alter table public.processing_jobs enable row level security;
alter table public.audit_events enable row level security;

revoke all on table public.organizations from anon, authenticated;
revoke all on table public.organization_memberships from anon, authenticated;
revoke all on table public.jobs from anon, authenticated;
revoke all on table public.candidates from anon, authenticated;
revoke all on table public.resume_documents from anon, authenticated;
revoke all on table public.screening_results from anon, authenticated;
revoke all on table public.processing_jobs from anon, authenticated;
revoke all on table public.audit_events from anon, authenticated;
