import json

import pytest

from tsi_agent import csr_fetch


class FakeResponse:
    def __init__(self, content=b"", headers=None):
        self.content, self.headers = content, headers or {}

    def raise_for_status(self):
        pass


class FakeHttp:
    def __init__(self, xlsx=b"PKzip-bytes", token="tok", **_):
        self.xlsx, self.token, self.cookies, self.urls = xlsx, token, {"sid": "1"}, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.urls.append(url)
        if "csrftoken" in url:
            return FakeResponse(headers={"qlik-csrf-token": self.token} if self.token else {})
        if "/hub" in url:
            return FakeResponse()
        return FakeResponse(self.xlsx, {"content-type": "text/html"})


class FakeWs:
    def __init__(self, error_on=None):
        self.sent, self.queue, self.error_on = [], [], error_on

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        self.queue.append(json.dumps({"method": "OnConnected"}))  # message sans id : ignore
        if msg["method"] == self.error_on:
            reply = {"id": msg["id"], "error": {"message": "boom"}}
        else:
            result = {"qReturn": {"qHandle": 7}, "qLayout": {"qHyperCube": {"qSize": {"qcy": 12, "qcx": 3}}},
                      "qUrl": "/tmp/export.xlsx"}
            reply = {"id": msg["id"], "result": result}
        self.queue.append(json.dumps(reply))

    async def recv(self):
        return self.queue.pop(0)


def patch(monkeypatch, http, ws):
    monkeypatch.setattr(csr_fetch.httpx, "AsyncClient", lambda **kw: http)
    monkeypatch.setattr(csr_fetch, "connect", lambda *a, **kw: ws)


def test_fetch_calls_and_writes_xlsx(monkeypatch, tmp_path):
    http, ws = FakeHttp(), FakeWs()
    patch(monkeypatch, http, ws)
    out = tmp_path / "csr" / "csr_full.xlsx"
    info = csr_fetch.fetch(out)
    assert [m["method"] for m in ws.sent] == ["OpenDoc", "GetObject", "GetLayout", "ExportData"]
    assert out.read_bytes() == b"PKzip-bytes" and not out.with_suffix(".xlsx.part").exists()
    assert info["rows"] == 12 and info["columns"] == 3
    assert http.urls[-1] == f"https://{csr_fetch.BASE}/tmp/export.xlsx"


def test_jsonrpc_error_is_a_runtime_error(monkeypatch, tmp_path):
    patch(monkeypatch, FakeHttp(), FakeWs(error_on="GetObject"))
    with pytest.raises(RuntimeError, match="GetObject"):
        csr_fetch.fetch(tmp_path / "x.xlsx")


def test_missing_token_is_a_runtime_error(monkeypatch, tmp_path):
    patch(monkeypatch, FakeHttp(token=None), FakeWs())
    with pytest.raises(RuntimeError, match="qlik-csrf-token"):
        csr_fetch.fetch(tmp_path / "x.xlsx")


def test_non_zip_download_keeps_existing_export(monkeypatch, tmp_path):
    patch(monkeypatch, FakeHttp(xlsx=b"<html>error</html>"), FakeWs())
    out = tmp_path / "csr_full.xlsx"
    out.write_bytes(b"good")
    with pytest.raises(RuntimeError, match="content-type"):
        csr_fetch.fetch(out)
    assert out.read_bytes() == b"good" and not out.with_suffix(".xlsx.part").exists()
