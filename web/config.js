// Configuration publique : la cle anon est faite pour etre publiee (lecture protegee par RLS, testeurs seulement).
// NE JAMAIS mettre ici la cle service_role.
export const SUPABASE_URL = "https://vxwmhsgxoomcbiukeinp.supabase.co";
export const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZ4d21oc2d4b29tY2JpdWtlaW5wIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYyNTMwODUsImV4cCI6MjA5MTgyOTA4NX0.DYfan7mRM0dYkB9xouBjb2Y1ONdmZxByrsr29nSZuAE";
// Chat et Impact : Edge Function tsi-assistant (Claude, Anthropic) ; aucune cle ici, la cle Anthropic reste dans les secrets
// de la fonction. Alternative (services Haystack de l'environnement client) : SUPABASE_URL + "/functions/v1/haystack-proxy".
export const PROXY_URL = SUPABASE_URL + "/functions/v1/tsi-assistant";
