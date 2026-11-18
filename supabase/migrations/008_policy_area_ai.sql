-- Domaine politique attribue par IA (retours utilisateurs, lot 2) : projets sans page detail classes par
-- `python -m tsi_agent area-classify` (front matter policy_area_source: ai). L'explorateur l'affiche « (AI) ».
-- A coller dans Supabase > SQL Editor APRES 001 a 007. Idempotent (rejouable).
-- ATTENTION : le projet Supabase est PARTAGE avec d'autres applications (Europanel) : ce script n'ajoute qu'une colonne
-- a la table projects de l'explorateur ; rien d'autre.
alter table public.projects add column if not exists policy_area_ai boolean not null default false;
