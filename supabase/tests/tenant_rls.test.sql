begin;
select plan(9);

-- Synthetic identities are transaction-local test fixtures, never login users.
insert into auth.users (id, aud, role, email, encrypted_password)
values
    ('10000000-0000-4000-8000-000000000001', 'authenticated', 'authenticated', 'rls-a@example.test', ''),
    ('20000000-0000-4000-8000-000000000002', 'authenticated', 'authenticated', 'rls-b@example.test', '');

insert into public.organizations (id, name, created_by)
values
    ('aaaaaaaa-0000-4000-8000-000000000001', 'Tenant A', '10000000-0000-4000-8000-000000000001'),
    ('bbbbbbbb-0000-4000-8000-000000000002', 'Tenant B', '20000000-0000-4000-8000-000000000002');

insert into public.organization_memberships (organization_id, user_id, role)
values
    ('aaaaaaaa-0000-4000-8000-000000000001', '10000000-0000-4000-8000-000000000001', 'owner'),
    ('bbbbbbbb-0000-4000-8000-000000000002', '20000000-0000-4000-8000-000000000002', 'owner');

insert into public.jobs (id, organization_id, title, description, created_by)
values
    ('a0000000-0000-4000-8000-000000000001', 'aaaaaaaa-0000-4000-8000-000000000001', 'Tenant A role', 'A description', '10000000-0000-4000-8000-000000000001'),
    ('b0000000-0000-4000-8000-000000000002', 'bbbbbbbb-0000-4000-8000-000000000002', 'Tenant B role', 'B description', '20000000-0000-4000-8000-000000000002');

select ok(
    (select relrowsecurity from pg_class where oid = 'public.jobs'::regclass),
    'RLS is enabled on jobs'
);
select ok(
    (select not public from storage.buckets where id = 'private-resumes'),
    'resume bucket is private'
);

set local role authenticated;
select set_config('request.jwt.claim.sub', '10000000-0000-4000-8000-000000000001', true);
select set_config('request.jwt.claim.role', 'authenticated', true);
select set_config('request.jwt.claims', '{"sub":"10000000-0000-4000-8000-000000000001","role":"authenticated"}', true);

select is((select count(*)::integer from public.jobs), 1, 'user sees only jobs in their organization');
select is((select count(*)::integer from public.organization_memberships), 1, 'user sees only their own membership');
select is((select count(*)::integer from public.organizations where id = 'bbbbbbbb-0000-4000-8000-000000000002'), 0, 'user cannot read another organization');
select is(
    (with changed as (
        update public.jobs set title = 'stolen'
         where id = 'b0000000-0000-4000-8000-000000000002'
         returning id
    ) select count(*)::integer from changed),
    0,
    'cross-organization update affects no rows'
);
select throws_ok(
    $$insert into public.jobs (organization_id, title, description, created_by)
      values ('bbbbbbbb-0000-4000-8000-000000000002', 'Unauthorized', 'No access', '10000000-0000-4000-8000-000000000001')$$,
    '42501',
    null,
    'cross-organization insert is rejected'
);
select throws_ok(
    $$update public.jobs set organization_id = 'bbbbbbbb-0000-4000-8000-000000000002'
      where id = 'a0000000-0000-4000-8000-000000000001'$$,
    '42501',
    null,
    'tenant ownership cannot be moved'
);
select is((select count(*)::integer from public.audit_events where organization_id = 'bbbbbbbb-0000-4000-8000-000000000002'), 0, 'audit events are tenant isolated');

select * from finish();
rollback;
