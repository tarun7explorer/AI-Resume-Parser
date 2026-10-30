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
