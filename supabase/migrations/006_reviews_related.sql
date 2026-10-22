-- Avis des testeurs sur les liens projet -> CSR et projets passes lies aux recommandations de la Commission (lot 4).
-- A coller dans Supabase > SQL Editor APRES 001 a 005. Idempotent (rejouable).
-- ATTENTION : le projet Supabase est PARTAGE avec d'autres applications : ce script ne touche qu'a link_reviews (et sa
-- sequence d'identite), a la vue link_review_latest, a csr_related_projects et a la fonction tsi_dashboard ; rien
-- d'autre.

-- Avis des testeurs (historique en ajout seul, ecrit DEPUIS LA PAGE par les testeurs ; supabase-sync n'y touche
-- jamais). Pas de cle etrangere vers project_csr_links : supabase-sync vide et recharge les liens, une cascade
-- effacerait l'historique. reviewer_id et reviewer_email viennent du jeton de l'appelant (valeurs par defaut, et
-- verifies par la regle d'insertion).
create table if not exists public.link_reviews (
  id bigint generated always as identity primary key,
  project_id text not null,
  part_id text not null,
  status text not null check (status in ('confirmed', 'rejected', 'to_review')),
  comment text check (char_length(comment) <= 2000),
  reviewer_id uuid not null default auth.uid(),
  reviewer_email text not null default (auth.jwt() ->> 'email'),
  created_at timestamptz not null default now()
);
create index if not exists link_reviews_link_created_idx on public.link_reviews (project_id, part_id, created_at desc);

-- Droits : lecture et insertion pour authenticated (filtrees par RLS), ni modification ni suppression ; service_role
-- garde tous les droits. La sequence d'identite n'a besoin d'aucun droit (valeur generee par le serveur).
revoke all on public.link_reviews from anon, authenticated;
grant select, insert on public.link_reviews to authenticated;
grant all on public.link_reviews to service_role;
do $$
declare seq text := pg_get_serial_sequence('public.link_reviews', 'id');
begin
  if seq is not null then
    execute format('revoke usage, select, update on sequence %s from anon, authenticated', seq);
  end if;
end $$;

alter table public.link_reviews enable row level security;
drop policy if exists tester_read on public.link_reviews;
create policy tester_read on public.link_reviews for select to authenticated using ((select public.is_tester()));
drop policy if exists tester_insert on public.link_reviews;
create policy tester_insert on public.link_reviews for insert to authenticated
  with check (reviewer_id = (select auth.uid())
              and reviewer_email = (select auth.jwt() ->> 'email')
              and (select public.is_tester()));
-- Pas de regle update / delete : l'historique ne se modifie pas (un nouvel avis remplace le precedent).

-- Dernier avis par lien (project_id, part_id), pour la page. security_invoker : le RLS des testeurs s'applique.
create or replace view public.link_review_latest with (security_invoker = true) as
  select distinct on (r.project_id, r.part_id)
         r.id, r.project_id, r.part_id, r.status, r.comment, r.reviewer_id, r.reviewer_email, r.created_at
  from public.link_reviews r
  order by r.project_id, r.part_id, r.created_at desc, r.id desc;
revoke all on public.link_review_latest from anon, authenticated;
grant select on public.link_review_latest to authenticated;
grant all on public.link_review_latest to service_role;

-- Projets passes sur le meme sujet qu'une recommandation (csr-related, juge par IA) ; remplacee en entier par
-- supabase-sync depuis output/csr_related/*.json. Pas de cle etrangere (meme raison que impact_reports).
create table if not exists public.csr_related_projects (
  csr_id text not null,
  project_id text not null,
  relevance text not null check (relevance in ('high', 'medium')),
  scope text not null check (scope in ('same_country', 'other_country')),
  rationale text,
  primary key (csr_id, project_id)
);
create index if not exists csr_related_projects_project_idx on public.csr_related_projects (project_id);

revoke all on public.csr_related_projects from anon, authenticated;
grant select on public.csr_related_projects to authenticated;
grant all on public.csr_related_projects to service_role;

alter table public.csr_related_projects enable row level security;
drop policy if exists tester_read on public.csr_related_projects;
create policy tester_read on public.csr_related_projects for select to authenticated
  using ((select public.is_tester()));

-- Tableau de bord : memes cles et formes que 005, plus
--   totals.link_reviews (nombre d'avis, historique compris) ;
--   review_by_level[] = {level, confirmed, rejected, to_review, not_reviewed} : dernier avis de chaque lien actuel
--   (project_csr_links), par niveau de lien (direct / partial) ; not_reviewed = liens sans avis.
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
  ),
  review_latest as (
    select distinct on (r.project_id, r.part_id) r.project_id, r.part_id, r.status
    from public.link_reviews r
    order by r.project_id, r.part_id, r.created_at desc, r.id desc
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
      'anomalies_high', (select count(*) from public.anomalies where severity = 'high'),
      'link_reviews', (select count(*) from public.link_reviews)
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
      -- Couverture hors recommandations de la Commission (annee en cours, aucun projet ne peut encore les soutenir).
      from (select c.country as k, count(*) as total, count(cv.csr_id) as cov
            from public.csrs c left join covered cv on cv.csr_id = c.csr_id
            left join public.csr_documents d on d.doc_id = c.doc_id
            where d.stage is distinct from 'Commission recommendation' group by 1) s
      left join (select d.country, count(*) as total, count(cd.doc_id) as cov
                 from public.csr_documents d left join covered_docs cd on cd.doc_id = d.doc_id
                 where d.stage is distinct from 'Commission recommendation'
                 group by 1) dc on dc.country = s.k
    ),
    'links_by_level', (
      select coalesce(jsonb_agg(jsonb_build_object('key', s.k, 'n', s.n) order by s.n desc, s.k), '[]'::jsonb)
      from (select level as k, count(*) as n from public.project_csr_links group by 1) s
    ),
    'review_by_level', (
      select coalesce(jsonb_agg(jsonb_build_object('level', s.k, 'confirmed', s.confirmed, 'rejected', s.rejected,
                                                   'to_review', s.to_review, 'not_reviewed', s.not_reviewed)
                                order by s.k), '[]'::jsonb)
      from (select l.level as k,
                   count(*) filter (where rl.status = 'confirmed') as confirmed,
                   count(*) filter (where rl.status = 'rejected') as rejected,
                   count(*) filter (where rl.status = 'to_review') as to_review,
                   count(*) filter (where rl.status is null) as not_reviewed
            from public.project_csr_links l
            left join review_latest rl on rl.project_id = l.project_id and rl.part_id = l.part_id
            group by 1) s
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

notify pgrst, 'reload schema';
