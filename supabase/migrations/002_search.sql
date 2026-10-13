-- Recherche plein texte pour l'assistant Claude (lot 2b). A coller dans Supabase > SQL Editor. Idempotent.
-- Colonnes generees `fts` (jamais envoyees par supabase-sync) et fonctions en security invoker : le RLS des testeurs
-- s'applique aux appels faits avec le jeton de l'utilisateur.
alter table public.projects add column if not exists fts tsvector
  generated always as (to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, ''))) stored;
create index if not exists projects_fts_idx on public.projects using gin (fts);
alter table public.csrs add column if not exists fts tsvector
  generated always as (to_tsvector('english', coalesce(title, '') || ' ' || coalesce(text, ''))) stored;
create index if not exists csrs_fts_idx on public.csrs using gin (fts);

create or replace function public.search_projects(q text, p_countries text[] default null,
  p_programme text default null, p_year_from int default null, p_year_to int default null, lim int default 10)
returns table (project_id text, title text, programme text, year int, countries text[], policy_area text,
               detail_page boolean, url text, summary text, csrs text[], rank real)
language sql stable security invoker set search_path = public as $$
  select p.project_id, p.title, p.programme, p.year, p.countries, p.policy_area, p.detail_page, p.url,
         left(p.summary, 600), p.csrs, ts_rank(p.fts, websearch_to_tsquery('english', q))
  from public.projects p
  where (p.fts @@ websearch_to_tsquery('english', q) or p.title ilike '%' || q || '%')
    and (p_countries is null or p.countries && p_countries)
    and (p_programme is null or p.programme = upper(p_programme))
    and (p_year_from is null or p.year >= p_year_from)
    and (p_year_to is null or p.year <= p_year_to)
  order by 11 desc, p.year desc nulls last
  limit least(greatest(coalesce(lim, 10), 1), 20);
$$;

create or replace function public.search_csrs(q text, p_countries text[] default null,
  p_year_from int default null, p_year_to int default null, lim int default 10)
returns table (csr_id text, country text, year int, title text, text text, assessment text,
               assessment_year int, rank real)
language sql stable security invoker set search_path = public as $$
  select c.csr_id, c.country, c.year, c.title, left(c.text, 800), c.assessment, c.assessment_year,
         ts_rank(c.fts, websearch_to_tsquery('english', q))
  from public.csrs c
  where c.fts @@ websearch_to_tsquery('english', q)
    and (p_countries is null or c.country = any (p_countries))
    and (p_year_from is null or c.year >= p_year_from)
    and (p_year_to is null or c.year <= p_year_to)
  order by 8 desc, c.year desc
  limit least(greatest(coalesce(lim, 10), 1), 20);
$$;

revoke all on function public.search_projects(text, text[], text, int, int, int) from public, anon;
revoke all on function public.search_csrs(text, text[], int, int, int) from public, anon;
grant execute on function public.search_projects(text, text[], text, int, int, int) to authenticated, service_role;
grant execute on function public.search_csrs(text, text[], int, int, int) to authenticated, service_role;
