"""Export complet de la base CSR d'ECFIN (application Qlik Sense publique) : reproduit le bouton
« Export Full Database » (ExportData OOXML du tableau) et telecharge le fichier xlsx."""

import asyncio
import json
import random
from pathlib import Path

import httpx
import websockets

BASE = "dashboard.tech.ec.europa.eu/qs_digit_dashboard_mt/public"
APP = "e94667ab-79dd-4636-8ba0-1ae23901f86c"
OBJECT = "nDjMYs"  # tableau de la base complete
HEADERS = {"User-Agent": "TSI-Extraction-Agent/0.1 (DG REFORM project; polite crawler)"}
TIMEOUT_S = 120


async def _export(out: Path) -> dict:
    xrf = "".join(random.choice("0123456789ABCDEF") for _ in range(16))
    async with httpx.AsyncClient(follow_redirects=True, headers=HEADERS, timeout=TIMEOUT_S) as http:
        await http.get(f"https://{BASE}/hub")
        res = await http.get(f"https://{BASE}/qps/csrftoken?xrfkey={xrf}", headers={"X-Qlik-Xrfkey": xrf})
        token = res.headers.get("qlik-csrf-token")
        cookie = "; ".join(f"{k}={v}" for k, v in http.cookies.items())
        url = f"wss://{BASE}/app/{APP}?qlik-csrf-token={token}"
        async with websockets.connect(url, additional_headers={**HEADERS, "Cookie": cookie}, max_size=None) as ws:
            seq = 0

            async def call(handle: int, method: str, params: list) -> dict:
                nonlocal seq
                seq += 1
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": seq, "handle": handle, "method": method,
                                          "params": params}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))
                    if msg.get("id") == seq:
                        if "error" in msg:
                            raise RuntimeError(f"Qlik {method}: {msg['error']}")
                        return msg["result"]

            doc = await call(-1, "OpenDoc", [APP])
            obj = await call(doc["qReturn"]["qHandle"], "GetObject", [OBJECT])
            handle = obj["qReturn"]["qHandle"]
            layout = await call(handle, "GetLayout", [])
            size = layout["qLayout"].get("qHyperCube", {}).get("qSize", {})
            exported = await call(handle, "ExportData", ["OOXML", "/qHyperCubeDef"])
        data = await http.get(f"https://{BASE}{exported['qUrl']}")
        data.raise_for_status()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data.content)
    return {"file": str(out), "bytes": len(data.content), "rows": size.get("qcy"), "columns": size.get("qcx")}


def fetch(out: Path) -> dict:
    """Telecharge l'export complet dans `out` (ecrase) ; renvoie taille et nombre de lignes annonce par Qlik."""
    return asyncio.run(_export(out))
