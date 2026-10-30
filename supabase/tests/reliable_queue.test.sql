begin;
select plan(12);

-- Synthetic test-only recruiter and organization. The whole fixture rolls back.
insert into auth.users (id, aud, role, email, encrypted_password)
values ('30000000-0000-4000-8000-000000000003', 'authenticated', 'authenticated', 'queue@example.test', '');
insert into public.organizations (id, name, created_by)
values ('cccccccc-0000-4000-8000-000000000003', 'Queue test', '30000000-0000-4000-8000-000000000003');
insert into public.organization_memberships (organization_id, user_id, role)
values ('cccccccc-0000-4000-8000-000000000003', '30000000-0000-4000-8000-000000000003', 'owner');
insert into public.jobs (id, organization_id, title, description, created_by, required_skills)
values ('c0000000-0000-4000-8000-000000000003', 'cccccccc-0000-4000-8000-000000000003', 'Queue role', 'Fixture role', '30000000-0000-4000-8000-000000000003', array['SQL']);

insert into public.candidates (id, organization_id, full_name, created_by)
values
 ('c1000000-0000-4000-8000-000000000003', 'cccccccc-0000-4000-8000-000000000003', 'Test Candidate One', '30000000-0000-4000-8000-000000000003'),
 ('c2000000-0000-4000-8000-000000000003', 'cccccccc-0000-4000-8000-000000000003', 'Test Candidate Two', '30000000-0000-4000-8000-000000000003');
insert into public.resume_documents (id, organization_id, candidate_id, storage_path, original_filename, file_size_bytes, content_sha256, status, created_by)
values
 ('c3000000-0000-4000-8000-000000000003', 'cccccccc-0000-4000-8000-000000000003', 'c1000000-0000-4000-8000-000000000003', 'cccccccc-0000-4000-8000-000000000003/c1000000-0000-4000-8000-000000000003/c3000000-0000-4000-8000-000000000003.pdf', 'one.pdf', 100, repeat('a', 64), 'available', '30000000-0000-4000-8000-000000000003'),
 ('c4000000-0000-4000-8000-000000000003', 'cccccccc-0000-4000-8000-000000000003', 'c2000000-0000-4000-8000-000000000003', 'cccccccc-0000-4000-8000-000000000003/c2000000-0000-4000-8000-000000000003/c4000000-0000-4000-8000-000000000003.pdf', 'two.pdf', 100, repeat('b', 64), 'available', '30000000-0000-4000-8000-000000000003');
insert into public.screening_results (id, organization_id, job_id, candidate_id, resume_document_id, status, created_by)
values
 ('c5000000-0000-4000-8000-000000000003', 'cccccccc-0000-4000-8000-000000000003', 'c0000000-0000-4000-8000-000000000003', 'c1000000-0000-4000-8000-000000000003', 'c3000000-0000-4000-8000-000000000003', 'queued', '30000000-0000-4000-8000-000000000003'),
 ('c6000000-0000-4000-8000-000000000003', 'cccccccc-0000-4000-8000-000000000003', 'c0000000-0000-4000-8000-000000000003', 'c2000000-0000-4000-8000-000000000003', 'c4000000-0000-4000-8000-000000000003', 'queued', '30000000-0000-4000-8000-000000000003');
insert into public.processing_jobs (id, organization_id, screening_id, requested_by, idempotency_key)
values
 ('c7000000-0000-4000-8000-000000000003', 'cccccccc-0000-4000-8000-000000000003', 'c5000000-0000-4000-8000-000000000003', '30000000-0000-4000-8000-000000000003', 'c8000000-0000-4000-8000-000000000003'),
 ('c9000000-0000-4000-8000-000000000003', 'cccccccc-0000-4000-8000-000000000003', 'c6000000-0000-4000-8000-000000000003', '30000000-0000-4000-8000-000000000003', 'ca000000-0000-4000-8000-000000000003');

