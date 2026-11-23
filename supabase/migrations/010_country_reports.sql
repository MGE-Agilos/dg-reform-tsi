-- Country reports du Semestre europeen 2026 (retours utilisateurs, lot 3) : un rapport de la Commission par Etat membre
-- (`python -m tsi_agent country-reports`), decoupe en passages cherchables.
-- A coller dans Supabase > SQL Editor APRES 001 a 009, AVANT `supabase-sync` (qui remplit les tables). Idempotent.
-- ATTENTION : le projet Supabase est PARTAGE avec d'autres applications : ce script ne touche qu'aux tables
-- country_reports et country_report_passages et a la fonction tsi_search_country_reports ; rien d'autre.

-- Remplacees en entier par supabase-sync. Pas de cle etrangere (meme raison que csr_recitals).
create table if not exists public.country_reports (
  report_id text primary key,
  country text not null,
  year int not null,
  title text not null,
  source_url text,
  page_url text,
  pages int
);
create table if not exists public.country_report_passages (
  passage_id text primary key,
  report_id text not null,
  country text not null,
  year int not null,
  page int not null,
  seq int not null,
  text text not null,
  source_url text
);
-- Recherche plein texte (anglais), colonne generee : jamais envoyee par supabase-sync.
alter table public.country_report_passages add column if not exists fts tsvector
  generated always as (to_tsvector('english', coalesce(text, ''))) stored;
create index if not exists country_report_passages_fts_idx on public.country_report_passages using gin (fts);
create index if not exists country_report_passages_country_idx
  on public.country_report_passages (country, year, page, seq);

revoke all on public.country_reports, public.country_report_passages from anon, authenticated;
grant select on public.country_reports, public.country_report_passages to authenticated;
grant all on public.country_reports, public.country_report_passages to service_role;

alter table public.country_reports enable row level security;
drop policy if exists tester_read on public.country_reports;
create policy tester_read on public.country_reports for select to authenticated using ((select public.is_tester()));
alter table public.country_report_passages enable row level security;
drop policy if exists tester_read on public.country_report_passages;
create policy tester_read on public.country_report_passages for select to authenticated
  using ((select public.is_tester()));

-- Recherche classee pour l'assistant (outil search_country_reports) ; security invoker : le RLS des testeurs s'applique.
create or replace function public.tsi_search_country_reports(q text, p_countries text[] default null,
  lim int default 8)
returns table (passage_id text, country text, year int, page int, text text, source_url text, rank real)
language sql stable security invoker set search_path = public as $$
  select r.passage_id, r.country, r.year, r.page, left(r.text, 1600), r.source_url,
         ts_rank(r.fts, websearch_to_tsquery('english', q))
  from public.country_report_passages r
  where r.fts @@ websearch_to_tsquery('english', q)
    and (p_countries is null or r.country = any (p_countries))
  order by 7 desc, r.country, r.page, r.seq
  limit least(greatest(coalesce(lim, 8), 1), 20);
$$;
revoke all on function public.tsi_search_country_reports(text, text[], int) from public, anon;
grant execute on function public.tsi_search_country_reports(text, text[], int) to authenticated, service_role;
