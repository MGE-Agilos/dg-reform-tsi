-- Explorateur TSI x CSR (lot 1). A coller dans Supabase > SQL Editor. Idempotent (rejouable).
-- Lecture reservee aux testeurs (table testers) ; aucune ecriture hors service_role.

create table if not exists public.projects (
  project_id text primary key,
  title text not null,
  programme text,
  year int,
  countries text[] not null default '{}',
  policy_area text,
  results text,
  detail_page boolean not null default false,
  url text,
  summary text,
  deliverables int not null default 0,
  csrs text[] not null default '{}'
);

create table if not exists public.csrs (
  csr_id text primary key,
  country text not null,
  year int not null,
  number text not null,
  title text not null,
  text text not null,
  policy_areas text[] not null default '{}',
  assessment text,
  assessment_year int,
  source_url text
);

create table if not exists public.csr_parts (
  part_id text primary key,
  csr_id text not null references public.csrs (csr_id) on delete cascade,
  country text not null,
  year int not null,
  text text not null,
  policy_areas text[] not null default '{}',
  assessment text
);

create table if not exists public.project_csr_links (
  project_id text not null references public.projects (project_id) on delete cascade,
  part_id text not null references public.csr_parts (part_id) on delete cascade,
  level text not null check (level in ('direct', 'partial')),
  rationale text not null,
  basis text not null,
  primary key (project_id, part_id)
);

create table if not exists public.anomalies (
  id bigint generated always as identity primary key,
  project_id text,
  title text,
  programme text,
  year int,
  countries text,
  type text not null,
  severity text not null,
  detail text,
  url text
);

create table if not exists public.testers (
  email text primary key check (email = lower(email))
);

create index if not exists projects_year_idx on public.projects (year);
create index if not exists projects_countries_idx on public.projects using gin (countries);
create index if not exists csr_parts_csr_idx on public.csr_parts (csr_id);
create index if not exists links_part_idx on public.project_csr_links (part_id);
create index if not exists csrs_country_year_idx on public.csrs (country, year);
create index if not exists anomalies_type_idx on public.anomalies (type, severity);

-- Droits explicites (independants des privileges par defaut du projet) : lecture pour authenticated (filtree par
-- RLS), aucune ecriture pour anon/authenticated ; service_role garde tous les droits.
grant select on public.projects, public.csrs, public.csr_parts, public.project_csr_links, public.anomalies
  to authenticated;
revoke insert, update, delete, truncate on public.projects, public.csrs, public.csr_parts,
  public.project_csr_links, public.anomalies, public.testers from anon, authenticated;
revoke select on public.testers from anon, authenticated;
revoke select on public.projects, public.csrs, public.csr_parts, public.project_csr_links, public.anomalies from anon;

-- Testeur = utilisateur courant dont l'e-mail CONFIRME (auth.users, pas seulement le jeton) est dans testers.
create or replace function public.is_tester() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from auth.users u join public.testers t on t.email = lower(u.email)
    where u.id = auth.uid() and u.email_confirmed_at is not null);
$$;
revoke all on function public.is_tester() from public;
revoke execute on function public.is_tester() from anon;
grant execute on function public.is_tester() to authenticated;

do $$
declare t text;
begin
  foreach t in array array['projects', 'csrs', 'csr_parts', 'project_csr_links', 'anomalies', 'testers'] loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
  foreach t in array array['projects', 'csrs', 'csr_parts', 'project_csr_links', 'anomalies'] loop
    execute format('drop policy if exists tester_read on public.%I', t);
    execute format('create policy tester_read on public.%I for select to authenticated using ((select public.is_tester()))', t);
  end loop;
end $$;
-- testers : RLS sans regle = illisible et non modifiable par anon/authenticated (service_role seulement).

-- Configuration Auth (inscription sur invitation seulement) : garder "Confirm email" actif, desactiver
-- "Allow new users to sign up", inviter chaque testeur (Authentication > Users > Invite user) ; la page demande le
-- lien avec shouldCreateUser: false. Puis ajouter les testeurs (e-mails en minuscules), par exemple :
-- insert into public.testers (email) values ('prenom.nom@example.org') on conflict do nothing;
