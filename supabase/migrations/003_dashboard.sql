-- Tableau de bord de l'explorateur et conservation des analyses d'impact (lot 3). A coller dans Supabase > SQL Editor.
-- Idempotent (rejouable). Necessite 001_explorer.sql (tables, is_tester).
-- ATTENTION : le projet Supabase est PARTAGE avec d'autres applications. Tout nouvel objet porte un nom propre a
-- l'explorateur (impact_reports) ou le prefixe tsi_ (tsi_dashboard) ; ne rien supprimer ni renommer d'autre.

-- Une ligne par analyse d'impact : source 'web' (fonction tsi-assistant) ou 'local' (supabase-sync, output/impact).
-- Pas de cle etrangere vers projects : supabase-sync vide et recharge projects, une cascade effacerait les rapports.
create table if not exists public.impact_reports (
  id bigint generated always as identity primary key,
  project_id text not null,
  title text,
  score int check (score between 0 and 10),
  level text check (level in ('strong', 'moderate', 'weak')),
  confidence text check (confidence in ('high', 'medium', 'low')),
  verdict text,
  report text not null,
  model text,
  source text not null check (source in ('web', 'local')),
  created_by uuid,
  created_at timestamptz not null default now()
);

create index if not exists impact_reports_project_created_idx on public.impact_reports (project_id, created_at desc);

-- Lecture par les testeurs (RLS) ; ecritures uniquement avec la cle service_role (fonction, synchronisation).
grant select on public.impact_reports to authenticated;
revoke insert, update, delete, truncate on public.impact_reports from anon, authenticated;
revoke select on public.impact_reports from anon;
grant all on public.impact_reports to service_role;

alter table public.impact_reports enable row level security;
drop policy if exists tester_read on public.impact_reports;
create policy tester_read on public.impact_reports for select to authenticated using ((select public.is_tester()));

