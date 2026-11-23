-- Considerants (recitals) des recommandations de la Commission (retours utilisateurs, lot 3) : paragraphes « (1) »,
-- « (2) »... qui precedent les recommandations dans chaque document 2026 (analyse des defis du pays).
-- A coller dans Supabase > SQL Editor APRES 001 a 008, AVANT `supabase-sync` (qui remplit la table). Idempotent.
-- ATTENTION : le projet Supabase est PARTAGE avec d'autres applications : ce script ne touche qu'a la table
-- csr_recitals ; rien d'autre.

-- Remplacee en entier par supabase-sync (texte de la publication de la Commission). Pas de cle etrangere (meme raison
-- que csr_related_projects : la synchronisation vide et recharge les tables).
create table if not exists public.csr_recitals (
  recital_id text primary key,
  country text not null,
  year int not null,
  number int not null,
  text text not null,
  source_url text,
  doc_id text
);
-- Recherche plein texte (anglais), colonne generee : jamais envoyee par supabase-sync.
alter table public.csr_recitals add column if not exists fts tsvector
  generated always as (to_tsvector('english', coalesce(text, ''))) stored;
create index if not exists csr_recitals_fts_idx on public.csr_recitals using gin (fts);
create index if not exists csr_recitals_country_year_idx on public.csr_recitals (country, year, number);

revoke all on public.csr_recitals from anon, authenticated;
grant select on public.csr_recitals to authenticated;
grant all on public.csr_recitals to service_role;

alter table public.csr_recitals enable row level security;
drop policy if exists tester_read on public.csr_recitals;
create policy tester_read on public.csr_recitals for select to authenticated
  using ((select public.is_tester()));

-- Recherche classee pour l'assistant (outil search_recitals), comme tsi_search_csrs (002) ; security invoker : le RLS
-- des testeurs s'applique.
create or replace function public.tsi_search_recitals(q text, p_countries text[] default null,
  p_year_from int default null, p_year_to int default null, lim int default 10)
returns table (recital_id text, country text, year int, number int, text text, source_url text, rank real)
language sql stable security invoker set search_path = public as $$
  select r.recital_id, r.country, r.year, r.number, left(r.text, 1500), r.source_url,
         ts_rank(r.fts, websearch_to_tsquery('english', q))
  from public.csr_recitals r
  where r.fts @@ websearch_to_tsquery('english', q)
    and (p_countries is null or r.country = any (p_countries))
    and (p_year_from is null or r.year >= p_year_from)
    and (p_year_to is null or r.year <= p_year_to)
  order by 7 desc, r.country, r.number
  limit least(greatest(coalesce(lim, 10), 1), 20);
$$;
revoke all on function public.tsi_search_recitals(text, text[], int, int, int) from public, anon;
grant execute on function public.tsi_search_recitals(text, text[], int, int, int) to authenticated, service_role;
