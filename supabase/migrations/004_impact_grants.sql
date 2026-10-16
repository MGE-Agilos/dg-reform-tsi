-- Droits resserres sur impact_reports (lot 3, correctif). A coller dans Supabase > SQL Editor. Idempotent (rejouable).
-- Necessite 003_dashboard.sql.
-- ATTENTION : le projet Supabase est PARTAGE avec d'autres applications : ce script ne touche qu'a impact_reports et a
-- sa sequence d'identite.

-- Table : tout retirer a anon et authenticated (y compris references, trigger et les privileges par defaut du
-- projet), puis lecture seule pour authenticated, filtree par le RLS des testeurs (tester_read). service_role inchange.
revoke all on public.impact_reports from anon, authenticated;
grant select on public.impact_reports to authenticated;

-- Sequence de la colonne identite id : aucun droit pour anon et authenticated (les insertions passent par service_role).
do $$
declare seq text := pg_get_serial_sequence('public.impact_reports', 'id');
begin
  if seq is not null then
    execute format('revoke usage, select, update on sequence %s from anon, authenticated', seq);
  end if;
end $$;