-- Agregats du tableau de bord en un seul appel (security invoker : le RLS des testeurs s'applique).
-- Statistiques d'impact calculees sur la DERNIERE analyse de chaque projet (latest) ; impact_recent liste les
-- 10 analyses les plus recentes, toutes confondues. Les listes vides valent [] (jamais null).
create or replace function public.tsi_dashboard() returns jsonb
language sql stable security invoker set search_path = public as $$
  with latest as (
    select distinct on (r.project_id) r.project_id, r.score, r.level, r.confidence, r.created_at
    from public.impact_reports r
    order by r.project_id, r.created_at desc, r.id desc
  ),
  covered as (
    select distinct cp.csr_id
    from public.csr_parts cp
    join public.project_csr_links l on l.part_id = cp.part_id
  )
  select jsonb_build_object(
    'totals', jsonb_build_object(
      'projects', (select count(*) from public.projects),
      'projects_with_detail', (select count(*) from public.projects where detail_page),
      'deliverables', (select coalesce(sum(deliverables), 0) from public.projects),
      'csrs', (select count(*) from public.csrs),
      'csr_parts', (select count(*) from public.csr_parts),
      'links', (select count(*) from public.project_csr_links),
      'projects_with_links', (select count(distinct project_id) from public.project_csr_links),
      'impact_reports', (select count(*) from public.impact_reports),
      'impact_projects', (select count(*) from latest),
      'impact_mean', (select round(avg(score)::numeric, 1) from latest where score is not null),
      'anomalies', (select count(*) from public.anomalies),
      'anomalies_high', (select count(*) from public.anomalies where severity = 'high')
    ),
    'projects_by_programme', (
      select coalesce(jsonb_agg(jsonb_build_object('key', s.k, 'n', s.n) order by s.n desc, s.k), '[]'::jsonb)
      from (select coalesce(programme, 'none') as k, count(*) as n from public.projects group by 1) s
    ),
    'projects_by_year_programme', (
      select coalesce(jsonb_agg(jsonb_build_object('year', s.y, 'programme', s.prog, 'n', s.n)
                                order by s.y, s.prog), '[]'::jsonb)
      from (select year as y, coalesce(programme, 'none') as prog, count(*) as n
            from public.projects where year is not null group by 1, 2) s
    ),
    'projects_by_country', (
      select coalesce(jsonb_agg(jsonb_build_object('key', s.k, 'n', s.n) order by s.n desc, s.k), '[]'::jsonb)
      from (select c.country as k, count(*) as n
            from public.projects p cross join lateral unnest(p.countries) as c(country) group by 1) s
    ),
    'projects_by_policy_area', (
      select coalesce(jsonb_agg(jsonb_build_object('key', s.k, 'n', s.n) order by s.n desc, s.k), '[]'::jsonb)
      from (select coalesce(policy_area, 'none') as k, count(*) as n from public.projects group by 1) s
    ),
    'projects_by_results', (
      select coalesce(jsonb_agg(jsonb_build_object('key', s.k, 'n', s.n) order by s.n desc, s.k), '[]'::jsonb)
      from (select coalesce(results, 'none') as k, count(*) as n from public.projects group by 1) s
    ),
    'csrs_by_year', (
      select coalesce(jsonb_agg(jsonb_build_object('year', s.y, 'n', s.n) order by s.y), '[]'::jsonb)
      from (select year as y, count(*) as n from public.csrs group by 1) s
    ),
    'csr_assessments', (
      select coalesce(jsonb_agg(jsonb_build_object('key', s.k, 'n', s.n) order by s.n desc, s.k), '[]'::jsonb)
      from (select coalesce(assessment, 'none') as k, count(*) as n from public.csrs group by 1) s
    ),
    'csr_coverage_by_country', (
      select coalesce(jsonb_agg(jsonb_build_object('country', s.k, 'csrs', s.total, 'csrs_with_project', s.cov)
                                order by s.k), '[]'::jsonb)
      from (select c.country as k, count(*) as total, count(cv.csr_id) as cov
            from public.csrs c left join covered cv on cv.csr_id = c.csr_id group by 1) s
    ),
    'links_by_level', (
      select coalesce(jsonb_agg(jsonb_build_object('key', s.k, 'n', s.n) order by s.n desc, s.k), '[]'::jsonb)
      from (select level as k, count(*) as n from public.project_csr_links group by 1) s
    ),
    'impact_scores', (
      select coalesce(jsonb_agg(jsonb_build_object('score', s.score, 'n', s.n) order by s.score), '[]'::jsonb)
      from (select g.score, count(l.project_id) as n
            from generate_series(0, 10) as g(score) left join latest l on l.score = g.score group by 1) s
    ),
    'impact_by_level', (
      select coalesce(jsonb_agg(jsonb_build_object('key', s.k, 'n', s.n) order by s.n desc, s.k), '[]'::jsonb)
      from (select coalesce(level, 'none') as k, count(*) as n from latest group by 1) s
    ),
    'impact_by_confidence', (
      select coalesce(jsonb_agg(jsonb_build_object('key', s.k, 'n', s.n) order by s.n desc, s.k), '[]'::jsonb)
      from (select coalesce(confidence, 'none') as k, count(*) as n from latest group by 1) s
    ),
    'impact_mean_by_policy_area', (
      select coalesce(jsonb_agg(jsonb_build_object('key', s.k, 'mean', s.mean, 'n', s.n) order by s.mean desc, s.k),
                      '[]'::jsonb)
      from (select coalesce(p.policy_area, 'none') as k, round(avg(l.score)::numeric, 2) as mean, count(*) as n
            from latest l join public.projects p on p.project_id = l.project_id
            where l.score is not null group by 1) s
    ),
    'impact_mean_by_country', (
      select coalesce(jsonb_agg(jsonb_build_object('key', s.k, 'mean', s.mean, 'n', s.n) order by s.mean desc, s.k),
                      '[]'::jsonb)
      from (select c.country as k, round(avg(l.score)::numeric, 2) as mean, count(*) as n
            from latest l join public.projects p on p.project_id = l.project_id
            cross join lateral unnest(p.countries) as c(country)
            where l.score is not null group by 1) s
    ),
    'impact_recent', (
      select coalesce(jsonb_agg(jsonb_build_object('project_id', s.project_id, 'title', s.title, 'score', s.score,
                                                   'level', s.level, 'confidence', s.confidence,
                                                   'created_at', s.created_at, 'source', s.source)
                                order by s.created_at desc, s.id desc), '[]'::jsonb)
      from (select r.id, r.project_id, r.title, r.score, r.level, r.confidence, r.created_at, r.source
            from public.impact_reports r order by r.created_at desc, r.id desc limit 10) s
    ),
    'anomalies_by_type_severity', (
      select coalesce(jsonb_agg(jsonb_build_object('type', s.t, 'severity', s.sev, 'n', s.n)
                                order by s.t, s.sev), '[]'::jsonb)
      from (select type as t, severity as sev, count(*) as n from public.anomalies group by 1, 2) s
    )
  );
$$;

revoke all on function public.tsi_dashboard() from public, anon;
grant execute on function public.tsi_dashboard() to authenticated, service_role;
