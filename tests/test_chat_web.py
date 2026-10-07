from haystack.components.embedders import MockTextEmbedder

from test_chat_agent import POLICY_AREAS, ScriptedGenerator, chat_settings
from tsi_agent.chat.pipeline import build_chat_pipeline
from tsi_agent.chat.web import create_app
from tsi_agent.components.claude_cli import LLMCallError, LLMQuotaExceeded


def client_with(tmp_path, replies):
    s = chat_settings(tmp_path)
    pipe = build_chat_pipeline(s, POLICY_AREAS, chat_generator=ScriptedGenerator(replies),
                               text_embedder=MockTextEmbedder(model="mock", dimension=8))
    return create_app(s, pipeline=pipe).test_client()


def test_index_page(tmp_path):
    res = client_with(tmp_path, []).get("/")
    assert res.status_code == 200 and b"<html" in res.data and b"/api/chat" in res.data


def test_chat_api(tmp_path):
    client = client_with(tmp_path, [[("search_projects", {"query": "energy"})], "Voici [1]."])
    res = client.post("/api/chat", json={"question": "Projets énergie ?", "history": []})
    assert res.status_code == 200
    body = res.get_json()
    assert body["answer"] == "Voici [1]." and body["sources"][0]["ref"] == 1
    assert body["steps"][0]["tool"] == "search_projects" and body["run_id"]


def test_chat_api_validation(tmp_path):
    client = client_with(tmp_path, [])
    assert client.post("/api/chat", json={"question": "  "}).status_code == 400
    assert client.post("/api/chat", json={"question": "q", "history": "x"}).status_code == 400


def test_chat_api_validation_rejects_bad_history_items(tmp_path):
    client = client_with(tmp_path, [])
    res = client.post("/api/chat", json={"question": "q", "history": ["x"]})
    assert res.status_code == 400 and res.get_json()["error"] == "invalid history"
    res = client.post("/api/chat", json={"question": "q", "history": [{"role": "system", "text": "x"}]})
    assert res.status_code == 400 and res.get_json()["error"] == "invalid history"


def test_chat_api_quota_and_errors(tmp_path):
    client = client_with(tmp_path, [LLMQuotaExceeded("usage limit reached"), LLMCallError("boom")])
    res = client.post("/api/chat", json={"question": "q", "history": []})
    assert res.status_code == 429 and "limit" in res.get_json()["error"]
    res = client.post("/api/chat", json={"question": "q", "history": []})
    body = res.get_json()
    assert res.status_code == 500
    assert "boom" not in body["error"]
    assert "LLMCallError" in body["error"]
