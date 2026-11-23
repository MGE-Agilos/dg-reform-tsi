-- TSI x CSR Explorer: compatibility objects for a plain PostgreSQL (self-hosted installation).
-- Run once as the database owner, BEFORE supabase/migrations/001 to 010 (deploy/onprem/README.md, step 4).
--
-- The schema was written for Supabase, which provides an `auth` schema. This script provides the same few objects:
--   auth.uid()   id of the signed-in user (uuid), from the token claims PostgREST sets for each request
--   auth.jwt()   all the token claims (jsonb)
--   auth.role()  role claim of the token
--   auth.users   the signed-in user only (id, email, email_confirmed_at), built from the token
-- The application service signs a user in with the identity provider, then issues the token. Its claims must be:
-- sub = a stable uuid for the user, email = the user's e-mail address (lower case), role = 'authenticated'.
-- A token is only issued after a successful sign-in: the e-mail address counts as confirmed.
--
-- The roles anon, authenticated and service_role must exist (README.md, step 2). Safe to run again.

do $$
begin
  if (select count(*) from pg_roles where rolname in ('anon', 'authenticated', 'service_role')) < 3 then
    raise exception 'Create the roles anon, authenticated and service_role first (deploy/onprem/README.md, step 2)';
  end if;
end $$;

create schema if not exists auth;

create or replace function auth.jwt() returns jsonb
language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim', true), ''),
    nullif(current_setting('request.jwt.claims', true), '')
  )::jsonb;
$$;

create or replace function auth.uid() returns uuid
language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
  )::uuid;
$$;

create or replace function auth.role() returns text
language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
  )::text;
$$;

-- The signed-in user only (no row without a token): enough for is_tester(), is_admin() and my_role().
create or replace view auth.users as
  select auth.uid() as id, lower(auth.jwt() ->> 'email') as email, now() as email_confirmed_at
  where auth.uid() is not null;

grant usage on schema auth to anon, authenticated, service_role;
grant execute on function auth.jwt(), auth.uid(), auth.role() to anon, authenticated, service_role;
grant select on auth.users to authenticated, service_role;
