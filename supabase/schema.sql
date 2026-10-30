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

-- Tenant policy helpers. SECURITY DEFINER is restricted to these read-only
-- membership/path checks; every function has a fixed empty search_path.
create or replace function private.has_org_role(p_org_id uuid, p_roles text[])
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1
        from public.organization_memberships m
        where m.organization_id = p_org_id
          and m.user_id = (select auth.uid())
          and m.role = any (p_roles)
    );
$$;

create or replace function private.can_read_resume_path(p_storage_path text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1
        from public.resume_documents d
        where d.storage_path = p_storage_path
          and d.status = 'available'
          and d.deleted_at is null
          and (d.retention_due_at is null or d.retention_due_at > now())
          and private.has_org_role(d.organization_id, array['owner', 'admin', 'recruiter', 'viewer'])
    );
$$;

create or replace function private.can_upload_resume_path(p_storage_path text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1
        from public.resume_documents d
        where d.storage_path = p_storage_path
          and d.status = 'awaiting_upload'
          and d.deleted_at is null
          and d.created_by = (select auth.uid())
          and private.has_org_role(d.organization_id, array['owner', 'admin', 'recruiter'])
    );
$$;

create or replace function private.can_delete_resume_path(p_storage_path text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1
        from public.resume_documents d
        where d.storage_path = p_storage_path
          and d.deleted_at is null
          and (
              private.has_org_role(d.organization_id, array['owner', 'admin'])
              or (
                  d.status = 'awaiting_upload'
                  and d.created_by = (select auth.uid())
                  and private.has_org_role(d.organization_id, array['recruiter'])
              )
          )
    );
$$;

revoke all on function private.has_org_role(uuid, text[]) from public, anon;
revoke all on function private.can_read_resume_path(text) from public, anon;
revoke all on function private.can_upload_resume_path(text) from public, anon;
revoke all on function private.can_delete_resume_path(text) from public, anon;
grant usage on schema private to authenticated;
grant execute on function private.has_org_role(uuid, text[]) to authenticated;
grant execute on function private.can_read_resume_path(text) to authenticated;
grant execute on function private.can_upload_resume_path(text) to authenticated;
grant execute on function private.can_delete_resume_path(text) to authenticated;

-- Small audit records never copy row contents, resume text, or credentials.
create or replace function private.write_audit_event()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_row jsonb;
    v_org_id uuid;
    v_entity_id uuid;
begin
    if tg_op = 'DELETE' then
        v_row := to_jsonb(old);
    else
        v_row := to_jsonb(new);
    end if;

    v_org_id := nullif(v_row ->> 'organization_id', '')::uuid;
    v_entity_id := coalesce(
        nullif(v_row ->> 'id', '')::uuid,
        nullif(v_row ->> 'user_id', '')::uuid
    );
    if v_org_id is not null and v_entity_id is not null then
        insert into public.audit_events (
            organization_id, actor_user_id, action, entity_type, entity_id, metadata
        ) values (
            v_org_id, (select auth.uid()), lower(tg_op), tg_table_name,
            v_entity_id, jsonb_build_object('source', 'database_trigger')
        );
    end if;

    if tg_op = 'DELETE' then
        return old;
    end if;
    return new;
end;
$$;

revoke all on function private.write_audit_event() from public, anon, authenticated;

create or replace function private.prevent_last_owner_removal()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    if old.role = 'owner'
       and (tg_op = 'DELETE' or new.role <> 'owner')
       and not exists (
           select 1
           from public.organization_memberships m
           where m.organization_id = old.organization_id
             and m.user_id <> old.user_id
             and m.role = 'owner'
       ) then
        raise exception using errcode = '23514', message = 'organization requires at least one owner';
    end if;
    if tg_op = 'DELETE' then
        return old;
    end if;
    return new;
end;
$$;

revoke all on function private.prevent_last_owner_removal() from public, anon, authenticated;
create trigger memberships_keep_owner
    before update or delete on public.organization_memberships
    for each row execute function private.prevent_last_owner_removal();

create trigger organizations_audit
    after insert or update or delete on public.organizations
    for each row execute function private.write_audit_event();
create trigger memberships_audit
    after insert or update or delete on public.organization_memberships
    for each row execute function private.write_audit_event();
create trigger jobs_audit
    after insert or update or delete on public.jobs
    for each row execute function private.write_audit_event();
create trigger candidates_audit
    after insert or update or delete on public.candidates
    for each row execute function private.write_audit_event();
