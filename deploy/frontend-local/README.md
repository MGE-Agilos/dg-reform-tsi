# Explorateur web sur un serveur local

L'explorateur (`web/` : projets, CSR, anomalies, onglets *Chat* et *Impact*) est publié sur GitHub Pages
(<https://mge-agilos.github.io/dg-reform-tsi/>). Ce dossier permet de le servir aussi depuis un PC ou un serveur
interne, avec sa propre configuration. Les données restent dans Supabase : seule la page est servie en local.

| Fichier | Rôle |
|---|---|
| `config.local.example.js` | modèle de configuration, à copier vers `config.local.js` (non versionné) |
| `docker-compose.yml`, `nginx.conf` | mode Docker : nginx sert `web/`, avec `config.local.js` à la place de `config.js` |
| `serve.ps1` | mode sans Docker : serveur HTTP de Python sur une copie de `web/` (`dist/`, non versionné) |

## 1. Configuration

```powershell
Copy-Item deploy\frontend-local\config.local.example.js deploy\frontend-local\config.local.js
notepad deploy\frontend-local\config.local.js
```

- `SUPABASE_URL` et `SUPABASE_ANON_KEY` : projet Supabase (par défaut le même que GitHub Pages). La clé `anon` est
  publique par nature ; **ne jamais** y mettre la clé `service_role`.
- `PROXY_URL` : service des onglets *Chat* et *Impact*. Garder une seule des deux lignes :
  - `tsi-assistant` : Claude (Anthropic), sur les données Supabase, comme sur GitHub Pages ;
  - `haystack-proxy` : services `tsi-chat` et `tsi-impact` de Haystack Enterprise (LLM du client). La fonction doit
    être déployée avec ses secrets (README racine, « Chat et impact (lot 2) »).

## 2. Lancer

**Avec Docker** (Docker Desktop ou Docker Engine) :

```powershell
cd deploy\frontend-local
docker compose up -d        # http://localhost:8080/
docker compose down         # arrêt
```

Pour un autre port, changer `"8080:80"` dans `docker-compose.yml`. Après une modification de `web/` ou de
`config.local.js`, recharger la page suffit (fichiers montés, `config.js` jamais mis en cache).

Créer `config.local.js` **avant** `docker compose up` : si le fichier manque, Docker crée à sa place un **dossier**
`config.local.js` vide et la page ne trouve pas sa configuration. Dans ce cas : `docker compose down`, supprimer ce
dossier, copier l'exemple, relancer.

**Sans Docker** (Python 3 suffit, celui de `.venv` s'il existe) :

Dans la console PowerShell (pas ISE), autoriser d'abord les scripts dans la fenêtre :
`Set-ExecutionPolicy -Scope Process Bypass` (ou lancer `powershell -ExecutionPolicy Bypass -File
.\deploy\frontend-local\serve.ps1`). Dépôt récupéré en ZIP : `Get-ChildItem -Recurse -Filter *.ps1 | Unblock-File`
une fois.

```powershell
.\deploy\frontend-local\serve.ps1                    # http://localhost:8000/
.\deploy\frontend-local\serve.ps1 -Port 8001
.\deploy\frontend-local\serve.ps1 -Bind 0.0.0.0      # accessible depuis les autres postes du réseau
```

Le script recopie `web/` dans `dist/` à chaque lancement : relancer après une modification. Ctrl+C pour arrêter. Le
serveur de Python convient pour des essais et quelques testeurs ; pour un service permanent, préférer Docker.

## 3. Supabase : autoriser l'adresse locale

Sans ces réglages, la connexion ou les onglets *Chat* et *Impact* échouent.

1. **Redirect URLs.** *Authentication > URL Configuration > Redirect URLs* : ajouter l'adresse de la page suivie de
   `/**`, par exemple `http://localhost:8080/**` (Docker), `http://localhost:8000/**` (`serve.ps1`) ou
   `https://<serveur>:<port>/**`. Le lien magique ramène l'utilisateur à l'adresse depuis laquelle il l'a demandé.
2. **Origines autorisées des Edge Functions.** Les fonctions `tsi-assistant` et `haystack-proxy` n'acceptent par
   défaut que `https://mge-agilos.github.io` et `http://localhost:8000`. Pour une autre adresse, renseigner le secret
   `ALLOWED_ORIGINS` (*Edge Functions > Secrets*) avec **toutes** les origines, séparées par des virgules (la valeur
   remplace la liste par défaut), par exemple :
   `https://mge-agilos.github.io,http://localhost:8000,http://localhost:8080`. Une origine est le schéma, l'hôte et le
   port, sans chemin ni `/` final.

`http://localhost:8000` et `http://127.0.0.1:8000` sont deux origines différentes pour le navigateur et pour
Supabase : ouvrir la page à l'adresse affichée par le script (`http://localhost:<port>/`), celle qui est déclarée
ci-dessus, et pas une autre écriture de la même machine.

## 4. Sur un vrai serveur

- Mettre **HTTPS** devant (reverse proxy : IIS, nginx, Caddy, Traefik… avec un certificat). En HTTP sur une autre
  adresse que `localhost`, le navigateur bloque certaines fonctions (copie dans le presse-papiers du rapport
  d'impact) et le lien magique arrive sur une page non sécurisée.
- Ouvrir le port choisi dans le pare-feu du serveur seulement si la page doit être vue depuis d'autres postes.

## Testeurs

L'accès reste géré dans Supabase, comme pour GitHub Pages : invitation dans *Authentication > Users*, puis ajout de
l'e-mail à la table `testers` (README racine, « Explorateur web »). Un serveur local ne donne accès à aucune donnée
sans connexion d'un testeur.
