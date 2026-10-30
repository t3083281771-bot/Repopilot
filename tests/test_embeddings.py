import math

import httpx
import pytest

from repopilot.config import Settings
from repopilot.embeddings import APIEmbedder, HashEmbedder, embedding_profile, get_embedder, tokenize


def test_hash_is_deterministic_normalized_and_explicitly_lexical():
    embedder = HashEmbedder()
    first = embedder.embed_query("parseJSON snake_case")
    assert first == HashEmbedder().embed_query("parseJSON snake_case")
    assert len(first) == 384
    assert math.sqrt(sum(value * value for value in first)) == pytest.approx(1)
    assert "Lexical" in embedder.description
    assert embedder.embed_query("") == [0.0] * 384
    assert {"parsejson", "parse", "json", "snake_case", "snake", "case"} <= set(tokenize("parseJSON snake_case"))


def test_shared_symbols_increase_hash_similarity():
    embedder = HashEmbedder()
    query, relevant, unrelated = embedder.embed([
        "normalize_tags", "normalize_tags lower strip tags", "database connection transaction postgres",
    ])
    def dot(vector):
        return sum(left * right for left, right in zip(query, vector))
    assert dot(relevant) > dot(unrelated)


def test_api_reorders_vectors_and_normalizes(tmp_path, monkeypatch):
    def handler(request):
        assert request.url.path == "/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [3, 4]},
        ]})

    client_type = httpx.Client
    monkeypatch.setattr("repopilot.embeddings.httpx.Client", lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs))
    settings = Settings(data_dir=tmp_path, embedding_provider="api", embedding_base_url="https://embeddings.test/v1", embedding_api_key="test-key", embedding_model="model")
    assert APIEmbedder(settings).embed(["one", "two"]) == [[0.6, 0.8], [0.0, 1.0]]


@pytest.mark.parametrize("data", [
    [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [1, 0]}],
    [{"index": 0, "embedding": []}, {"index": 1, "embedding": [1]}],
    [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [1, 0]}],
])
def test_api_rejects_malformed_vectors(tmp_path, monkeypatch, data):
    client_type = httpx.Client
    monkeypatch.setattr("repopilot.embeddings.httpx.Client", lambda **kwargs: client_type(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": data})), **kwargs))
    settings = Settings(data_dir=tmp_path, embedding_base_url="https://embeddings.test", embedding_model="model")
    with pytest.raises(ValueError):
        APIEmbedder(settings).embed(["one", "two"])


def test_api_failure_never_silently_falls_back_to_hash(tmp_path, monkeypatch):
    client_type = httpx.Client
    monkeypatch.setattr("repopilot.embeddings.httpx.Client", lambda **kwargs: client_type(transport=httpx.MockTransport(lambda request: httpx.Response(401)), **kwargs))
    settings = Settings(data_dir=tmp_path, embedding_provider="api", embedding_base_url="https://embeddings.test", embedding_model="model")
    with pytest.raises(RuntimeError, match="HTTP 401"):
        get_embedder(settings).embed_query("query")


def test_embedding_profile_separates_services_without_containing_keys(tmp_path):
    settings = Settings(data_dir=tmp_path, embedding_provider="api", embedding_model="same-dimension-model", embedding_base_url="https://first.test/v1/", embedding_api_key="never-in-profile")
    first = embedding_profile(settings)
    assert "never-in-profile" not in first
    settings.embedding_api_key = "new-key"
    assert embedding_profile(settings) == first
    settings.embedding_base_url = "https://second.test/v1"
    assert embedding_profile(settings) != first