create trigger resumes_audit
    after insert or update or delete on public.resume_documents
    for each row execute function private.write_audit_event();
create trigger screenings_audit
    after insert or update or delete on public.screening_results
    for each row execute function private.write_audit_event();
create trigger processing_jobs_audit
    after insert or update or delete on public.processing_jobs
    for each row execute function private.write_audit_event();

-- All public tables remain unavailable to anon. Policies are tenant-scoped;
-- policy helper functions avoid recursive reads of membership RLS.
grant usage on schema public to authenticated;

grant select, update on public.organizations to authenticated;
grant select, insert, update on public.organization_memberships to authenticated;
grant select, insert, update on public.jobs to authenticated;
grant select, insert, update on public.candidates to authenticated;
grant select, insert on public.resume_documents to authenticated;
grant select on public.screening_results, public.processing_jobs, public.audit_events to authenticated;

create policy organizations_member_read on public.organizations
    for select to authenticated
    using (private.has_org_role(id, array['owner', 'admin', 'recruiter', 'viewer']));
create policy organizations_owner_update on public.organizations
    for update to authenticated
    using (private.has_org_role(id, array['owner']))
    with check (private.has_org_role(id, array['owner']));

create policy memberships_self_or_admin_read on public.organization_memberships
    for select to authenticated
    using (
        user_id = (select auth.uid())
        or private.has_org_role(organization_id, array['owner', 'admin'])
    );
create policy memberships_owner_insert on public.organization_memberships
    for insert to authenticated
    with check (private.has_org_role(organization_id, array['owner']));
create policy memberships_owner_update on public.organization_memberships
    for update to authenticated
    using (private.has_org_role(organization_id, array['owner']))
    with check (private.has_org_role(organization_id, array['owner']));

create policy jobs_member_read on public.jobs
    for select to authenticated
    using (
        deleted_at is null
        and private.has_org_role(organization_id, array['owner', 'admin', 'recruiter', 'viewer'])
    );
create policy jobs_recruiter_insert on public.jobs
    for insert to authenticated
    with check (
        created_by = (select auth.uid())
        and deleted_at is null
        and private.has_org_role(organization_id, array['owner', 'admin', 'recruiter'])
    );
create policy jobs_recruiter_update on public.jobs
    for update to authenticated
    using (
        deleted_at is null
        and private.has_org_role(organization_id, array['owner', 'admin', 'recruiter'])
    )
    with check (private.has_org_role(organization_id, array['owner', 'admin', 'recruiter']));

create policy candidates_member_read on public.candidates
    for select to authenticated
    using (
        deleted_at is null
        and private.has_org_role(organization_id, array['owner', 'admin', 'recruiter', 'viewer'])
    );
create policy candidates_recruiter_insert on public.candidates
    for insert to authenticated
    with check (
        created_by = (select auth.uid())
        and deleted_at is null
        and private.has_org_role(organization_id, array['owner', 'admin', 'recruiter'])
    );
create policy candidates_recruiter_update on public.candidates
    for update to authenticated
    using (
        deleted_at is null
        and private.has_org_role(organization_id, array['owner', 'admin', 'recruiter'])
    )
    with check (private.has_org_role(organization_id, array['owner', 'admin', 'recruiter']));

create policy resumes_member_read on public.resume_documents
    for select to authenticated
    using (
        deleted_at is null
        and (retention_due_at is null or retention_due_at > now())
        and private.has_org_role(organization_id, array['owner', 'admin', 'recruiter', 'viewer'])
    );
create policy resumes_recruiter_insert on public.resume_documents
    for insert to authenticated
    with check (
        created_by = (select auth.uid())
        and status = 'awaiting_upload'
        and deleted_at is null
        and private.has_org_role(organization_id, array['owner', 'admin', 'recruiter'])
    );

create policy screenings_member_read on public.screening_results
    for select to authenticated
    using (
        deleted_at is null
        and private.has_org_role(organization_id, array['owner', 'admin', 'recruiter', 'viewer'])
    );

create policy processing_jobs_member_read on public.processing_jobs
    for select to authenticated
    using (private.has_org_role(organization_id, array['owner', 'admin', 'recruiter', 'viewer']));

create policy audit_admin_read on public.audit_events
    for select to authenticated
    using (private.has_org_role(organization_id, array['owner', 'admin']));

