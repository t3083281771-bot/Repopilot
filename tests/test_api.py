import time

from fastapi.testclient import TestClient

from repopilot.api import create_app
from repopilot.config import Settings


def wait_for(client, endpoint):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        response = client.get(endpoint)
        assert response.status_code == 200
        row = response.json()
        if row["status"] in {"ready", "completed", "failed"}:
            return row
        time.sleep(.03)
    raise AssertionError("Background task did not finish")


def test_full_demo_task_source_evidence_and_replayed_events(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        health = client.get("/api/health").json()
        assert health["provider"] == "demo"
        imported = client.post("/api/repos", json={"source": "demo"})
        assert imported.status_code == 202
        repo_id = imported.json()["id"]
        repo = wait_for(client, f"/api/repos/{repo_id}")
        assert repo["status"] == "ready", repo
        assert "snapshot_path" not in repo
        duplicate = client.post("/api/repos", json={"source": "demo"}).json()
        assert duplicate["id"] == repo_id
        hits = client.post("/api/search", json={"repo_id": repo_id, "query": "calculate_total quantity=0"}).json()
        assert any(c["symbol"] == "calculate_total" for c in hits)
        assert all("embedding" not in c for c in hits)
        task = client.post("/api/tasks", json={"repo_id": repo_id,
            "question": "calculate_total 在 quantity=0 时仍收取商品价格，问题在哪里？"}).json()
        result = wait_for(client, f"/api/tasks/{task['id']}")
        assert result["status"] == "completed", result
        assert result["result"]["metrics"]["provider"] == "demo"
        citations = [c for f in result["result"]["findings"] for c in f["citations"]]
        assert citations
        for citation in citations:
            source = client.get(f"/api/repos/{repo_id}/file", params={
                "path": citation["path"], "start_line": citation["start_line"], "end_line": citation["end_line"]})
            assert source.status_code == 200
            assert source.json()["content"] == citation["content"]
        events = client.get(f"/api/tasks/{task['id']}/events")
        assert "event: progress" in events.text and "event: done" in events.text
        assert client.post(f"/api/tasks/{task['id']}/resume").status_code == 409


def test_api_rejects_unsafe_sources_paths_and_invalid_task(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        for source in ["file:///etc/passwd", "https://localhost/repo", "D:\\secret", "https://github.com/a/b?token=x"]:
            assert client.post("/api/repos", json={"source": source}).status_code == 422
        assert client.post("/api/repos", json={"source": "demo", "ref": "../HEAD"}).status_code == 422
        assert client.get("/api/tasks/missing").status_code == 404
        assert client.get("/api/unknown").status_code == 404
        assert client.post("/api/tasks", json={"repo_id": "missing", "question": "a question"}).status_code == 404


def test_real_provider_missing_key_has_clear_error(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, provider="deepseek"))) as client:
        repo = client.post("/api/repos", json={"source": "demo"}).json()
        wait_for(client, f"/api/repos/{repo['id']}")
        response = client.post("/api/tasks", json={"repo_id": repo["id"], "question": "calculate_total problem"})
        assert response.status_code == 422
        assert "DEEPSEEK_API_KEY" in response.json()["detail"]
        assert client.get("/api/health").json()["model_configured"] is False
