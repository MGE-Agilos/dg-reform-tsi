// Configuration du front end local : copier ce fichier vers config.local.js (meme dossier), puis l'adapter.
// Memes exports que web/config.js ; config.local.js remplace config.js a l'execution (docker-compose.yml ou serve.ps1)
// et n'est pas versionne.
//
// Supabase : URL du projet et cle anon (publique par nature : la lecture est protegee par RLS, testeurs seulement).
// NE JAMAIS mettre ici la cle service_role.
export const SUPABASE_URL = "https://vxwmhsgxoomcbiukeinp.supabase.co";
export const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZ4d21oc2d4b29tY2JpdWtlaW5wIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYyNTMwODUsImV4cCI6MjA5MTgyOTA4NX0.DYfan7mRM0dYkB9xouBjb2Y1ONdmZxByrsr29nSZuAE";

// Onglets Chat et Impact : Edge Function appelee par la page (aucune cle ici, elles restent dans les secrets de la
// fonction). Choisir l'une des deux :
// - tsi-assistant  : Claude (Anthropic), sur les donnees Supabase ; c'est le choix de GitHub Pages.
// - haystack-proxy : services tsi-chat et tsi-impact de Haystack Enterprise (LLM du client, gpt-5.1 via EC GPT).
// Dans les deux cas, ajouter l'adresse de ce serveur au secret ALLOWED_ORIGINS de la fonction (voir README.md).
export const PROXY_URL = SUPABASE_URL + "/functions/v1/tsi-assistant";
// export const PROXY_URL = SUPABASE_URL + "/functions/v1/haystack-proxy";