-- Supabase private bucket: uploads/downloads only succeed when a matching
-- tenant-scoped resume row permits the operation. No public read policy exists.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('private-resumes', 'private-resumes', false, 10485760, array['application/pdf'])
on conflict (id) do update
set public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

alter table storage.objects enable row level security;

-- Restrictive guards intersect any pre-existing permissive Storage policy. The
-- guards apply only to this bucket and preserve independently configured buckets.
create policy resume_objects_anon_select_guard on storage.objects
    as restrictive for select to anon
    using (bucket_id <> 'private-resumes');
create policy resume_objects_authenticated_select_guard on storage.objects
    as restrictive for select to authenticated
    using (bucket_id <> 'private-resumes' or private.can_read_resume_path(name));
create policy resume_objects_anon_insert_guard on storage.objects
    as restrictive for insert to anon
    with check (bucket_id <> 'private-resumes');
create policy resume_objects_authenticated_insert_guard on storage.objects
    as restrictive for insert to authenticated
    with check (bucket_id <> 'private-resumes' or private.can_upload_resume_path(name));
create policy resume_objects_anon_update_guard on storage.objects
    as restrictive for update to anon
    using (bucket_id <> 'private-resumes')
    with check (bucket_id <> 'private-resumes');
create policy resume_objects_authenticated_update_guard on storage.objects
    as restrictive for update to authenticated
    using (bucket_id <> 'private-resumes' or private.can_upload_resume_path(name))
    with check (bucket_id <> 'private-resumes' or private.can_upload_resume_path(name));
create policy resume_objects_anon_delete_guard on storage.objects
    as restrictive for delete to anon
    using (bucket_id <> 'private-resumes');
create policy resume_objects_authenticated_delete_guard on storage.objects
    as restrictive for delete to authenticated
    using (bucket_id <> 'private-resumes' or private.can_delete_resume_path(name));

create policy resume_objects_member_read on storage.objects
    for select to authenticated
    using (
        bucket_id = 'private-resumes'
        and private.can_read_resume_path(name)
    );
create policy resume_objects_recruiter_insert on storage.objects
    for insert to authenticated
    with check (
        bucket_id = 'private-resumes'
        and private.can_upload_resume_path(name)
    );
create policy resume_objects_recruiter_retry_update on storage.objects
    for update to authenticated
    using (
        bucket_id = 'private-resumes'
        and private.can_upload_resume_path(name)
    )
    with check (
        bucket_id = 'private-resumes'
        and private.can_upload_resume_path(name)
    );
create policy resume_objects_admin_delete on storage.objects
    for delete to authenticated
    using (
        bucket_id = 'private-resumes'
        and private.can_delete_resume_path(name)
    );

-- Atomic database side of a screening intake. The user JWT and membership
-- check are required; the function has no service-role bypass path.
create or replace function public.create_screening_intake(
    p_organization_id uuid,
    p_job_id uuid,
    p_candidate_name text,
    p_candidate_email text,
    p_original_filename text,
    p_file_size_bytes bigint,
    p_content_sha256 text,
    p_idempotency_key uuid
)
returns table (
    screening_id uuid,
    candidate_id uuid,
    resume_document_id uuid,
    processing_job_id uuid,
    storage_path text,
    resume_status text,
    screening_status text,
    content_sha256 text,
    file_size_bytes bigint,
    is_replay boolean
)
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_user_id uuid := (select auth.uid());
    v_existing record;
    v_candidate_id uuid := gen_random_uuid();
    v_resume_id uuid := gen_random_uuid();
    v_screening_id uuid := gen_random_uuid();
    v_processing_id uuid := gen_random_uuid();
    v_storage_path text;
