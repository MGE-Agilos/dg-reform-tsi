// config.js of a self-hosted installation (deploy/onprem/README.md, step 7): copy it over web/config.js in the
// published folder. The page talks to the application service on the same site (/api); it holds no key or secret.
export const AUTH_MODE = "server";
export const SUPABASE_URL = new URL("api", new URL("./", location.href)).href; // .../api, data requests: .../api/rest/v1
export const SUPABASE_ANON_KEY = "server"; // not a secret: the service ignores it (each request carries the user's token)
export const PROXY_URL = SUPABASE_URL + "/assistant";
