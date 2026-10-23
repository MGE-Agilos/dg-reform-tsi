-- Administration de l'explorateur (roles User / Admin) et suivi des visites (lot 5).
-- A coller dans Supabase > SQL Editor APRES 001 a 006. Idempotent (rejouable).
-- ATTENTION : le projet Supabase est PARTAGE avec d'autres applications (Europanel) : ce script ne touche qu'a la table
-- testers de l'explorateur (colonnes role, invited_at, invited_by et une regle de lecture pour les administrateurs), a la
-- table page_views (et sa sequence d'identite) et aux fonctions is_admin, my_role et admin_usage ; rien d'autre (ni
-- reglages Auth, ni utilisateurs).

-- ---------- roles ----------
-- invited_at sans valeur par defaut a l'ajout : les testeurs existants gardent null (date d'invitation inconnue) ; les
-- nouvelles lignes recoivent now().
alter table public.testers add column if not exists role text not null default 'user';
alter table public.testers add column if not exists invited_at timestamptz;
alter table public.testers alter column invited_at set default now();
alter table public.testers add column if not exists invited_by text;
do $$
begin
  if not exists (select 1 from pg_constraint
                 where conname = 'testers_role_check' and conrelid = 'public.testers'::regclass) then
    alter table public.testers add constraint testers_role_check check (role in ('user', 'admin'));
  end if;
end $$;

-- Premier (et pour l'instant seul) administrateur.
insert into public.testers (email, role) values ('maxime.gerard@agilos.com', 'admin')
  on conflict (email) do update set role = 'admin';

-- Administrateur = utilisateur courant dont l'e-mail CONFIRME (auth.users) est dans testers avec le role admin.
create or replace function public.is_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from auth.users u join public.testers t on t.email = lower(u.email)
    where u.id = auth.uid() and u.email_confirmed_at is not null and t.role = 'admin');
$$;
revoke all on function public.is_admin() from public, anon;
grant execute on function public.is_admin() to authenticated, service_role;

-- Role de l'appelant uniquement ('admin' | 'user'), null s'il n'est pas testeur (ou e-mail non confirme).
create or replace function public.my_role() returns text
language sql stable security definer set search_path = public as $$
  select t.role from auth.users u join public.testers t on t.email = lower(u.email)
  where u.id = auth.uid() and u.email_confirmed_at is not null
  limit 1;
$$;
revoke all on function public.my_role() from public, anon;
grant execute on function public.my_role() to authenticated;

-- Les administrateurs lisent la liste des testeurs ; les autres testeurs n'y ont toujours aucun acces (aucune regle ne
-- les y autorise). Aucune ecriture hors service_role (la fonction tsi-admin ecrit avec la cle service_role).
grant select on public.testers to authenticated;
revoke insert, update, delete, truncate on public.testers from anon, authenticated;
drop policy if exists admin_read on public.testers;
create policy admin_read on public.testers for select to authenticated using ((select public.is_admin()));

