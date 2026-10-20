-- Couche « document CSR » (un document de recommandations par Etat membre et par an, 27 par an). A coller dans
-- Supabase > SQL Editor APRES 001 a 004, AVANT `supabase-sync` (qui envoie csrs.doc_id). Idempotent (rejouable).
-- ATTENTION : le projet Supabase est PARTAGE avec d'autres applications : ce script ne touche qu'a csr_documents
-- (nouvelle table de l'explorateur), a la colonne csrs.doc_id et a la fonction tsi_dashboard ; rien d'autre.

-- Documents : derives des recommandations par supabase-sync (doc_id = csrdoc-<ISO>-<annee>).
-- stage : 'Council recommendation' (2016-2025, base ECFIN) ou 'Commission recommendation' (2026, pas encore adoptee).
-- document_url : PDF anglais du pays (2026) ; base ECFIN sinon (pas d'URL par document).
create table if not exists public.csr_documents (
  doc_id text primary key,
  country text not null,
  year int not null,
  title text not null,
  stage text not null,
  n_recommendations int not null default 0,
  document_url text
);
create index if not exists csr_documents_country_year_idx on public.csr_documents (country, year);

-- Recommandation -> document (supabase-sync vide csrs avant csr_documents, et remplit csr_documents avant csrs).
alter table public.csrs add column if not exists doc_id text;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'csrs_doc_id_fkey'
                 and conrelid = 'public.csrs'::regclass) then
    alter table public.csrs add constraint csrs_doc_id_fkey
      foreign key (doc_id) references public.csr_documents (doc_id) on delete cascade;
  end if;
end $$;
create index if not exists csrs_doc_idx on public.csrs (doc_id);

-- Droits comme csrs : tout retirer a anon et authenticated, lecture seule pour authenticated (RLS des testeurs),
-- service_role (supabase-sync) garde tous les droits.
revoke all on public.csr_documents from anon, authenticated;
grant select on public.csr_documents to authenticated;
grant all on public.csr_documents to service_role;

alter table public.csr_documents enable row level security;
drop policy if exists tester_read on public.csr_documents;
create policy tester_read on public.csr_documents for select to authenticated using ((select public.is_tester()));

-- Tableau de bord : memes cles et formes que 003, plus
--   totals.csr_documents, totals.csr_documents_latest_year (documents de la derniere annee) ;
--   csrs_by_year[] = {year, n, n_documents, n_recommendations} (n = n_documents, compatibilite) ;
--   csr_coverage_by_country[] gagne documents et documents_with_project (documents dont une recommandation a au
--   moins une sous-partie liee a un projet).
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
  ),
  covered_docs as (
    select distinct c.doc_id
    from public.csrs c
    join covered cv on cv.csr_id = c.csr_id
    where c.doc_id is not null
  )
  select jsonb_build_object(
    'totals', jsonb_build_object(
      'projects', (select count(*) from public.projects),
      'projects_with_detail', (select count(*) from public.projects where detail_page),
      'deliverables', (select coalesce(sum(deliverables), 0) from public.projects),
      'csrs', (select count(*) from public.csrs),
      'csr_documents', (select count(*) from public.csr_documents),
      'csr_documents_latest_year', (select count(*) from public.csr_documents
                                    where year = (select max(year) from public.csr_documents)),
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
      select coalesce(jsonb_agg(jsonb_build_object('year', s.y, 'n', s.nd, 'n_documents', s.nd,
                                                   'n_recommendations', s.nr) order by s.y), '[]'::jsonb)
      from (select y, coalesce(d.nd, 0) as nd, coalesce(r.nr, 0) as nr
            from (select year as y from public.csr_documents union select year from public.csrs) years
            left join (select year, count(*) as nd from public.csr_documents group by 1) d on d.year = years.y
            left join (select year, count(*) as nr from public.csrs group by 1) r on r.year = years.y) s
    ),
    'csr_assessments', (
      select coalesce(jsonb_agg(jsonb_build_object('key', s.k, 'n', s.n) order by s.n desc, s.k), '[]'::jsonb)
      from (select coalesce(assessment, 'none') as k, count(*) as n from public.csrs group by 1) s
    ),
    'csr_coverage_by_country', (
      select coalesce(jsonb_agg(jsonb_build_object('country', s.k, 'csrs', s.total, 'csrs_with_project', s.cov,
                                                   'documents', coalesce(dc.total, 0),
                                                   'documents_with_project', coalesce(dc.cov, 0))
                                order by s.k), '[]'::jsonb)
      from (select c.country as k, count(*) as total, count(cv.csr_id) as cov
            from public.csrs c left join covered cv on cv.csr_id = c.csr_id group by 1) s
      left join (select d.country, count(*) as total, count(cd.doc_id) as cov
                 from public.csr_documents d left join covered_docs cd on cd.doc_id = d.doc_id
                 group by 1) dc on dc.country = s.k
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
