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
