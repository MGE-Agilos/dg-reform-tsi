"""Export complet de la base CSR d'ECFIN (application Qlik Sense publique) : reproduit le bouton
« Export Full Database » (ExportData OOXML du tableau) et telecharge le fichier xlsx."""

import asyncio
import json
import secrets
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

BASE = "dashboard.tech.ec.europa.eu/qs_digit_dashboard_mt/public"
APP = "e94667ab-79dd-4636-8ba0-1ae23901f86c"
OBJECT = "nDjMYs"  # tableau de la base complete
HEADERS = {"User-Agent": "TSI-Extraction-Agent/0.1 (DG REFORM project; polite crawler)"}
TIMEOUT_S = 120


async def _export(out: Path) -> dict:
    xrf = secrets.token_hex(8).upper()
    async with httpx.AsyncClient(follow_redirects=True, headers=HEADERS, timeout=TIMEOUT_S) as http:
        (await http.get(f"https://{BASE}/hub")).raise_for_status()
        res = await http.get(f"https://{BASE}/qps/csrftoken?xrfkey={xrf}", headers={"X-Qlik-Xrfkey": xrf})
        res.raise_for_status()
        token = res.headers.get("qlik-csrf-token")
        if not token:
            raise RuntimeError("Qlik: no qlik-csrf-token header")
        cookie = "; ".join(f"{k}={v}" for k, v in http.cookies.items())
        url = f"wss://{BASE}/app/{APP}?qlik-csrf-token={token}"
        async with connect(url, additional_headers={**HEADERS, "Cookie": cookie}, max_size=None) as ws:
            seq = 0

            async def call(handle: int, method: str, params: list) -> dict:
                nonlocal seq
                seq += 1
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": seq, "handle": handle, "method": method,
                                          "params": params}))

                async def reply() -> dict:
                    while True:
                        msg = json.loads(await ws.recv())
                        if msg.get("id") == seq:
                            if "error" in msg:
                                raise RuntimeError(f"Qlik {method}: {msg['error']}")
                            return msg["result"]

                return await asyncio.wait_for(reply(), TIMEOUT_S)  # delai global, pas par message

            doc = await call(-1, "OpenDoc", [APP])
            obj = await call(doc["qReturn"]["qHandle"], "GetObject", [OBJECT])
            handle = obj["qReturn"]["qHandle"]
            layout = await call(handle, "GetLayout", [])
            size = layout["qLayout"].get("qHyperCube", {}).get("qSize", {})
            exported = await call(handle, "ExportData", ["OOXML", "/qHyperCubeDef"])
        q_url = exported["qUrl"]
        if q_url.startswith("http"):
            download = q_url
        elif q_url.startswith("/qs_digit_dashboard_mt/public/"):
            download = f"https://dashboard.tech.ec.europa.eu{q_url}"
        else:
            download = f"https://{BASE}{q_url}"
        data = await http.get(download)
        data.raise_for_status()
    if not data.content.startswith(b"PK"):  # un xlsx est un zip
        raise RuntimeError(f"Qlik: le telechargement n'est pas un xlsx (content-type {data.headers.get('content-type')})")
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(".xlsx.part")
    part.write_bytes(data.content)
    part.replace(out)  # ecriture atomique : un echec ne remplace jamais un bon export
    return {"file": str(out), "bytes": len(data.content), "rows": size.get("qcy"), "columns": size.get("qcx")}


def fetch(out: Path) -> dict:
    """Telecharge l'export complet dans `out` (ecrase) ; renvoie taille et nombre de lignes annonce par Qlik."""
    return asyncio.run(_export(out))