begin
    if v_user_id is null then
        raise exception using errcode = '42501', message = 'authentication required';
    end if;
    if not private.has_org_role(p_organization_id, array['owner', 'admin', 'recruiter']) then
        raise exception using errcode = '42501', message = 'organization access denied';
    end if;
    if p_idempotency_key is null
       or p_original_filename is null
       or p_file_size_bytes is null
       or p_file_size_bytes not between 1 and 10485760
       or p_content_sha256 is null
       or p_content_sha256 !~ '^[0-9a-f]{64}$'
       or length(btrim(p_original_filename)) not between 1 and 255
       or (p_candidate_name is not null and length(btrim(p_candidate_name)) not between 1 and 200)
       or (p_candidate_email is not null and (
           length(p_candidate_email) > 254
           or p_candidate_email !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'
       )) then
        raise exception using errcode = '22023', message = 'invalid intake metadata';
    end if;

    if not exists (
        select 1 from public.jobs j
        join public.organizations o on o.id = j.organization_id
        where j.id = p_job_id
          and j.organization_id = p_organization_id
          and j.status = 'active'
          and j.deleted_at is null
          and o.archived_at is null
    ) then
        raise exception using errcode = 'P0002', message = 'job not found';
    end if;

    -- Serialize identical tenant/user idempotency keys before looking up an
    -- existing request, so concurrent retries return one durable intake.
    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            p_organization_id::text || ':' || v_user_id::text || ':' || p_idempotency_key::text,
            0
        )
    );

    select p.id as processing_job_id, p.screening_id, r.candidate_id,
           r.id as resume_document_id, r.storage_path,
           r.status as resume_status, s.status as screening_status,
           r.content_sha256, r.file_size_bytes, s.job_id,
           c.full_name, c.contact_email, r.original_filename
      into v_existing
      from public.processing_jobs p
      join public.screening_results s on s.id = p.screening_id
      join public.resume_documents r on r.id = s.resume_document_id
      join public.candidates c on c.id = r.candidate_id
     where p.organization_id = p_organization_id
       and p.requested_by = v_user_id
       and p.idempotency_key = p_idempotency_key;

    if found then
        if v_existing.content_sha256 <> p_content_sha256
           or v_existing.file_size_bytes <> p_file_size_bytes
           or v_existing.job_id <> p_job_id
           or v_existing.full_name is distinct from nullif(btrim(p_candidate_name), '')
           or v_existing.contact_email is distinct from nullif(lower(btrim(p_candidate_email)), '')
           or v_existing.original_filename <> btrim(p_original_filename) then
            raise exception using errcode = '23505', message = 'idempotency key payload differs';
        end if;
        return query select v_existing.screening_id, v_existing.candidate_id,
            v_existing.resume_document_id, v_existing.processing_job_id,
            v_existing.storage_path, v_existing.resume_status,
            v_existing.screening_status, v_existing.content_sha256,
            v_existing.file_size_bytes, true;
        return;
    end if;

    v_storage_path := p_organization_id::text || '/' || v_candidate_id::text || '/' || v_resume_id::text || '.pdf';

    insert into public.candidates (id, organization_id, full_name, contact_email, created_by)
    values (v_candidate_id, p_organization_id, nullif(btrim(p_candidate_name), ''), nullif(lower(btrim(p_candidate_email)), ''), v_user_id);

    insert into public.resume_documents (
        id, organization_id, candidate_id, storage_path, original_filename,
        file_size_bytes, content_sha256, status, created_by
    ) values (
        v_resume_id, p_organization_id, v_candidate_id, v_storage_path,
        btrim(p_original_filename), p_file_size_bytes, p_content_sha256,
        'awaiting_upload', v_user_id
    );

    insert into public.screening_results (
        id, organization_id, job_id, candidate_id, resume_document_id,
        status, created_by
    ) values (
        v_screening_id, p_organization_id, p_job_id, v_candidate_id, v_resume_id,
        'queued', v_user_id
    );

    insert into public.processing_jobs (
        id, organization_id, screening_id, requested_by, idempotency_key,
        status, attempt_count
    ) values (
        v_processing_id, p_organization_id, v_screening_id, v_user_id,
        p_idempotency_key, 'queued', 0
    );

    return query select v_screening_id, v_candidate_id, v_resume_id,
        v_processing_id, v_storage_path, 'awaiting_upload'::text,
        'queued'::text, p_content_sha256, p_file_size_bytes, false;
end;
$$;

revoke all on function public.create_screening_intake(uuid, uuid, text, text, text, bigint, text, uuid) from public, anon;
grant execute on function public.create_screening_intake(uuid, uuid, text, text, text, bigint, text, uuid) to authenticated;