set local role service_role;
select set_config('request.jwt.claim.role', 'service_role', true);
select set_config('request.jwt.claims', '{"role":"service_role"}', true);
select is(
    (select screening_id from public.claim_screening_job('queue-worker-one', 60)),
    'c5000000-0000-4000-8000-000000000003'::uuid,
    'first worker receives one atomic queue claim'
);
select is(
    (select count(*)::integer from public.claim_screening_job('queue-worker-two', 60)),
    1,
    'a second worker claims a different unlocked job'
);

reset role;
update public.processing_jobs set lease_expires_at = now() - interval '1 second'
 where id = 'c7000000-0000-4000-8000-000000000003';
set local role service_role;
select is(
    (select attempt_count from public.claim_screening_job('restarted-worker', 60)
      where screening_id = 'c5000000-0000-4000-8000-000000000003'),
    2,
    'expired processing lease is reclaimed after a worker restart'
);
reset role;
update public.processing_jobs set attempt_count = max_attempts, lease_expires_at = now() - interval '1 second'
 where id = 'c7000000-0000-4000-8000-000000000003';
set local role service_role;
select count(*) from public.claim_screening_job('exhaustion-worker', 60);
select is((select status from public.processing_jobs where id = 'c7000000-0000-4000-8000-000000000003'), 'failed', 'retry exhaustion moves the processing job to failed');
select is((select status from public.screening_results where id = 'c5000000-0000-4000-8000-000000000003'), 'failed', 'retry exhaustion is visible on screening history');

reset role;
set local role authenticated;
select set_config('request.jwt.claim.sub', '30000000-0000-4000-8000-000000000003', true);
select set_config('request.jwt.claim.role', 'authenticated', true);
select set_config('request.jwt.claims', '{"sub":"30000000-0000-4000-8000-000000000003","role":"authenticated"}', true);
select is(
    (select is_replay from public.create_screening_intake(
        'cccccccc-0000-4000-8000-000000000003', 'c0000000-0000-4000-8000-000000000003',
        'Idempotent Test', 'idempotent@example.test', 'fixture.pdf', 100, repeat('d', 64),
        'cb000000-0000-4000-8000-000000000003'
    )),
    false,
    'first intake creates one durable per-file job'
);
select is(
    (select is_replay from public.create_screening_intake(
        'cccccccc-0000-4000-8000-000000000003', 'c0000000-0000-4000-8000-000000000003',
        'Idempotent Test', 'idempotent@example.test', 'fixture.pdf', 100, repeat('d', 64),
        'cb000000-0000-4000-8000-000000000003'
    )),
    true,
    'a repeated key and identical payload return the original intake'
);
select throws_ok(
    $$select * from public.create_screening_intake(
        'cccccccc-0000-4000-8000-000000000003', 'c0000000-0000-4000-8000-000000000003',
        'Idempotent Test', 'idempotent@example.test', 'fixture.pdf', 100, repeat('e', 64),
        'cb000000-0000-4000-8000-000000000003'
    )$$,
    '23505', null,
    'reusing an idempotency key for different bytes is rejected'
);
select is(
    public.schedule_resume_deletion('cccccccc-0000-4000-8000-000000000003', 'c4000000-0000-4000-8000-000000000003'),
    'queued',
    'deletion first hides data and queues private storage cleanup'
);
select is((select status from public.resume_documents where id = 'c4000000-0000-4000-8000-000000000003'), 'deleted', 'deleted resume becomes inaccessible immediately');
select ok((select deleted_at is not null and report_json is null from public.screening_results where id = 'c6000000-0000-4000-8000-000000000003'), 'associated report is scrubbed and soft-deleted');
select is((select status from public.storage_cleanup_jobs where resume_document_id = 'c4000000-0000-4000-8000-000000000003'), 'queued', 'private object deletion remains recoverable through a durable cleanup job');

select * from finish();
rollback;
