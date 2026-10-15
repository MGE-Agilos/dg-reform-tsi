// Configuration of the local front end: copy this file to config.local.js (same folder), then adapt it.
// Same exports as web/config.js; config.local.js replaces config.js at run time (docker-compose.yml or serve.ps1)
// and is not in Git.
//
// Supabase: project URL and anon key (public by nature: reading is protected by RLS, testers only).
// NEVER put the service_role key here.
export const SUPABASE_URL = "https://vxwmhsgxoomcbiukeinp.supabase.co";
export const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZ4d21oc2d4b29tY2JpdWtlaW5wIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzYyNTMwODUsImV4cCI6MjA5MTgyOTA4NX0.DYfan7mRM0dYkB9xouBjb2Y1ONdmZxByrsr29nSZuAE";

// Chat and Impact tabs: Edge Function called by the page (no key here, the keys stay in the function's
// secrets). Choose one of the two:
// - tsi-assistant  : Claude (Anthropic), on the Supabase data; this is the choice of GitHub Pages.
// - haystack-proxy : tsi-chat and tsi-impact services of Haystack Enterprise (client's LLM, gpt-5.1 through EC GPT).
// In both cases, add the address of this server to the function's ALLOWED_ORIGINS secret (see README.md).
export const PROXY_URL = SUPABASE_URL + "/functions/v1/tsi-assistant";
// export const PROXY_URL = SUPABASE_URL + "/functions/v1/haystack-proxy";