-- ---------- suivi des visites ----------
-- Une ligne par vue terminee (page ou section de l'application, temps visible seulement), ecrite DEPUIS LA PAGE par le
-- testeur lui-meme ; user_id et email viennent du jeton (valeurs par defaut, verifiees par la regle d'insertion).
-- view : section (dashboard, projects, project, csrs, csr, csrdoc, lineage, anomalies, impact, guide, reviews, admin,
-- chat, ...) ; detail : parametres de la route seulement (identifiant, sous-vue, pays), jamais un texte saisi.
create table if not exists public.page_views (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid(),
  email text not null default (auth.jwt() ->> 'email'),
  session_id text not null check (char_length(session_id) between 8 and 64),
  view text not null check (view ~ '^[a-z][a-z0-9_-]{0,39}$'),
  detail text check (char_length(detail) <= 200),
  started_at timestamptz not null,
  duration_s int not null check (duration_s between 0 and 86400),
  created_at timestamptz not null default now()
);
create index if not exists page_views_started_idx on public.page_views (started_at desc);
create index if not exists page_views_email_started_idx on public.page_views (email, started_at desc);

revoke all on public.page_views from anon, authenticated;
grant select, insert on public.page_views to authenticated;
grant all on public.page_views to service_role;
do $$
declare seq text := pg_get_serial_sequence('public.page_views', 'id');
begin
  if seq is not null then
    execute format('revoke usage, select, update on sequence %s from anon, authenticated', seq);
  end if;
end $$;

alter table public.page_views enable row level security;
drop policy if exists tester_insert on public.page_views;
create policy tester_insert on public.page_views for insert to authenticated
  with check (user_id = (select auth.uid())
              and email = (select auth.jwt() ->> 'email')
              and started_at > now() - interval '2 days'
              and started_at < now() + interval '5 minutes'
              and (select public.is_tester()));
drop policy if exists admin_read on public.page_views;
create policy admin_read on public.page_views for select to authenticated using ((select public.is_admin()));
-- Pas de regle update / delete : le journal ne se modifie pas depuis la page (purge eventuelle par service_role).

-- Agregats du suivi pour la page Admin, sur [p_from, p_to[ (au plus 400 jours), jours dans le fuseau p_tz (nom IANA,
-- UTC si inconnu). security invoker : le RLS s'applique ; un non-administrateur recoit l'erreur 42501.
-- Forme : {totals: {users, sessions, views, total_s}, per_user: [{email, last_seen, sessions, views, total_s, top_view}],
-- per_view: [{view, views, total_s, users}], per_day: [{day, users, sessions, views, total_s}]}.
create or replace function public.admin_usage(p_from timestamptz, p_to timestamptz, p_tz text default 'UTC')
returns jsonb
language plpgsql stable security invoker set search_path = public as $$
declare
  tz text := coalesce((select name from pg_timezone_names where name = p_tz limit 1), 'UTC');
  result jsonb;
begin
  if not public.is_admin() then
    raise exception 'administrators only' using errcode = '42501';
  end if;
  if p_from is null or p_to is null or p_to <= p_from or p_to - p_from > interval '400 days' then
    raise exception 'invalid period' using errcode = '22023';
  end if;
  with v as (
    select * from public.page_views where started_at >= p_from and started_at < p_to
  ),
  top as (
    select distinct on (email) email, view
    from (select email, view, sum(duration_s) as s from v group by 1, 2) x
    order by email, s desc, view
  )
  select jsonb_build_object(
    'totals', (select jsonb_build_object('users', count(distinct email), 'sessions', count(distinct session_id),
                                         'views', count(*), 'total_s', coalesce(sum(duration_s), 0)) from v),
    'per_user', (
      select coalesce(jsonb_agg(jsonb_build_object('email', s.email, 'last_seen', s.last_seen, 'sessions', s.sessions,
                                                   'views', s.views, 'total_s', s.total_s, 'top_view', t.view)
                                order by s.last_seen desc, s.email), '[]'::jsonb)
      from (select email, max(started_at + make_interval(secs => duration_s)) as last_seen,
                   count(distinct session_id) as sessions, count(*) as views, sum(duration_s) as total_s
            from v group by 1) s
      left join top t on t.email = s.email
    ),
    'per_view', (
      select coalesce(jsonb_agg(jsonb_build_object('view', s.view, 'views', s.views, 'total_s', s.total_s,
                                                   'users', s.users) order by s.total_s desc, s.view), '[]'::jsonb)
      from (select view, count(*) as views, sum(duration_s) as total_s, count(distinct email) as users
            from v group by 1) s
    ),
    'per_day', (
      select coalesce(jsonb_agg(jsonb_build_object('day', s.day, 'users', s.users, 'sessions', s.sessions,
                                                   'views', s.views, 'total_s', s.total_s) order by s.day), '[]'::jsonb)
      from (select (started_at at time zone tz)::date as day, count(distinct email) as users,
                   count(distinct session_id) as sessions, count(*) as views, sum(duration_s) as total_s
            from v group by 1) s
    )
  ) into result;
  return result;
end;
$$;
revoke all on function public.admin_usage(timestamptz, timestamptz, text) from public, anon;
grant execute on function public.admin_usage(timestamptz, timestamptz, text) to authenticated, service_role;

notify pgrst, 'reload schema';