create or replace function public.complete_resume_upload(p_resume_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_doc public.resume_documents%rowtype;
begin
    select * into v_doc from public.resume_documents d
     where d.id = p_resume_id and d.deleted_at is null;
    if not found
       or (select auth.uid()) is null
       or not private.has_org_role(v_doc.organization_id, array['owner', 'admin', 'recruiter'])
       or not (
           v_doc.created_by = (select auth.uid())
           or private.has_org_role(v_doc.organization_id, array['owner', 'admin'])
       ) then
        raise exception using errcode = '42501', message = 'resume access denied';
    end if;
    if v_doc.status = 'available' then
        return;
    end if;
    if v_doc.status <> 'awaiting_upload'
       or not exists (
           select 1 from storage.objects o
            where o.bucket_id = 'private-resumes'
              and o.name = v_doc.storage_path
       ) then
        raise exception using errcode = '23503', message = 'resume object is not present';
    end if;
    update public.resume_documents set status = 'available' where id = p_resume_id;
end;
$$;

revoke all on function public.complete_resume_upload(uuid) from public, anon;
grant execute on function public.complete_resume_upload(uuid) to authenticated;

create or replace function public.fail_resume_upload(p_resume_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_doc public.resume_documents%rowtype;
begin
    select * into v_doc from public.resume_documents d
     where d.id = p_resume_id and d.deleted_at is null;
    if not found
       or v_doc.status <> 'awaiting_upload'
       or (select auth.uid()) is null
       or not (
           v_doc.created_by = (select auth.uid())
           or private.has_org_role(v_doc.organization_id, array['owner', 'admin'])
       ) then
        raise exception using errcode = '42501', message = 'resume access denied';
    end if;
    update public.resume_documents
       set status = 'deleted', deleted_at = now()
     where id = p_resume_id;
    update public.screening_results
       set status = 'failed', deleted_at = now()
     where resume_document_id = p_resume_id and status = 'queued';
    update public.processing_jobs
       set status = 'failed', completed_at = now(), last_error_code = 'storage_upload_failed'
     where screening_id in (select s.id from public.screening_results s where s.resume_document_id = p_resume_id)
       and status = 'queued';
    if not exists (
        select 1 from public.resume_documents d
         where d.candidate_id = v_doc.candidate_id and d.deleted_at is null
    ) then
        update public.candidates
           set full_name = null, contact_email = null, deleted_at = now()
         where id = v_doc.candidate_id and deleted_at is null;
    end if;
end;
$$;

revoke all on function public.fail_resume_upload(uuid) from public, anon;
grant execute on function public.fail_resume_upload(uuid) to authenticated;

create or replace function public.delete_resume_document(p_resume_id uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
    v_doc public.resume_documents%rowtype;
begin
    select * into v_doc from public.resume_documents d
     where d.id = p_resume_id and d.deleted_at is null;
    if not found
       or not private.has_org_role(v_doc.organization_id, array['owner', 'admin']) then
        raise exception using errcode = '42501', message = 'resume access denied';
    end if;
    if exists (
        select 1 from storage.objects o
         where o.bucket_id = 'private-resumes'
           and o.name = v_doc.storage_path
    ) then
        raise exception using errcode = '55000', message = 'resume object must be removed first';
    end if;

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
     where screening_id in (select s.id from public.screening_results s where s.resume_document_id = p_resume_id)
       and status in ('queued', 'retryable_failure', 'processing');

    if not exists (
        select 1 from public.resume_documents d
         where d.candidate_id = v_doc.candidate_id and d.deleted_at is null
    ) then
        update public.candidates
           set full_name = null, contact_email = null, deleted_at = now()
         where id = v_doc.candidate_id and deleted_at is null;
    end if;
end;
$$;

revoke all on function public.delete_resume_document(uuid) from public, anon;
grant execute on function public.delete_resume_document(uuid) to authenticated;

-- Job criteria are added in a forward migration so already initialized
-- Supabase projects can apply this change without rewriting migration history.
alter table public.jobs
    add column required_skills text[] not null default '{}',
    add column preferred_skills text[] not null default '{}',
    add column minimum_experience_years numeric(4, 1),
    add column qualifications text not null default '',
    add column responsibilities text not null default '';

alter table public.jobs
    add constraint jobs_experience_years_check
        check (minimum_experience_years between 0 and 60),
    add constraint jobs_qualifications_length_check
        check (length(qualifications) <= 10000),
    add constraint jobs_responsibilities_length_check
        check (length(responsibilities) <= 10000),
    add constraint jobs_required_skills_check
        check (cardinality(required_skills) <= 50 and array_position(required_skills, '') is null),
    add constraint jobs_preferred_skills_check
        check (cardinality(preferred_skills) <= 50 and array_position(preferred_skills, '') is null);

alter table public.candidates
    drop constraint if exists candidates_contact_email_check,
    add constraint candidates_contact_email_format_check
        check (
            contact_email is null
            or (length(contact_email) <= 254 and contact_email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$')
        );

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
