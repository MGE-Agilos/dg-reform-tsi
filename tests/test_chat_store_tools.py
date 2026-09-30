import pytest

from conftest import make_vector_store
from tsi_agent import vector_state
from tsi_agent.chat.store import PersistedInMemoryDocumentStore
from tsi_agent.vector_state import VectorLayerError


def test_persisted_store_loads_and_serializes_as_path(tmp_path):
    path = make_vector_store(tmp_path)
    store = PersistedInMemoryDocumentStore(path=str(path))
    assert store.count_documents() > 5
    assert store.to_dict() == {"type": "tsi_agent.chat.store.PersistedInMemoryDocumentStore",
                               "init_parameters": {"path": str(path)}}
    again = PersistedInMemoryDocumentStore.from_dict(store.to_dict())
    assert again.count_documents() == store.count_documents()


def test_second_instance_shares_storage_without_reloading(tmp_path, monkeypatch):
    path = make_vector_store(tmp_path)
    first = PersistedInMemoryDocumentStore(path=str(path))
    calls = []
    monkeypatch.setattr("tsi_agent.chat.store.read_documents", lambda p: calls.append(p) or [])
    second = PersistedInMemoryDocumentStore(path=str(path))
    assert calls == [] and second.count_documents() == first.count_documents()


def test_missing_store_is_a_clear_error(tmp_path):
    with pytest.raises(VectorLayerError, match="tsi_agent index"):
        PersistedInMemoryDocumentStore(path=str(tmp_path / "nope" / "store.json"))


def test_read_documents_round_trip(tmp_path):
    path = make_vector_store(tmp_path)
    docs = vector_state.read_documents(path)
    assert docs and all(d.embedding for d in docs)
