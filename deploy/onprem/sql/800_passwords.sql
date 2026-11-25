-- TSI x CSR Explorer: passwords of the users, for a self-hosted installation that signs in with e-mail + password
-- (AUTH_MODE=password, deploy/onprem/README.md, step 6). Run as the database owner AFTER supabase/migrations/001 to
-- 010 and BEFORE 900_service_role.sql. Safe to run again. Not needed with AUTH_MODE=oidc (it does no harm).
--
-- One row per user who has a password: only a hash (scrypt, computed by the application service), never the password.
-- Revoking a user (deleting the row of testers) deletes their password. Neither the page nor the users can read this
-- table: only service_role (the application service) can.

create table if not exists public.user_passwords (
  email text primary key references public.testers (email) on delete cascade,
  hash text not null,
  must_change boolean not null default true,      -- temporary password given by an administrator
  failed_count integer not null default 0,        -- failed sign-ins in a row
  locked_until timestamptz,                       -- sign-in refused until then after too many failures
  updated_at timestamptz not null default now()
);

alter table public.user_passwords enable row level security;
revoke all on public.user_passwords from anon, authenticated;
grant select, insert, update, delete on public.user_passwords to service_role;
