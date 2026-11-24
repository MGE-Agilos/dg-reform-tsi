-- TSI x CSR Explorer: full access of service_role when it cannot have the BYPASSRLS attribute.
-- Run as the database owner AFTER supabase/migrations/001 to 010 (deploy/onprem/README.md, step 4), and again after
-- every upgrade that adds tables. Safe to run again.
--
-- The application service (administration) and the data updates use the role service_role, which must read and
-- write every row, whatever the row-level security rules. On the cloud service it has the attribute BYPASSRLS. Only a
-- superuser can give that attribute; when it is missing, this script gives the same access with one rule per table:
-- "service_role may read and write all rows". The rules only concern service_role (anon and authenticated keep
-- their own rules). They are harmless once service_role has BYPASSRLS.

do $$
declare
  t record;
begin
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    raise exception 'Create the role service_role first (deploy/onprem/README.md, step 2)';
  end if;
  for t in
    select c.relname
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind in ('r', 'p') and c.relrowsecurity
    order by c.relname
  loop
    if not exists (select 1 from pg_policies
                   where schemaname = 'public' and tablename = t.relname and policyname = 'service_role_all') then
      execute format('create policy service_role_all on %I.%I for all to service_role using (true) with check (true)',
                     'public', t.relname);
      raise notice 'service_role_all created on %', t.relname;
    end if;
  end loop;
end $$;
